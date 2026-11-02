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

# PubMed ISO 缩写 → 期刊封面文件名（covers/ 目录）
COVER_MAP = {
    "Am J Sports Med": "AmJSportsMed",
    "Arthroscopy": "Arthroscopy",
    "Arthrosc Tech": "ArthroscTech",
    "Knee Surg Sports Traumatol Arthrosc": "KSSTA",
    "Br J Sports Med": "BJSM",
    "Orthop J Sports Med": "OJSM",
    "J Shoulder Elbow Surg": "JSES",
    "Foot Ankle Int": "FootAnkleInt",
    "Sports Health": "SportsHealth",
    "J ISAKOS": "JISAKOS",
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
            MONTHS = {"jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
                      "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12"}
            if m:
                mk = m.strip().lower()[:3]
                if mk in MONTHS:
                    m = MONTHS[mk]
                elif m.isdigit() and len(m) == 1:
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
    total = len(arts)
    kept = [a for a in arts if not (set(a["ptypes"]) & EXCLUDE_PUBTYPES)]
    log("fetched %d, kept %d after excluding editorials/letters", total, len(kept))
    return kept, target, total, lookback_days


# ---------------- Europe PMC（题图 / 免费 PDF） ----------------

def europepmc_search_pmid(pmid):
    try:
        q = urllib.parse.urlencode({
            "query": "EXT_ID:%s AND SRC:MED" % pmid, "format": "json", "resultType": "lite",
        })
        raw = http_get_retry(EPMC + "/search?" + q, tries=3, delay=2.0)
        data = json.loads(raw)
        hits = data.get("resultList", {}) or {}
        hits = hits.get("result", []) if isinstance(hits, dict) else hits
        return hits[0] if hits else None
    except Exception as e:
        log("europepmc search failed for %s: %s", pmid, e)
        return None


def download_figure(pmcid, pmid):
    """取 PMC 全文里第一张图作为题图/文中图。失败返回 None。"""
    try:
        xml_text = http_get(EPMC + "/%s/fullTextXML" % pmcid, timeout=90)
        m = re.search(r'<graphic[^>]*xlink:href="([^"]+)"', xml_text)
        if not m:
            return None
        href = m.group(1)
        if href.startswith("//"):
            href = "https:" + href
        # 相对文件名（如 xxx-fig1.webp）→ 按 PMC 图床规则拼绝对 URL
        if not href.startswith("http"):
            candidates = [
                "https://europepmc.org/articles/%s/bin/%s" % (pmcid, href),
                "https://www.ncbi.nlm.nih.gov/pmc/articles/%s/bin/%s" % (pmcid, href),
            ]
        else:
            candidates = [href]
        ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
        blob = None
        for u in candidates:
            try:
                blob = http_get(u, timeout=90, binary=True, headers=ua)
                if blob and len(blob) > 5000:
                    break
                blob = None
            except Exception as e:
                log("figure url failed %s: %s", u[:80], e)
                blob = None
        if not blob or len(blob) < 5000 or len(blob) > 5_000_000:
            return None
        if not (blob[:3] == b"\xff\xd8\xff" or blob[:8] == b"\x89PNG\r\n\x1a\n" or blob[:4] == b"RIFF"):
            return None
        # 压缩控制仓库体积（>800px 宽或 >300KB 时压缩）
        try:
            import io
            from PIL import Image
            im = Image.open(io.BytesIO(blob)).convert("RGB")
            w, h = im.size
            if w > 800:
                im = im.resize((800, int(h * 800.0 / w)), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80, optimize=True)
            small = buf.getvalue()
            if len(small) < len(blob):
                blob = small
        except Exception as e:
            log("figure compress skipped for %s: %s", pmid, e)
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


def article_block(arts, max_ab=1600):
    lines = []
    for a in arts:
        ab = a["abstract"].strip().replace("\n", " ")
        if len(ab) > max_ab:
            ab = ab[:max_ab] + "…"
        lines.append("PMID %s | %s | %s" % (a["pmid"], a["jiso"], a["date"]))
        lines.append("标题: %s" % a["title"])
        lines.append("摘要: %s" % ab)
        lines.append("")
    return "\n".join(lines)


def build_selection_prompt(arts, target_date):
    lines = ["今天是 %s。以下 %d 篇文献是昨日新收录到 PubMed 的（剔除编辑评论/来信后）。" % (target_date, len(arts)), ""]
    lines.append(article_block(arts))
    lines.append("""请先完成本期选文与主打深读，按以下 JSON 输出（key 固定，不得增删）：
{
  "headline": "本期主标题，≤30字，冒号分隔式，突出主打结论",
  "editor_note": "编者按，1 段 100-160 字，点出本期最值得关注的结论并联系临床",
  "selected_pmids": ["从上面文献中选出的 10-15 篇的 PMID（含主打），按重要性排序"],
  "focus": {
    "pmid": "主打文献 PMID",
    "badge_cn": "研究类型徽标，如：系统综述+Meta · I-II级证据 · 9项RCT · 556例",
    "badge_en": "对应英文徽标",
    "title_cn": "中文标题（含核心数值结论，≤30字）",
    "title_short": "主打精简题目，≤16字，保留关键数字，不要使用冒号（用于公众号标题）",
    "title_en": "英文原标题（原文照录）",
    "meta": "期刊缩写 · 日期 · 研究类型 · PMID xxx",
    "one_line": "一句话结论，80-120字，含关键数字",
    "points": "要点，60-100字",
    "implications": "对临床的启示，100-160字，可联系近期话题，并指出研究局限",
    "conclusion": "英文 Conclusion，对应 one_line",
    "key_points": "英文 Key points，对应 points",
    "clinical_implications": "英文 Clinical implications，对应 implications"
  },
  "discuss": "文末互动问题 1 句"
}
规则：
1. selected_pmids 必须包含 10-15 篇（含 focus 的 PMID），优先 RCT/Meta/系统综述、长期随访与临床决策价值高的研究。
2. focus 必须是 selected_pmids 之一。
3. PMID 必须与输入一致，不得虚构。
4. 所有文本中不得出现 Markdown 语法。""")
    return "\n".join(lines)


def build_briefs_prompt(subset, target_date):
    lines = ["今天是 %s。请为下列 %d 篇文献撰写条目（均来自本期精选）。" % (target_date, len(subset)), ""]
    lines.append(article_block(subset))
    lines.append("""按以下 JSON 输出（key 固定）：
{
  "briefs": [
    {
      "pmid": "PMID",
      "category": "膝关节/肩关节/髋关节/踝足/肘腕手/运动医学/运动防护/其他 之一",
      "title_cn": "中文标题（≤30字，含关键结论）",
      "title_en": "英文原标题（原文照录）",
      "meta": "期刊缩写 · PMID xxxxxxxx",
      "summary_cn": "PubMed 英文摘要的【完整】中文翻译：逐句对应，研究设计、样本量、主要与次要终点、关键数值（含百分比/均值/置信区间/P 值）、局限与结论全部保留，不得删减、不得概括成几句话说；只翻译、不做评论"
    }
  ]
}
规则：每篇都必须有完整摘要翻译，篇幅应与原文摘要相当；PMID 与输入一致。""")
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
    if art.get("_pmc_url"):
        pdf_html = ('<p style="margin:8px 0 2px 0;font-size:13px;color:#0E7C66;font-weight:bold;">'
                    '📥 开放获取 · 免费全文（点击或复制链接下载 PDF）：</p>'
                    '<p style="margin:0;font-size:12.5px;">'
                    '<a href="%s" style="color:#0E7C66;text-decoration:none;overflow-wrap:break-word;">%s</a></p>'
                    % (art["_pmc_url"], art["_pmc_url"]))
    fig_html = ""
    if art.get("_fig"):
        fig_html = ('<p style="margin:0 0 8px 0;"><img src="imgs/fig_%s.jpg" alt="" '
                    'style="width:100%%;display:block;border-radius:4px;"></p>' % b["pmid"])
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
            '    %(fig_html)s\n'
            '    %(doi_html)s\n'
            '    %(pdf_html)s\n'
            '  </section>\n' % {
                "pmid": b["pmid"], "category": esc(b.get("category", "其他")),
                "title_cn": esc(b.get("title_cn", "")), "title_en": esc(b.get("title_en", "")),
                "meta": esc(b.get("meta", "")), "summary_cn": esc(b.get("summary_cn", "")),
                "summary_en": esc(b.get("summary_en", "")),
                "fig_html": fig_html,
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
    if data.get("_window", 2) > 2:
        stats = "近 %d 天 10 本核心期刊新收录 <b>%d 篇</b> → 精选 <b>%d 篇</b>（剔除编辑评论/来信）" % (
            data["_window"], data["_total"], data["_kept"])
    else:
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
  <p style="margin:0;font-size:13.5px;color:#8A6D1B;line-height:1.8;">① 点黄色按钮复制全文（中英双语版）→ 公众号编辑器 Ctrl+V（文字样式完整；若题图提示"上传失败"，见②）；② 题图单独传：点卡片右上角「复制题图」→ 回到编辑器中该题图位置 Ctrl+V，编辑器会直接当图片上传（100%% 成功）；③ 拆发单篇用「复制本篇」。题图文件也在工作区 imgs 文件夹，可在编辑器手动上传。</p>
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
    <p style="margin:0 0 6px 0;font-size:13px;color:#64748B;line-height:1.8;"><strong style="color:#334155;">关于本栏目：</strong>每日定时检索 10 本运动医学核心期刊前一日新收录文献，精选 10-15 篇：1 篇主打深读 + 每篇完整中英双语摘要；开放获取文章配文中图片与免费全文 PDF。</p>
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


def render_wechat_page(data, arts_by_pmid, issue, date_str, target, has_img):
    """微信 draft/add 专用精简版：内容全保留、样式瘦身（正文需 <2 万字符）。"""
    out = []
    out.append('<div id="content">')
    out.append('<p id="wx_title_short" style="display:none;">%s</p>' % esc(data.get("focus", {}).get("title_short", "")))
    out.append('<section>')
    out.append('<p style="text-align:center;font-size:12px;color:#888;letter-spacing:2px;">'
               'SPORTS MEDICINE &amp; ARTHROSCOPY DIGEST</p>')
    out.append('<p style="text-align:center;font-size:20px;font-weight:bold;color:#1E5AA8;line-height:1.45;">%s</p>'
               % esc(data.get("headline", "")))
    out.append('<p style="text-align:center;font-size:13px;color:#888;">第 %s 期 · %s</p>' % (issue, date_str))
    out.append('</section>')
    if data.get("_window", 2) > 2:
        out.append('<p style="font-size:12.5px;color:#666;text-align:center;">近 %d 天 10 本核心期刊新收录 %d 篇 → 精选 %d 篇（剔除编辑评论/来信）</p>'
                   % (data["_window"], data["_total"], data["_kept"]))
    else:
        out.append('<p style="font-size:12.5px;color:#666;text-align:center;">%s 10 本核心期刊新收录 %d 篇 → 精选 %d 篇（剔除编辑评论/来信）</p>'
                   % (target.strftime("%m月%d日"), data["_total"], data["_kept"]))
    if data.get("editor_note"):
        out.append('<p style="font-size:15px;color:#1E3A5F;line-height:1.9;">%s</p>' % esc(data["editor_note"]))

    f = data["focus"]
    out.append('<p style="font-size:17px;font-weight:bold;color:#0E7C6B;">▍今日主打 · TODAY’S FOCUS</p>')
    out.append('<section>')
    if has_img:
        out.append('<img src="imgs/img_%s.jpg" style="width:100%%;">' % f["pmid"])
    out.append('<p style="font-size:11.5px;color:#666;">%s · %s</p>' % (esc(f.get("badge_cn", "")), esc(f.get("badge_en", ""))))
    out.append('<p style="font-size:17px;font-weight:bold;line-height:1.5;">%s</p>' % esc(f.get("title_cn", "")))
    out.append('<p style="font-size:12.5px;color:#666;font-style:italic;">%s</p>' % esc(f.get("title_en", "")))
    out.append('<p style="font-size:12.5px;color:#999;">%s</p>' % esc(f.get("meta", "")))
    out.append('<p style="font-size:15px;color:#0E7C66;line-height:1.8;"><strong>一句话结论：</strong>%s</p>'
               % esc(f.get("one_line", "")))
    out.append('<p style="font-size:14.5px;line-height:1.85;"><strong style="color:#1E5AA8;">要点：</strong>%s</p>'
               % esc(f.get("points", "")))
    out.append('<p style="font-size:14.5px;line-height:1.85;"><strong style="color:#C05621;">对临床的启示：</strong>%s</p>'
               % esc(f.get("implications", "")))
    out.append('<p style="font-size:14.5px;line-height:1.85;"><strong>Conclusion:</strong> %s</p>'
               % esc(f.get("conclusion", "")))
    out.append('<p style="font-size:14.5px;line-height:1.85;"><strong>Key points:</strong> %s</p>'
               % esc(f.get("key_points", "")))
    out.append('<p style="font-size:14.5px;line-height:1.85;"><strong>Clinical implications:</strong> %s</p>'
               % esc(f.get("clinical_implications", "")))
    art = arts_by_pmid.get(f["pmid"], {})
    if art.get("doi"):
        out.append('<p style="font-size:12px;color:#999;">原文 / Source：https://doi.org/%s</p>' % esc(art["doi"]))
    out.append('</section>')

    out.append('<p style="font-size:17px;font-weight:bold;color:#64748B;">▍其余 %d 篇 · 一屏看完</p>'
               % len(data.get("briefs", [])))
    for b in data.get("briefs", []):
        pmid = b.get("pmid", "")
        if pmid not in arts_by_pmid:
            continue
        art = arts_by_pmid[pmid]
        out.append('<section>')
        cov = COVER_MAP.get(art.get("jiso", ""))
        if cov:
            out.append('<img src="covers/%s.jpg" style="width:100%%;">' % cov)
        out.append('<p style="font-size:15.5px;font-weight:bold;line-height:1.5;">[%s] %s</p>'
                   % (esc(b.get("category", "其他")), esc(b.get("title_cn", ""))))
        out.append('<p style="font-size:12px;color:#666;font-style:italic;">%s</p>' % esc(b.get("title_en", "")))
        out.append('<p style="font-size:12.5px;color:#999;">%s</p>' % esc(b.get("meta", "")))
        out.append('<p style="font-size:14px;line-height:1.85;"><strong style="color:#0E7C66;">摘要：</strong>%s</p>'
                   % esc(b.get("summary_cn", "")))
        out.append('<!--en--><p style="font-size:13.5px;color:#666;font-style:italic;line-height:1.8;">%s</p>'
                   % esc(b.get("summary_en", "")))
        if art.get("_fig"):
            out.append('<img src="imgs/fig_%s.jpg" style="width:100%%;">' % pmid)
        if art.get("doi"):
            out.append('<p style="font-size:12px;color:#999;">原文：https://doi.org/%s</p>' % esc(art["doi"]))
        if art.get("_pmc_url"):
            out.append('<p style="font-size:13.5px;color:#0E7C66;font-weight:bold;margin-top:8px;">📥 开放获取 · 免费全文（复制下方链接到浏览器可下载 PDF）：</p>')
            out.append('<p style="font-size:12.5px;color:#0E7C66;">%s</p>' % art["_pmc_url"])
        out.append('</section>')
        out.append('<p style="text-align:center;color:#B7C2D6;font-size:13px;margin:16px 0 8px 0;">· · · · ·</p>')

    out.append('<p style="font-size:14.5px;color:#8A6D1B;"><strong>💬 一起聊聊：</strong>%s</p>'
               % esc(data.get("discuss", "")))
    out.append('<p style="font-size:12px;color:#999;line-height:1.8;">本内容由 AI 辅助整理，仅供学术交流参考，'
               '观点与数据请以原文为准，不构成诊疗建议。文献题录与摘要来源：PubMed。</p>')
    out.append('<p style="font-size:11px;color:#CBD5E1;text-align:center;">— 运动医学 · 关节镜文献速览 · 第 %s 期 —</p>'
               % issue)
    out.append('</div>')
    return "\n".join(out)


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

    arts, target, total, win = fetch_new_articles(journals)
    if 0 < len(arts) < 10:
        log("only %d articles in %d-day window (weekend/holiday?), widening to 4 days", len(arts), win)
        arts4, target4, total4, win4 = fetch_new_articles(journals, lookback_days=4)
        if len(arts4) >= 10:
            arts, target, total, win = arts4, target4, total4, win4
        else:
            log("still only %d in 4-day window, keep %d-day result", len(arts4), win)
    log("fetched %d articles for %s (window %d days)", len(arts), target, win)
    if not arts:
        log("无新文献，跳过")
        sys.exit(0)

    arts_by_pmid = {a["pmid"]: a for a in arts}
    sel = call_deepseek(build_selection_prompt(arts, target.strftime("%Y-%m-%d")))
    data = {
        "headline": sel.get("headline", ""),
        "editor_note": sel.get("editor_note", ""),
        "discuss": sel.get("discuss", ""),
        "focus": sel.get("focus", {}) or {},
        "_total": total,
        "_window": win,
    }

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

    # 选文列表（10-15 篇，含主打）
    selected = []
    for p in (sel.get("selected_pmids") or []):
        p = str(p).strip()
        if p in arts_by_pmid and p not in selected:
            selected.append(p)
    if focus_pmid not in selected:
        selected.insert(0, focus_pmid)
    selected = selected[:15]
    log("LLM selected %d pmids: %s", len(selected), ",".join(selected))

    # 分批生成【完整摘要翻译】（每批 6 篇，避免输出超限）
    brief_pmids = [p for p in selected if p != focus_pmid]
    uniq = []
    for i in range(0, len(brief_pmids), 6):
        batch = brief_pmids[i:i + 6]
        subset = [arts_by_pmid[p] for p in batch]
        try:
            res = call_deepseek(build_briefs_prompt(subset, target.strftime("%Y-%m-%d")))
        except Exception as e:
            log("briefs batch %d failed: %s", i // 6 + 1, e)
            res = {}
        got = {}
        for b in (res.get("briefs") or []):
            if b.get("pmid") in arts_by_pmid:
                got[b["pmid"]] = b
        for p in batch:
            b = got.get(p)
            if not b:
                a = arts_by_pmid[p]
                b = {
                    "pmid": p, "category": "其他",
                    "title_cn": a["title"][:30], "title_en": a["title"],
                    "meta": "%s · PMID %s" % (a["jiso"], p),
                    "summary_cn": a["abstract"].strip()[:600],
                }
                log("brief fallback for %s", p)
            b["summary_en"] = arts_by_pmid[p]["abstract"].strip()
            uniq.append(b)
        log("briefs batch %d done (%d articles)", i // 6 + 1, len(batch))

    # 数量控制：10-15 篇（briefs 最多 14 + 1 篇主打）
    if len(uniq) > 14:
        log("briefs %d -> 截断到 14", len(uniq))
        uniq = uniq[:14]
    covered = {b["pmid"] for b in uniq} | {focus_pmid}
    for a in arts:
        if len(covered) >= 10:
            break
        if a["pmid"] in covered:
            continue
        ab = a["abstract"].strip()
        uniq.append({
            "pmid": a["pmid"], "category": "其他",
            "title_cn": a["title"][:30], "title_en": a["title"],
            "meta": "%s · PMID %s" % (a["jiso"], a["pmid"]),
            "summary_cn": ab[:600],
            "summary_en": ab,
        })
        covered.add(a["pmid"])
        log("补卡 fallback for %s", a["pmid"])
    data["briefs"] = uniq
    data["_kept"] = 1 + len(uniq)
    log("selected %d articles (1 focus + %d briefs)", data["_kept"], len(uniq))

    # 图与免费全文：主打题图 + 各篇文中图 / PDF（仅开放获取）
    selected = [focus_pmid] + [b["pmid"] for b in uniq]
    has_img = False
    for pmid in selected:
        a = arts_by_pmid[pmid]
        hit = europepmc_search_pmid(pmid)
        pmcid = hit.get("pmcid") if hit else ""
        if not pmcid:
            continue
        blob = download_figure(pmcid, pmid)
        if blob:
            fname = ("img_%s.jpg" if pmid == focus_pmid else "fig_%s.jpg") % pmid
            with open(os.path.join(REPO, "imgs", fname), "wb") as fp:
                fp.write(blob)
            if pmid == focus_pmid:
                has_img = True
                log("hero image saved for %s", pmid)
            else:
                a["_fig"] = True
                log("figure saved for %s", pmid)
        # 免费全文：直接给 PMC 链接（不再把大文件下载进仓库）
        a["_pmc_url"] = "https://www.ncbi.nlm.nih.gov/pmc/articles/%s/" % pmcid
        log("pmc link for %s", pmid)

    issue = int(state.get("issue", 21)) + 1
    date_str = today_bj.strftime("%Y-%m-%d")
    html = render_page(data, arts_by_pmid, issue, date_str, target, has_img)
    with open(os.path.join(REPO, "index.html"), "w", encoding="utf-8") as fp:
        fp.write(html)
    log("index.html written (%d bytes)", len(html.encode("utf-8")))

    whtml = render_wechat_page(data, arts_by_pmid, issue, date_str, target, has_img)
    with open(os.path.join(REPO, "wechat.html"), "w", encoding="utf-8") as fp:
        fp.write(whtml)
    log("wechat.html written (%d bytes)", len(whtml.encode("utf-8")))

    state["issue"] = issue
    state["last_digest_date"] = date_str
    with open(state_path, "w", encoding="utf-8") as fp:
        json.dump(state, fp, ensure_ascii=False, indent=2)

    git_commit_push(date_str)
    log("done")


if __name__ == "__main__":
    main()
