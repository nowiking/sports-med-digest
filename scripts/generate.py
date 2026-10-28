#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
运动医学·关节镜文献速览 — 每日生成脚本（在 GitHub Actions ubuntu-latest 上运行）

流程：
1. PubMed E-utilities 检索 10 本核心期刊「前一日(北京时间)」新收录文献
2. 剔除编辑评论/来信等非研究类型
3. 调用 DeepSeek API 生成中英双语精要（JSON）
4. 渲染 index.html（沿用现有版式），尽力抓取主打文章题图与免费全文 PDF
5. git 提交推送 → GitHub Pages 自动更新

环境变量：
  DEEPSEEK_API_KEY  必填（DeepSeek 官方 API key）
  GITHUB_TOKEN      Actions 自动注入，用于推送
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BASE)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
DS_URL = "https://api.deepseek.com/chat/completions"

EXCLUDE_PUBTYPES = {
    "Editorial", "Comment", "Letter", "Erratum", "Correction",
    "Retraction of Publication", "News", "Published Erratum", "Biography",
}

MAX_LLM_RETRY = 3


def log(*a):
    print("[digest]", *a, flush=True)


# ---------------- HTTP helpers ----------------

def http_get(url, timeout=120, binary=False, headers=None):
    h = {"User-Agent": "sports-med-digest/1.0"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


def http_post_json(url, payload, headers=None, timeout=300):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json", "User-Agent": "sports-med-digest/1.0"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def http_get_retry(url, tries=4, delay=2.0, binary=False):
    for i in range(tries):
        try:
            return http_get(url, binary=binary)
        except Exception as e:
            if i == tries - 1:
                raise
            log("retry(%d) %s: %s", i + 1, url[:90], e)
            time.sleep(delay * (i + 1))


# ---------------- PubMed ----------------

def esearch_pmids(term, retmax=300):
    q = urllib.parse.urlencode({
        "db": "pubmed", "term": term, "retmode": "json", "retmax": str(retmax),
    })
    data = json.loads(http_get_retry(EUTILS + "/esearch.fcgi?" + q))
    return data.get("esearchresult", {}).get("idlist", [])


def efetch_articles(pmids):
    if not pmids:
        return []
    q = urllib.parse.urlencode({
        "db": "pubmed", "id": ",".join(pmids), "retmode": "xml",
    })
    xml_text = http_get_retry(EUTILS + "/efetch.fcgi?" + q)
    root = ET.fromstring(xml_text)
    arts = []
    for pa in root.iter("PubmedArticle"):
        pmid = pa.findtext(".//MedlineCitation/PMID") or ""
        title_el = pa.find(".//Article/ArticleTitle")
        title = "".join(title_el.itertext()).strip() if title_el is not None else ""
        journal = pa.findtext(".//Journal/Title") or ""
        jiso = pa.findtext(".//Journal/ISOAbbreviation") or journal
        # date
        y = pa.findtext(".//JournalIssue/PubDate/Year")
        m = pa.findtext(".//JournalIssue/PubDate/Month")
        d = pa.findtext(".//JournalIssue/PubDate/Day")
        if not (y and m):
            ad = pa.find(".//ArticleDate")
            if ad is not None:
                y = ad.findtext("Year"); m = ad.findtext("Month"); d = ad.findtext("Day")
        date = "-".join([x for x in (y, m, d) if x]) or ""
        try:
            if m and m.isdigit() and len(m) == 1:
                m = "0" + m
            if d and d.isdigit() and len(d) == 1:
                d = "0" + d
            date = "-".join([x for x in (y, m, d) if x])
        except Exception:
            pass
        ab_els = pa.findall(".//Abstract/AbstractText")
        abstract = " ".join("".join(t.itertext()).strip() for t in ab_els)
        doi = ""
        for eid in pa.findall(".//ELocationID"):
            if eid.get("EIdType") == "doi":
                doi = eid.text or ""
        ptypes = [p.text or "" for p in pa.findall(".//PublicationTypeList/PublicationType")]
        arts.append({
            "pmid": pmid, "title": title, "journal": journal, "jiso": jiso,
            "date": date, "abstract": abstract, "doi": doi, "ptypes": ptypes,
        })
    return arts


def fetch_new_articles(journals, lookback_days=2):
    """检索最近 lookback_days 天内（含昨日）新收录文献；优先昨天。"""
    term_j = " OR ".join('"%s"[Journal]' % j for j in journals)
    today_bj = datetime.now(BEIJING).date()
    target = today_bj - timedelta(days=1)
    start = target - timedelta(days=lookback_days - 1)
    ds0 = start.strftime("%Y/%m/%d")
    ds1 = target.strftime("%Y/%m/%d")
    term = '(%s) AND ("%s"[edat] : "%s"[edat])' % (term_j, ds0, ds1)
    log("esearch term:", term[:200])
    pmids = esearch_pmids(term)
    arts = efetch_articles(pmids)
    kept = [a for a in arts if not (set(a["ptypes"]) & EXCLUDE_PUBTYPES)]
    return kept, target


# ---------------- Europe PMC（题图 / 免费 PDF） ----------------

def europepmc_search_pmid(pmid):
    try:
        q = urllib.parse.urlencode({
            "query": "EXT_ID:%s AND SRC:MED" % pmid, "format": "json", "resultType": "lite",
        })
        data = json.loads(http_get(EPMC + "/search?" + q, timeout=60))
        hits = data.get("resultList", [])
        return hits[0] if hits else None
    except Exception as e:
        log("europepmc search failed for %s: %s", pmid, e)
        return None


def download_figure(pmcid, pmid):
    """取 PMC 全文里第一张图作为题图。失败返回 None。"""
    try:
        xml_text = http_get(EPMC + "/%s/fullTextXML" % pmcid, timeout=90)
        m = re.search(r'<graphic[^>]*xlink:href="([^"]+)"', xml_text)
        if not m:
            return None
        url = m.group(1)
        if url.startswith("//"):
            url = "https:" + url
        blob = http_get_retry(url, tries=2, binary=True)
        if len(blob) < 5000 or len(blob) > 3_000_000:
            return None
        if not (blob[:3] == b"\xff\xd8\xff" or blob[:8] == b"\x89PNG\r\n\x1a\n"):
            return None
        return blob
    except Exception as e:
        log("figure download failed for %s: %s", pmid, e)
        return None


def download_pdf(pmcid, pmid):
    try:
        blob = http_get(EPMC + "/%s/fullTextPDF" % pmcid, timeout=120, binary=True)
        if len(blob) < 2000 or len(blob) > 15_000_000 or not blob.startswith(b"%PDF"):
            return None
        return blob
    except Exception as e:
        log("pdf download failed for %s: %s", pmid, e)
        return None


# ---------------- DeepSeek LLM ----------------

SYSTEM_PROMPT = """你是运动医学领域的资深医学编辑，为公众号「运动有术」撰写每日文献速览。写作要求：
- 中文严谨、客观、克制、量化驱动，符合运动医学科医生阅读习惯；英文为对应意译而非逐字翻译
- 标题用冒号分隔的描述性句式，突出核心数值结论
- 只输出一个合法 JSON 对象，不要输出任何解释文字或代码块标记"""


def build_user_prompt(arts, target_date):
    lines = []
    lines.append("今天是 %s。以下 %d 篇文献是昨天新收录到 PubMed 的（剔除编辑评论/来信后）。" % (target_date, len(arts)))
    lines.append("")
    for a in arts:
        ab = a["abstract"].strip().replace("\n", " ")
        if len(ab) > 1400:
            ab = ab[:1400] + "…"
        lines.append("PMID %s | %s | %s" % (a["pmid"], a["jiso"], a["date"]))
        lines.append("标题: %s" % a["title"])
        lines.append("摘要: %s" % ab)
        lines.append("")
    lines.append("""请按以下字段产出 JSON（key 固定，不得增删）：
{
  "headline": "本期主标题，≤30字，冒号分隔式，突出主打结论",
  "editor_note": "编者按，1 段 100-180 字，点出本期最值得关注的结论并联系临床",
  "focus": {
    "pmid": "主打文献 PMID",
    "badge_cn": "研究类型徽标，如：系统综述+Meta · I-II级证据 · 9项RCT · 556例",
    "badge_en": "对应英文徽标",
    "title_cn": "中文标题（含核心数值结论，≤30字）",
    "title_en": "英文原标题（原文照录）",
    "meta": "期刊缩写 · 日期 · 研究类型 · PMID xxx",
    "one_line": "一句话结论，80-150字，含关键数字",
    "points": "要点，60-120字",
    "implications": "对临床的启示，120-220字，可联系近期话题，并指出研究局限",
    "conclusion": "英文 Conclusion，对应 one_line",
    "key_points": "英文 Key points，对应 points",
    "clinical_implications": "英文 Clinical implications，对应 implications"
  },
  "briefs": [
    {
      "pmid": "PMID",
      "category": "膝关节/肩关节/髋关节/踝足/肘腕手/运动医学/运动防护/其他 之一",
      "title_cn": "中文标题（≤30字，含关键结论）",
      "title_en": "英文原标题（原文照录）",
      "meta": "期刊缩写 · PMID xxxxxxxx",
      "summary_cn": "中文摘要，50-90字，含关键数字",
      "summary_en": "对应英文摘要"
    }
  ],
  "discuss": "文末互动问题 1 句"
}
规则：
1. focus 从文献中挑 1 篇：优先 RCT/Meta/系统综述或临床决策影响大的研究；技术短篇只有当没有更好选择时才可作主打。
2. briefs 必须覆盖除 focus 外的每一篇文献，PMID 必须与输入完全一致，不得虚构。
3. meta 中期刊用输入里给的标准缩写。
4. 所有文本中不得出现 Markdown 语法。""")
    return "\n".join(lines)


def call_deepseek(prompt):
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 8192,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"]}
    for i in range(MAX_LLM_RETRY):
        try:
            resp = http_post_json(DS_URL, payload, headers=headers)
            content = resp["choices"][0]["message"]["content"]
            data = json.loads(content)
            return data
        except Exception as e:
            log("llm attempt %d failed: %s", i + 1, e)
            time.sleep(8 * (i + 1))
    raise RuntimeError("DeepSeek API 调用失败")


# ---------------- HTML 渲染 ----------------

def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def focus_card_html(f, art, has_img):
    img_html = ""
    img_btn = ""
    if has_img:
        img_html = ('<img class="hero" src="imgs/img_%s.jpg" alt="" '
                    'style="width:100%%;box-sizing:border-box;display:block;border-radius:4px;margin:0 0 10px 0;">' % f["pmid"])
        img_btn = ('<button class="cpbtn" onclick="copyImg(\'card_%s\')" '
                   'style="position:absolute;top:8px;right:8px;border:none;background:rgba(255,255,255,.92);'
                   'border-radius:6px;font-size:11.5px;color:#64748B;padding:4px 10px;cursor:pointer;">复制题图</button>' % f["pmid"])
    doi = art.get("doi", "")
    doi_html = ""
    if doi:
        doi_html = ('<p style="margin:10px 0 0 0;font-size:12px;color:#94A3B8;">原文 / Source：'
                    '<a href="https://doi.org/%s" style="color:#1E5AA8;text-decoration:none;overflow-wrap:break-word;">doi.org/%s</a></p>'
                    % (esc(doi), esc(doi)))
    return ('\n  <section id="card_%(pmid)s" style="width:100%%;box-sizing:border-box;background-color:#FFFFFF;'
            'border-radius:2px;padding:14px 10px;margin:10px 0;position:relative;">\n'
            '    %(img)s\n'
            '    %(imgbtn)s\n'
            '    <button class="cpbtn" onclick="copyOne(\'card_%(pmid)s\')" style="position:absolute;top:36px;right:8px;'
            'border:none;background:rgba(255,255,255,.92);border-radius:6px;font-size:11.5px;color:#64748B;padding:4px 10px;cursor:pointer;">复制本篇</button>\n'
            '    <p style="margin:0 0 8px 0;"><span style="display:inline-block;background-color:#F1F5F9;color:#64748B;'
            'font-size:11.5px;padding:3px 10px;border-radius:20px;">%(badge_cn)s</span> '
            '<span style="display:inline-block;background-color:#FFFFFF;color:#94A3B8;font-size:11px;padding:3px 10px;'
            'border-radius:20px;border:1px solid #E2E8F0;">%(badge_en)s</span></p>\n'
            '    <p style="margin:0 0 6px 0;font-size:17.5px;font-weight:bold;color:#0F172A;line-height:1.5;">%(title_cn)s</p>\n'
            '    <p style="margin:0 0 9px 0;font-size:12.5px;color:#64748B;line-height:1.6;font-style:italic;'
            'overflow-wrap:break-word;word-break:break-word;">%(title_en)s</p>\n'
            '    <p style="margin:0 0 10px 0;font-size:12.5px;color:#94A3B8;">%(meta)s</p>\n'
            '    <p style="margin:0 0 7px 0;font-size:15px;color:#0E7C66;line-height:1.8;">'
            '<strong>一句话结论：</strong>%(one_line)s</p>\n'
            '    <p style="margin:0 0 5px 0;font-size:14.5px;color:#334155;line-height:1.85;">'
            '<strong style="color:#1E5AA8;">要点：</strong>%(points)s</p>\n'
            '    <p style="margin:0;font-size:14.5px;color:#334155;line-height:1.85;">'
            '<strong style="color:#C05621;">对临床的启示：</strong>%(implications)s</p>\n'
            '    <section style="margin:10px 0 0 0;padding-top:8px;border-top:1px dashed #E2E8F0;"></section>\n'
            '    <p style="margin:8px 0 7px 0;font-size:15px;color:#0E7C66;line-height:1.8;">'
            '<strong>Conclusion:</strong> %(conclusion)s</p>\n'
            '    <p style="margin:0 0 5px 0;font-size:14.5px;color:#334155;line-height:1.85;">'
            '<strong style="color:#1E5AA8;">Key points:</strong> %(key_points)s</p>\n'
            '    <p style="margin:0;font-size:14.5px;color:#334155;line-height:1.85;">'
            '<strong style="color:#C05621;">Clinical implications:</strong> %(clinical_implications)s</p>\n'
            '    %(doi_html)s\n'
            '  </section>\n' % {
                "pmid": f["pmid"], "img": img_html, "imgbtn": img_btn,
                "badge_cn": esc(f.get("badge_cn", "")), "badge_en": esc(f.get("badge_en", "")),
                "title_cn": esc(f.get("title_cn", "")), "title_en": esc(f.get("title_en", "")),
                "meta": esc(f.get("meta", "")), "one_line": esc(f.get("one_line", "")),
                "points": esc(f.get("points", "")), "implications": esc(f.get("implications", "")),
                "conclusion": esc(f.get("conclusion", "")), "key_points": esc(f.get("key_points", "")),
                "clinical_implications": esc(f.get("clinical_implications", "")),
                "doi_html": doi_html,
            })


def brief_card_html(b, art, pdf_ready):
    doi = art.get("doi", "")
    doi_html = ""
    if doi:
        doi_html = ('<p style="margin:8px 0 0 0;font-size:12px;color:#94A3B8;">原文：'
                    '<a href="https://doi.org/%s" style="color:#1E5AA8;text-decoration:none;overflow-wrap:break-word;">doi.org/%s</a></p>'
                    % (esc(doi), esc(doi)))
    pdf_html = ""
    if pdf_ready:
        pdf_html = ('<p id="pdflink_%(pmid)s" style="margin:6px 0 0 0;font-size:12.5px;">'
                    '<a href="pdfs/%(pmid)s.pdf" style="color:#0E7C66;text-decoration:none;">📥 下载PDF（免费全文）</a></p>'
                    % {"pmid": b["pmid"]})
    return ('\n  <section id="card_%(pmid)s" style="width:100%%;box-sizing:border-box;background-color:#FFFFFF;'
            'border-radius:2px;padding:14px 10px;margin:10px 0;position:relative;">\n'
            '    <p style="margin:0 0 5px 0;"><span style="display:inline-block;background-color:#1E5AA8;color:#FFFFFF;'
            'font-size:11px;padding:2px 9px;border-radius:10px;">%(category)s</span></p>\n'
            '    <p style="margin:0 0 4px 0;font-size:15.5px;font-weight:bold;color:#0F172A;line-height:1.5;">%(title_cn)s</p>\n'
            '    <p style="margin:0 0 6px 0;font-size:12px;color:#64748B;line-height:1.6;font-style:italic;'
            'overflow-wrap:break-word;word-break:break-word;">%(title_en)s</p>\n'
            '    <p style="margin:0 0 6px 0;font-size:12.5px;color:#94A3B8;">%(meta)s</p>\n'
            '    <p style="margin:0 0 6px 0;font-size:14px;color:#334155;line-height:1.85;">'
            '<strong style="color:#0E7C66;">摘要：</strong>%(summary_cn)s</p>\n'
            '    <section style="margin:8px 0 0 0;padding-top:6px;border-top:1px dashed #E2E8F0;"></section>\n'
            '    <p style="margin:8px 0 0 0;font-size:13.5px;color:#64748B;line-height:1.8;font-style:italic;">%(summary_en)s</p>\n'
            '    %(doi_html)s\n'
            '    %(pdf_html)s\n'
            '  </section>\n' % {
                "pmid": b["pmid"], "category": esc(b.get("category", "其他")),
                "title_cn": esc(b.get("title_cn", "")), "title_en": esc(b.get("title_en", "")),
                "meta": esc(b.get("meta", "")), "summary_cn": esc(b.get("summary_cn", "")),
                "summary_en": esc(b.get("summary_en", "")),
                "doi_html": doi_html, "pdf_html": pdf_html,
            })


PAGE_JS = r"""<script>
function clean(html){const t=document.createElement('div');t.innerHTML=html;t.querySelectorAll('.cpbtn,.noexport').forEach(n=>n.remove());return t.innerHTML;}
function toast(m){const el=document.getElementById('toastbar');el.textContent=m;setTimeout(()=>el.textContent='就绪 / Ready',3500);}
async function copyHTML(html,msg){
  try{
    const item=new ClipboardItem({'text/html':new Blob([html],{type:'text/html'}),'text/plain':new Blob([html.replace(/<[^>]+>/g,' ')],{type:'text/plain'})});
    await navigator.clipboard.write([item]);toast(msg);
  }catch(e){
    const dv=document.createElement('div');dv.contentEditable='true';dv.style.cssText='position:fixed;left:-9999px;top:0;';dv.innerHTML=html;document.body.appendChild(dv);
    const r=document.createRange();r.selectNodeContents(dv);const s=getSelection();s.removeAllRanges();s.addRange(r);
    const okc=document.execCommand('copy');s.removeAllRanges();dv.remove();toast(msg+(okc?'':'（失败，请手动全选内容区复制）'));
  }
}
function copyAll(){copyHTML('<section style="max-width:750px;margin:0 auto;">'+clean(document.getElementById('content').innerHTML)+'</section>','✅ 全文已复制（中英双语）');}
function copyOne(id){copyHTML(clean(document.getElementById(id).innerHTML),'✅ 单篇已复制（中英双语）');}
async function copyImg(id){
  try{
    const img=document.querySelector('#'+id+' img.hero');
    if(!img){toast('未找到题图');return;}
    const cnv=document.createElement('canvas');cnv.width=img.naturalWidth;cnv.height=img.naturalHeight;
    cnv.getContext('2d').drawImage(img,0,0);
    const blob=await new Promise(r=>cnv.toBlob(r,'image/png'));
    await navigator.clipboard.write([new ClipboardItem({'image/png':blob})]);
    toast('✅ 题图已复制（PNG）→ 到编辑器对应位置 Ctrl+V 粘贴');
  }catch(e){toast('题图复制失败：'+e.message);}
}
</script>
"""


def render_page(data, arts_by_pmid, issue, date_str, target_date, has_img):
    editor_html = ""
    if data.get("editor_note"):
        editor_html = ('\n  <section style="background-color:#EEF4FB;border:none;border-radius:4px;'
                       'padding:14px 14px;margin:10px 0;">\n'
                       '    <p style="margin:0;font-size:15px;color:#1E3A5F;line-height:1.9;">%s</p>\n'
                       '  </section>' % esc(data["editor_note"]))
    focus = data["focus"]
    focus_card = focus_card_html(focus, arts_by_pmid.get(focus["pmid"], {}), has_img)
    brief_cards = ""
    n_brief = 0
    for b in data.get("briefs", []):
        pmid = b.get("pmid", "")
        art = arts_by_pmid.get(pmid)
        if not art:
            log("brief pmid %s not in fetched set, skip", pmid)
            continue
        brief_cards += brief_card_html(b, art, bool(art.get("_pdf")))
        n_brief += 1
    stats = "%s 10 本核心期刊新收录 <b>%d 篇</b> → 精选 <b>%d 篇</b>（剔除编辑评论/来信）" % (
        target_date.strftime("%m月%d日"), data["_total"], data["_kept"])

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>公众号发文助手 · 文献速览 %(date)s</title></head>
<body style="margin:0;background-color:#F5F7FA;">
<section class="noexport" style="max-width:756px;margin:10px auto 0 auto;padding:0 10px;">
  <p style="margin:0;display:flex;gap:16px;font-size:12.5px;">
    <span style="color:#0F2C4F;font-weight:bold;">运动医学 · 关节镜</span>
    <a href="trauma/" style="color:#8C2F1F;text-decoration:none;">创伤骨科 →</a>
  </p>
</section>
<section style="position:sticky;top:0;z-index:99;background-color:#0F2C4F;padding:12px 16px;display:flex;align-items:center;gap:12px;box-shadow:0 2px 8px rgba(0,0,0,.25);">
  <button onclick="copyAll()" style="background-color:#F2D979;border:none;border-radius:8px;padding:10px 22px;font-size:15px;font-weight:bold;color:#0F2C4F;cursor:pointer;">📋 一键复制全文（中英双语）</button>
  <span id="toastbar" style="color:#BBD3F0;font-size:13px;">就绪 / Ready · 粘贴到公众号编辑器后图片会自动转存</span>
</section>
<section class="noexport" style="max-width:756px;margin:10px auto;padding:10px 14px;background-color:#FFF8E1;border:none;border-radius:6px;">
  <p style="margin:0;font-size:13.5px;color:#8A6D1B;line-height:1.8;">① 点黄色按钮复制全文（中英双语版）→ 公众号编辑器 Ctrl+V（文字样式完整；若题图提示"上传失败"，见②）；② 题图单独传：点卡片右上角「复制题图」→ 回到编辑器中该题图位置 Ctrl+V，编辑器会直接当图片上传（100% 成功）；③ 拆发单篇用「复制本篇」。题图文件也在工作区 imgs 文件夹，可在编辑器手动上传。</p>
</section>
<section style="max-width:760px;margin:0 auto;padding:4px 8px 24px 8px;">
<div id="content">

  <section style="background-color:#1E5AA8;border-radius:12px 12px 0 0;padding:24px 20px 18px 20px;">
    <p style="margin:0;font-size:12px;letter-spacing:2px;color:#BBD3F0;text-align:center;">SPORTS MEDICINE &amp; ARTHROSCOPY DIGEST</p>
    <p style="margin:10px 0 0 0;font-size:21px;font-weight:bold;color:#FFFFFF;text-align:center;line-height:1.45;">%(headline)s</p>
    <p style="margin:10px 0 0 0;font-size:13px;color:#BBD3F0;text-align:center;">第 %(issue)s 期 · %(date)s</p>
  </section>
  <section style="background-color:#164a8c;padding:10px 20px;border-radius:0 0 12px 12px;">
    <p style="margin:0;font-size:12.5px;color:#D8E6F7;text-align:center;line-height:1.7;">%(stats)s</p>
  </section>
%(editor)s
  <section style="margin:18px 0 8px 0;">
    <p style="margin:0;font-size:17px;font-weight:bold;color:#0E7C6B;line-height:1.4;">▍今日主打 <span style="font-weight:normal;font-size:12px;color:#94A3B8;">· TODAY’S FOCUS</span></p>
    <section style="height:3px;background-color:#0E7C6B;border-radius:2px;margin-top:6px;width:56px;"></section>
  </section>
%(focus_card)s
  <section style="margin:22px 0 8px 0;">
    <p style="margin:0;font-size:17px;font-weight:bold;color:#64748B;line-height:1.4;">▍其余 %(n_brief)s 篇 <span style="font-weight:normal;font-size:12px;color:#94A3B8;">· 一屏看完</span></p>
    <section style="height:3px;background-color:#94A3B8;border-radius:2px;margin-top:6px;width:56px;"></section>
  </section>
%(briefs)s
  <section style="background-color:#FFF8E1;border:none;border-radius:4px;padding:14px 14px;margin:10px 0;">
    <p style="margin:0;font-size:14.5px;color:#8A6D1B;line-height:1.9;"><strong>💬 一起聊聊：</strong>%(discuss)s</p>
  </section>

  <section style="background-color:#FFFFFF;border:none;border-radius:0;padding:14px 2px;margin:10px 0;">
    <p style="margin:0 0 6px 0;font-size:13px;color:#64748B;line-height:1.8;"><strong style="color:#334155;">关于本栏目：</strong>每日定时检索 10 本运动医学核心期刊前一日新收录文献，每日聚焦 1 篇值得深读的 + 其余精简速览，AI 辅助生成中英双语精要。</p>
    <p style="margin:0;font-size:12px;color:#94A3B8;line-height:1.8;"><strong style="color:#334155;">声明：</strong>本内容由 AI 辅助整理，仅供学术交流参考，观点与数据请以原文为准，不构成诊疗建议。文献题录与摘要来源：PubMed。</p>
  </section>
  <p style="margin:14px 0 8px 0;font-size:11px;color:#CBD5E1;text-align:center;">— 运动医学 · 关节镜文献速览 · 第 %(issue)s 期 —</p>
</div>
</section>
%(js)s</body></html>
""" % {
        "date": date_str, "issue": issue, "headline": esc(data.get("headline", "")),
        "stats": stats, "editor": editor_html, "focus_card": focus_card,
        "n_brief": n_brief, "briefs": brief_cards, "discuss": esc(data.get("discuss", "")),
        "js": PAGE_JS,
    }
    return html


# ---------------- git ----------------

def git_commit_push(today_str):
    token = os.environ.get("GITHUB_TOKEN", "")
    repo_full = os.environ.get("GITHUB_REPOSITORY", "nowiking/sports-med-digest")
    if not token:
        log("no GITHUB_TOKEN, skip push")
        return
    import subprocess
    def run(args):
        log("git " + " ".join(args))
        subprocess.run(["git", "-C", REPO] + args, check=True)

    run(["config", "user.name", "digest-bot"])
    run(["config", "user.email", "digest-bot@users.noreply.github.com"])
    run(["remote", "set-url", "origin",
         "https://x-access-token:%s@github.com/%s.git" % (token, repo_full)])
    run(["add", "-A"])
    msg = "文献速览更新 %s" % today_str
    r = subprocess.run(["git", "-C", REPO, "commit", "-m", msg],
                       capture_output=True, text=True)
    log("commit rc=%d out=%s", r.returncode, (r.stdout + r.stderr).strip()[:200])
    run(["push", "origin", "HEAD"])


# ---------------- main ----------------

def main():
    if not os.environ.get("DEEPSEEK_API_KEY"):
        log("DEEPSEEK_API_KEY 未设置，退出")
        sys.exit(2)

    with open(os.path.join(BASE, "journals.json"), encoding="utf-8") as fp:
        journals = json.load(fp)["journals"]

    state_path = os.path.join(BASE, "state.json")
    with open(state_path, encoding="utf-8") as fp:
        state = json.load(fp)

    today_bj = datetime.now(BEIJING).date()
    if state.get("last_digest_date") >= today_bj.strftime("%Y-%m-%d"):
        log("今日已生成（%s），跳过", state["last_digest_date"])
        sys.exit(0)

    arts, target = fetch_new_articles(journals)
    log("fetched %d articles for %s", len(arts), target)
    if not arts:
        log("无新文献，跳过")
        sys.exit(0)

    arts_by_pmid = {a["pmid"]: a for a in arts}
    prompt = build_user_prompt(arts, target.strftime("%Y-%m-%d"))
    data = call_deepseek(prompt)
    data["_total"] = len(arts)
    data["_kept"] = len(arts)

    # 校验 focus
    focus_pmid = data.get("focus", {}).get("pmid", "")
    if focus_pmid not in arts_by_pmid:
        focus_pmid = arts[0]["pmid"]
        data["focus"] = {
            "pmid": focus_pmid,
            "badge_cn": "", "badge_en": "",
            "title_cn": arts[0]["title"][:30], "title_en": arts[0]["title"],
            "meta": "%s · %s · PMID %s" % (arts[0]["jiso"], arts[0]["date"], arts[0]["pmid"]),
            "one_line": arts[0]["abstract"][:150], "points": "",
            "implications": "", "conclusion": "", "key_points": "", "clinical_implications": "",
        }
        log("llm focus invalid, fallback to first article")
    # 补缺：LLM 漏掉的文献用原始摘要补一张卡片
    covered = {b.get("pmid") for b in data.get("briefs", [])} | {focus_pmid}
    for a in arts:
        if a["pmid"] not in covered:
            ab = a["abstract"].strip()
            data["briefs"].append({
                "pmid": a["pmid"], "category": "其他",
                "title_cn": a["title"][:30], "title_en": a["title"],
                "meta": "%s · PMID %s" % (a["jiso"], a["pmid"]),
                "summary_cn": ab[:100] + ("…" if len(ab) > 100 else ""),
                "summary_en": "",
            })
            log("补卡 fallback for %s", a["pmid"])

    # 题图 / PDF（主打文章）
    has_img = False
    f_pmid = data["focus"]["pmid"]
    hit = europepmc_search_pmid(f_pmid)
    pmcid = hit.get("pmcid") if hit else ""
    if pmcid:
        blob = download_figure(pmcid, f_pmid)
        if blob:
            with open(os.path.join(REPO, "imgs", "img_%s.jpg" % f_pmid), "wb") as fp:
                fp.write(blob)
            has_img = True
            log("hero image saved for %s", f_pmid)
        else:
            log("no figure for focus %s", f_pmid)
    else:
        log("focus %s not in Europe PMC", f_pmid)

    # 每篇免费全文 PDF
    for a in arts:
        hit2 = europepmc_search_pmid(a["pmid"]) if a["pmid"] != f_pmid else hit
        pcid = hit2.get("pmcid") if hit2 else ""
        if pcid:
            pdf = download_pdf(pcid, a["pmid"])
            if pdf:
                with open(os.path.join(REPO, "pdfs", "%s.pdf" % a["pmid"]), "wb") as fp:
                    fp.write(pdf)
                a["_pdf"] = True
                log("pdf saved for %s", a["pmid"])

    issue = int(state.get("issue", 21)) + 1
    date_str = today_bj.strftime("%Y-%m-%d")
    html = render_page(data, arts_by_pmid, issue, date_str, target, has_img)
    with open(os.path.join(REPO, "index.html"), "w", encoding="utf-8") as fp:
        fp.write(html)
    log("index.html written (%d bytes)", len(html.encode("utf-8")))

    state["issue"] = issue
    state["last_digest_date"] = date_str
    with open(state_path, "w", encoding="utf-8") as fp:
        json.dump(state, fp, ensure_ascii=False, indent=2)

    git_commit_push(date_str)
    log("done")


if __name__ == "__main__":
    main()
