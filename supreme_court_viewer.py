"""U.S. Supreme Court slip opinions viewer generator (Term 2025)."""

import html
import io
import json
import os
import re
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime

try:
    from PyPDF2 import PdfReader
except Exception:
    PdfReader = None

BASE_DIR = os.path.dirname(__file__)
HTML_FILE = os.path.join(BASE_DIR, "supreme_opinions_index.html")
JSON_FILE = os.path.join(BASE_DIR, "supreme_opinions_data.json")
LIST_SOURCE_URL = "https://www.supremecourt.gov/opinions/slipopinion/25"
PDF_TEXT_MAX_PAGES = int(os.getenv("SUPREME_PDF_TEXT_MAX_PAGES", "40"))
PDF_TEXT_MAX_CHARS = int(os.getenv("SUPREME_PDF_TEXT_MAX_CHARS", "220000"))


def fetch_html(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(request, timeout=timeout).read().decode("utf-8", errors="ignore")


def normalize_ws(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_issue_date(raw_date: str) -> str:
    value = normalize_ws(raw_date)
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            continue
    return ""


def dedupe(values: list[str], limit: int = 40) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = normalize_ws(value).strip(" ,.;:")
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def normalize_statute_citation(raw_value: str) -> list[str]:
    value = normalize_ws(raw_value)
    if not value:
        return []

    title_section = re.match(
        r"^Section\s+([0-9A-Za-z._\-]+(?:\([0-9A-Za-z]+\))*)\s+of\s+Title\s+(\d+)$",
        value,
        flags=re.IGNORECASE,
    )
    if title_section:
        return [f"{title_section.group(2)} U.S.C. §{title_section.group(1)}"]

    usc_title_section = re.match(
        r"^Title\s+(\d+)\s+United\s+States\s+Code\s*,?\s*(?:Section|§)\s*([0-9A-Za-z._\-]+(?:\([0-9A-Za-z]+\))*)$",
        value,
        flags=re.IGNORECASE,
    )
    if usc_title_section:
        return [f"{usc_title_section.group(1)} U.S.C. §{usc_title_section.group(2)}"]

    value = re.sub(r"U\.?\s*S\.?\s*C\.?\.?", "U.S.C.", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip(" ,.;:")

    m = re.match(
        r"^(?P<title>\d+)\s+U\.S\.C\.\s*(?P<marker>§{1,2})?\s*(?P<rest>.+)$",
        value,
        flags=re.IGNORECASE,
    )
    if not m:
        return [value]

    title = m.group("title")
    rest = normalize_ws(m.group("rest") or "")
    if not rest:
        return [f"{title} U.S.C."]

    parts = re.split(r"\s*(?:,|;|\band\b|\bor\b)\s*", rest, flags=re.IGNORECASE)
    cites: list[str] = []
    for part in parts:
        chunk = normalize_ws(part).strip(" ,.;:")
        if not chunk:
            continue
        token_match = re.match(
            r"^([0-9A-Za-z._\-]+(?:\([0-9A-Za-z]+\))*)(?:\s+et\s+seq\.)?",
            chunk,
            flags=re.IGNORECASE,
        )
        if not token_match:
            continue
        section = token_match.group(1)
        cites.append(f"{title} U.S.C. §{section}")

    return cites or [value]


def clean_case_citations(items: list[str], title: str | None = None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()

    party = r"[A-Z][A-Za-z0-9'&.\-]*(?:\s+[A-Z][A-Za-z0-9'&.\-]*){0,12}"
    case_core_pattern = re.compile(
        rf"(({party})\s+v\.\s+({party})(?:,\s*\d+\s+[A-Z][A-Za-z.\d\s]{{1,25}}\s+(?:\d+|___)(?:,\s*\d+)?)?(?:\s*\([^)]*\))?)",
        flags=re.IGNORECASE,
    )

    blocked_pattern = re.compile(
        r"Court Description|Petitioner|Respondent|DKT\.?\s*NO\.?|Syllabus|Opinion of the Court|Certiorari|Reporter of Decisions|October Term|^No\.?\s*\d",
        flags=re.IGNORECASE,
    )

    title_key = normalize_ws(title).lower() if title else ""

    for raw in items:
        value = normalize_ws(raw).strip(" ,;:")
        if not value:
            continue

        value = re.sub(r"^[^A-Z]*(?=[A-Z])", "", value)
        value = re.sub(
            r"^(?:See(?:\s+also)?|Cf\.?|But\s+see|Compare|Accord|E\.g\.,?|quoting)\s+",
            "",
            value,
            flags=re.IGNORECASE,
        )

        signal_split = re.split(
            r"\b(?:See(?:\s+also)?|Cf\.?|But\s+see|Compare|Accord|E\.g\.,?|quoting)\b",
            value,
            flags=re.IGNORECASE,
        )
        if len(signal_split) > 1:
            value = normalize_ws(signal_split[-1]).strip(" ,;:")

        value = re.sub(r"\s+\((?:19|20)\d{2}\)$", "", value)

        embedded_case = re.search(
            r"([A-Z][A-Za-z0-9'&.\-]*(?:\s+[A-Z][A-Za-z0-9'&.\-]*){0,10}\s+v\.\s+[A-Z][A-Za-z0-9'&.\-]*(?:\s+[A-Z][A-Za-z0-9'&.\-]*){0,10})",
            value,
        )
        if embedded_case:
            value = normalize_ws(embedded_case.group(1)).strip(" ,;:")

        core = case_core_pattern.search(value)
        if core:
            value = normalize_ws(core.group(1)).strip(" ,;:")

        lowered = value.lower()
        is_title = bool(title_key and lowered == title_key)
        has_v = bool(re.search(r"\bv\.\s", value, flags=re.IGNORECASE))
        has_in_re = bool(re.search(r"^\s*in\s+re\b", value, flags=re.IGNORECASE))

        if blocked_pattern.search(value):
            continue
        if len(value) > 155:
            continue
        if not (is_title or has_v or has_in_re):
            continue

        if has_v:
            parts = re.split(r"\bv\.\s", value, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                left_part = normalize_ws(parts[0])
                right_part = normalize_ws(parts[1])
                if len(left_part) < 3 or len(right_part) < 3:
                    continue
                if re.match(r"^(inc|co|corp|llc|ltd)\.?$", left_part.strip(), flags=re.IGNORECASE):
                    continue

        key = lowered
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(value)

    return cleaned[:40]


def fetch_pdf_text(pdf_url: str, max_pages: int = PDF_TEXT_MAX_PAGES) -> str:
    if PdfReader is None:
        return ""
    try:
        req = urllib.request.Request(pdf_url, headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=30).read()
    except Exception:
        return ""
    if not data:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception:
        return ""

    chunks: list[str] = []
    for page in reader.pages[:max_pages]:
        try:
            txt = page.extract_text() or ""
        except Exception:
            txt = ""
        if txt:
            chunks.append(txt)
    return normalize_ws(" ".join(chunks))[:PDF_TEXT_MAX_CHARS]


def extract_authorities(text: str, title: str, citation: str) -> dict:
    source = normalize_ws(text or "")
    raw_cases: list[str] = []
    raw_statutes: list[str] = []
    rules: list[str] = []
    regulations: list[str] = []
    constitutional: list[str] = []

    if title:
        raw_cases.append(title)
    if citation:
        raw_cases.append(citation)

    party = r"[A-Z][A-Za-z0-9&.'\-]*(?:\s+[A-Za-z0-9&.'\-]+){0,18}"
    case_patterns = [
        rf"(({party})\s+v\.\s+({party}))(?:,\s*\d+\s+(?:U\.?\s*S\.?|S\.?\s*Ct\.?|L\.?\s*Ed\.?\s*\d*d?|F\.?\s*\d+d|F\.?\s*\d+th)\s+\d+(?:,\s*\d+)*)?(?:\s*\([^\)]*(?:19|20)\d{{2}}[^\)]*\))?",
        rf"(In\s+re\s+{party})(?:,\s*\d+\s+(?:U\.?\s*S\.?|S\.?\s*Ct\.?|L\.?\s*Ed\.?\s*\d*d?|F\.?\s*\d+d|F\.?\s*\d+th)\s+\d+(?:,\s*\d+)*)?(?:\s*\([^\)]*(?:19|20)\d{{2}}[^\)]*\))?",
    ]
    for pattern in case_patterns:
        for m in re.finditer(pattern, source):
            raw_cases.append(m.group(1).strip())

    statute_patterns = [
        r"(\d+\s+U\.?\s*S\.?\s*C\.?\s*§+\s*[0-9A-Za-z._\-]+(?:\([a-zA-Z0-9]+\))*)",
        r"(\d+\s+U\.?\s*S\.?\s*C\.?\s*§§\s*[0-9A-Za-z._\-]+(?:\s*(?:,|and)\s*[0-9A-Za-z._\-]+)*)",
        r"(\d+\s+U\.?\s*S\.?\s*C\.?\s*\d+[0-9A-Za-z._\-]*(?:\([a-zA-Z0-9]+\))*)",
        r"(\d+\s+U\.?\s*S\.?\s*C\.?\s*(?:§+\s*)?\d+[0-9A-Za-z._\-]*(?:\([a-zA-Z0-9]+\))*)",
        r"(Title\s+\d+\s+United\s+States\s+Code\s*,?\s*(?:Section|§)\s*[0-9A-Za-z._\-]+(?:\([a-zA-Z0-9]+\))*)",
        r"((?:Section|§)\s*[0-9A-Za-z._\-]+(?:\([a-zA-Z0-9]+\))*\s+of\s+Title\s+\d+)",
    ]
    for pattern in statute_patterns:
        for m in re.finditer(pattern, source, re.IGNORECASE):
            raw_statutes.append(m.group(1))

    title_section_patterns = [
        r"(?:Section|§)\s*([0-9A-Za-z._\-]+(?:\([a-zA-Z0-9]+\))*)\s+of\s+Title\s+(\d+)",
        r"Title\s+(\d+)\s+United\s+States\s+Code\s*,?\s*(?:Section|§)\s*([0-9A-Za-z._\-]+(?:\([a-zA-Z0-9]+\))*)",
    ]
    for m in re.finditer(title_section_patterns[0], source, re.IGNORECASE):
        raw_statutes.append(f"{m.group(2)} U.S.C. §{m.group(1)}")
    for m in re.finditer(title_section_patterns[1], source, re.IGNORECASE):
        raw_statutes.append(f"{m.group(1)} U.S.C. §{m.group(2)}")

    rule_patterns = [
        r"(Fed\.\s*R\.\s*App\.\s*P\.\s*\d+(?:\([a-zA-Z0-9]+\))*)",
        r"(Fed\.\s*R\.\s*Civ\.\s*P\.\s*\d+(?:\([a-zA-Z0-9]+\))*)",
        r"(Fed\.\s*R\.\s*Crim\.\s*P\.\s*\d+(?:\([a-zA-Z0-9]+\))*)",
        r"(Fed\.\s*R\.\s*Evid\.\s*\d+(?:\([a-zA-Z0-9]+\))*)",
        r"(Rule\s+\d+(?:\([a-zA-Z0-9]+\))*)",
        r"(Sup\.?\s*Ct\.?\s*R\.?\s*\d+(?:\.\d+)?(?:\([a-zA-Z0-9]+\))*)",
    ]
    for pattern in rule_patterns:
        for m in re.finditer(pattern, source, re.IGNORECASE):
            rules.append(m.group(1))

    for m in re.finditer(r"(\d+\s+C\.F\.R\.?\s*§+\s*[0-9A-Za-z.\-]+)", source, re.IGNORECASE):
        regulations.append(m.group(1))

    constitutional_patterns = [
        r"((?:First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|Eleventh|Twelfth|Thirteenth|Fourteenth|Fifteenth)\s+Amendment)",
        r"(U\.?\s*S\.?\s*Const\.?\s*(?:amend\.?\s*[IVXLC]+|art\.?\s*[IVXLC]+(?:,\s*§\s*\d+)?))",
    ]
    for pattern in constitutional_patterns:
        for m in re.finditer(pattern, source, re.IGNORECASE):
            constitutional.append(m.group(1))

    cases = clean_case_citations(raw_cases, title=title)

    normalized_statutes: list[str] = []
    for item in raw_statutes:
        normalized_statutes.extend(normalize_statute_citation(item))
    statutes = dedupe(normalized_statutes, limit=30)
    rules = dedupe(rules, limit=30)
    regulations = dedupe(regulations, limit=20)
    constitutional = dedupe(constitutional, limit=20)

    return {
        "citations": {
            "cases": cases,
            "statutes": statutes,
            "rules": rules,
            "regulations": regulations,
        },
        "authorities": {
            "cases": cases,
            "statutes": statutes,
            "rules": rules,
            "regulations": regulations,
            "constitutional": constitutional,
            "other": [],
        },
    }


def parse_rows(page_html: str) -> list[dict]:
    row_pattern = re.compile(
        r"<tr>\s*"
        r"<td[^>]*>\s*(?P<rank>\d+)\s*</td>\s*"
        r"<td[^>]*>\s*(?P<date>[^<]+)\s*</td>\s*"
        r"<td[^>]*>\s*(?P<docket>[^<]+)\s*</td>\s*"
        r"<td[^>]*>\s*(?P<name_cell>.*?)\s*</td>\s*"
        r"<td[^>]*>\s*(?P<justice>[^<]*)\s*</td>\s*"
        r"<td[^>]*>\s*(?P<citation_cell>.*?)\s*</td>\s*"
        r"</tr>",
        flags=re.IGNORECASE | re.DOTALL,
    )

    opinions: list[dict] = []
    seen_pdf: set[str] = set()

    for match in row_pattern.finditer(page_html):
        name_cell = match.group("name_cell")
        citation_cell = match.group("citation_cell")

        link_match = re.search(
            r"<a[^>]*href=['\"](?P<href>[^'\"]+)['\"][^>]*>(?P<title>.*?)</a>",
            name_cell,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not link_match:
            continue

        href = normalize_ws(html.unescape(link_match.group("href")))
        if not href:
            continue
        pdf_url = urllib.parse.urljoin(LIST_SOURCE_URL, href)
        if pdf_url in seen_pdf:
            continue
        seen_pdf.add(pdf_url)

        name = normalize_ws(re.sub(r"<[^>]+>", " ", html.unescape(link_match.group("title"))))
        title_attr_match = re.search(
            r"title=['\"]([^'\"]+)['\"]", link_match.group(0), flags=re.IGNORECASE | re.DOTALL
        )
        summary = normalize_ws(html.unescape(title_attr_match.group(1) if title_attr_match else ""))

        citation = normalize_ws(re.sub(r"<[^>]+>", " ", html.unescape(citation_cell)))
        date_raw = normalize_ws(match.group("date"))
        docket = normalize_ws(match.group("docket"))
        justice = normalize_ws(match.group("justice"))
        rank_text = normalize_ws(match.group("rank"))

        issue_date = parse_issue_date(date_raw)

        opinions.append(
            {
                "id": len(opinions) + 1,
                "rank": int(rank_text) if rank_text.isdigit() else None,
                "title": name or f"Supreme Court Opinion {docket}",
                "date": date_raw,
                "issue_date": issue_date,
                "docket": docket,
                "justice": justice,
                "citation": citation,
                "summary": summary,
                "url": pdf_url,
                "pdf_url": pdf_url,
                "subjects": ["Supreme Court", "Slip Opinions"],
                "published": True,
                "text": summary,
                "citations": {
                    "cases": [citation] if citation else [],
                    "statutes": [],
                    "rules": [],
                    "regulations": [],
                },
                "authorities": {
                    "cases": [name] + ([citation] if citation else []),
                    "statutes": [],
                    "rules": [],
                    "regulations": [],
                    "constitutional": [],
                    "other": [],
                },
            }
        )

    opinions.sort(
        key=lambda op: ((op.get("issue_date") or ""), -(op.get("rank") or 0)), reverse=True
    )
    for idx, op in enumerate(opinions, start=1):
        op["id"] = idx
    return opinions


def export_to_json() -> int:
    page_html = fetch_html(LIST_SOURCE_URL)
    opinions = parse_rows(page_html)

    for op in opinions:
        pdf_text = fetch_pdf_text(op.get("pdf_url") or "")
        merged_text = normalize_ws((op.get("summary") or "") + " " + pdf_text)
        op["text"] = merged_text[:PDF_TEXT_MAX_CHARS] if merged_text else (op.get("summary") or "")
        extracted = extract_authorities(
            merged_text,
            op.get("title") or "",
            op.get("citation") or "",
        )
        op["citations"] = extracted["citations"]
        op["authorities"] = extracted["authorities"]

    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(opinions, f, ensure_ascii=False, indent=2)

    print(f"[OK] Exported {len(opinions)} Supreme Court opinions to JSON")
    return len(opinions)


def create_searchable_html(count: int):
    with open(JSON_FILE, "r", encoding="utf-8") as jf:
        embedded_json = jf.read().replace("</script", "<\\/script")

    html_doc = (
        """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Atlas Law — U.S. Supreme Court Slip Opinions</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: "Times New Roman", Times, serif;
            background: #f5f7fa;
            min-height: 100vh;
            padding: 20px;
            color: #333;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border: 2px solid #003da5;
            padding: 30px;
            box-shadow: 0 2px 8px rgba(0,61,165,0.1);
        }
        h1 { color: #003da5; margin-bottom: 5px; text-align: center; font-size: 28px; }
        .stats { text-align: center; color: #0066cc; margin-bottom: 20px; font-size: 16px; }
        .update-controls { text-align: center; margin-bottom: 14px; }
        .update-btn { background: #0a6d3a; color: #fff; border: 1px solid #0a6d3a; padding: 8px 16px; font-size: 13px; font-weight: bold; cursor: pointer; }
        .update-btn:hover { background: #085b31; }
        .update-status { margin-left: 10px; font-size: 12px; color: #4a628b; }
        .theme-btn { margin-left: 8px; background: #1e293b; color: #fff; border: 1px solid #1e293b; padding: 8px 12px; font-size: 12px; font-weight: bold; cursor: pointer; }
        .theme-btn:hover { background: #0f172a; }
        .view-switch { display: flex; justify-content: center; gap: 8px; margin-bottom: 14px; }
        .view-link { border: 1px solid #0066cc; color: #0066cc; padding: 6px 10px; font-size: 13px; font-weight: bold; text-decoration: none; background: #fff; }
        .view-link.active { background: #0066cc; color: #fff; }
        .search-box { display: flex; gap: 10px; margin-bottom: 20px; }
        input[type="text"] { padding: 10px; border: 1px solid #0066cc; font-size: 14px; font-family: "Times New Roman", Times, serif; flex: 1; }
        button { padding: 10px 20px; background: #0066cc; color: white; border: none; cursor: pointer; font-weight: bold; font-family: "Times New Roman", Times, serif; font-size: 14px; }
        .layout { display: grid; grid-template-columns: 34% 66%; gap: 16px; min-height: 70vh; }
        .left-pane { border: 1px solid #c7d9f7; background: #fbfdff; overflow-y: auto; max-height: 74vh; }
        .right-pane { border: 1px solid #c7d9f7; padding: 14px; overflow-y: auto; max-height: 74vh; background: white; }
        .opinion-item { border-bottom: 1px solid #e7eefc; padding: 12px; cursor: pointer; }
        .opinion-item:hover { background: #eef4ff; }
        .opinion-item.active { background: #e3edff; border-left: 4px solid #0066cc; }
        .inline-detail-mobile { display: none; }
        .opinion-item-title { font-size: 15px; color: #003da5; font-weight: bold; margin-bottom: 6px; }
        .opinion-item-date { font-size: 12px; color: #4a628b; margin-bottom: 4px; font-weight: bold; }
        .opinion-item-preview { font-size: 12px; color: #555; line-height: 1.35; }
        .right-title { color: #003da5; font-size: 20px; margin-bottom: 8px; }
        .meta { font-size: 13px; margin-bottom: 8px; }
        .authority-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 14px; }
        .authority-box { border: 1px solid #d8e5fb; background: #f8fbff; padding: 10px; min-height: 90px; }
        .authority-head { font-size: 13px; color: #003da5; font-weight: bold; margin-bottom: 6px; }
        .authority-list { font-size: 12px; color: #333; line-height: 1.35; list-style: none; }
        .authority-list li { margin-bottom: 4px; }
        .source-panel { border: 1px solid #c7d9f7; background: #f5f7fa; padding: 16px; }
        .preview-text { color: #444; font-size: 14px; margin-bottom: 10px; line-height: 1.45; }
        .pdf-frame { width: 100%; height: 520px; border: 1px solid #c7d9f7; background: #f5f7fa; }
        .no-results, .empty-state { text-align: center; color: #666; padding: 30px; }
        a { color: #0066cc; text-decoration: none; font-size: 13px; }
        a:hover { text-decoration: underline; }

        body.night { background: #0b1220; color: #e5e7eb; }
        body.night .container { background: #111827; border-color: #1d4ed8; box-shadow: 0 2px 8px rgba(0,0,0,0.35); }
        body.night h1,
        body.night .right-title,
        body.night .authority-head,
        body.night .opinion-item-title { color: #93c5fd; }
        body.night .stats,
        body.night .opinion-item-date,
        body.night .update-status,
        body.night .preview-text,
        body.night .authority-list,
        body.night .empty-state,
        body.night .no-results,
        body.night .meta { color: #cbd5e1; }
        body.night .left-pane,
        body.night .right-pane,
        body.night .source-panel,
        body.night .authority-box { background: #0f172a; border-color: #334155; }
        body.night .opinion-item { border-bottom-color: #334155; }
        body.night .opinion-item:hover { background: #1e293b; }
        body.night .opinion-item.active { background: #1f2a44; border-left-color: #60a5fa; }
        body.night input[type="text"] { background: #0f172a; color: #e5e7eb; border-color: #3b82f6; }
        body.night a { color: #93c5fd; }
        @media (max-width: 1080px) {
            .layout { grid-template-columns: 1fr; }
            .right-pane { display: none; }
            .left-pane, .right-pane { max-height: none; }
            .inline-detail-mobile { display: block; margin-top: 10px; border-top: 1px solid #c7d9f7; padding-top: 10px; }
            .authority-grid { grid-template-columns: 1fr; }
            .inline-detail-mobile .pdf-frame { height: 360px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Atlas Law Opinions</h1>
        <div class="view-switch">
            <a class="view-link active" href="/supreme_opinions_index.html">U.S. Supreme Court</a>
            <a class="view-link" href="/opinions_index.html">Ninth Circuit</a>
            <a class="view-link" href="/central_opinions_index.html">Central District (C.D. Cal.)</a>
        </div>
        <div class="stats"><span id="total-count">"""
        + str(count)
        + """</span> opinions | Searchable</div>
        <div class="update-controls">
            <button id="update-all-btn" class="update-btn" onclick="runAtlasRefresh()">Update All Courts</button>
            <span id="update-status" class="update-status">Idle</span>
            <button id="theme-toggle-btn" class="theme-btn" onclick="toggleNightVision()">Night Vision: Off</button>
        </div>

        <div class="search-box">
            <input type="text" id="search-input" placeholder="Search by case name, docket, citation..." autofocus>
            <button onclick="search()">Search</button>
            <button onclick="clearSearch()">Clear</button>
        </div>

        <div class="layout">
            <div id="results" class="left-pane"></div>
            <div id="detail" class="right-pane"><div class="empty-state">Select an opinion from the left list.</div></div>
        </div>
    </div>

    <script>
        const EMBEDDED_OPINIONS = __EMBEDDED_OPINIONS__;
        let allOpinions = Array.isArray(EMBEDDED_OPINIONS) ? EMBEDDED_OPINIONS : [];
        let filteredOpinions = [];
        let selectedOpinionId = null;

        function applyTheme(theme) {
            const isNight = theme === 'night';
            document.body.classList.toggle('night', isNight);
            const btn = document.getElementById('theme-toggle-btn');
            if (btn) btn.textContent = isNight ? 'Night Vision: On' : 'Night Vision: Off';
        }

        function toggleNightVision() {
            const current = localStorage.getItem('atlas-supreme-theme') || 'day';
            const next = current === 'night' ? 'day' : 'night';
            localStorage.setItem('atlas-supreme-theme', next);
            applyTheme(next);
        }

        async function refreshUpdateStatus() {
            const el = document.getElementById('update-status');
            const btn = document.getElementById('update-all-btn');
            if (!el || !btn) return;
            try {
                const resp = await fetch('/api/refresh_status?t=' + Date.now(), { cache: 'no-store' });
                const data = await resp.json();
                if (data.running) {
                    el.textContent = 'Updating…';
                    btn.disabled = true;
                    btn.textContent = 'Updating…';
                } else {
                    const added = Number(data?.refresh_summary?.total_added || 0);
                    const warnings = Array.isArray(data?.refresh_summary?.warnings)
                        ? data.refresh_summary.warnings
                        : [];
                    const warningText = warnings.length ? ' (CACD unavailable)' : '';
                    if (data.exit_code === 0) {
                        el.textContent = `Last update: +${added} cases${warningText}`;
                    } else if (data.exit_code !== null && data.exit_code !== undefined) {
                        el.textContent = 'Last update failed';
                    } else {
                        el.textContent = 'Idle';
                    }
                    btn.disabled = false;
                    btn.textContent = 'Update All Courts';
                }
            } catch (_) {
                el.textContent = 'Status unavailable';
            }
        }

        async function runAtlasRefresh() {
            const el = document.getElementById('update-status');
            const btn = document.getElementById('update-all-btn');
            if (btn) {
                btn.disabled = true;
                btn.textContent = 'Starting…';
            }
            try {
                const resp = await fetch('/api/run_refresh', { method: 'POST' });
                const data = await resp.json();
                if (!resp.ok || !data.ok) {
                    if (el) el.textContent = data.message || 'Unable to start update';
                } else if (el) {
                    el.textContent = 'Updating…';
                }
            } catch (_) {
                if (el) el.textContent = 'Unable to start update';
            }
            refreshUpdateStatus();
        }

        function cleanText(value) {
            return String(value || '').replace(/\s+/g, ' ').trim();
        }

        function displayAll() {
            filteredOpinions = allOpinions;
            applyTheme(localStorage.getItem('atlas-supreme-theme') || 'day');
            refreshUpdateStatus();
            setInterval(refreshUpdateStatus, 15000);
            displayResults();
        }

        function search() {
            const query = cleanText(document.getElementById('search-input').value).toLowerCase();
            if (!query) return displayAll();
            filteredOpinions = allOpinions.filter(op => {
                const hay = [op.title, op.docket, op.citation, op.summary, op.issue_date].map(cleanText).join(' ').toLowerCase();
                return hay.includes(query);
            });
            displayResults();
        }

        function clearSearch() {
            document.getElementById('search-input').value = '';
            selectedOpinionId = null;
            displayAll();
        }

        function selectOpinion(id) {
            selectedOpinionId = id;
            displayResults();
        }

        function findOpinionById(opinionId) {
            if (opinionId === null || opinionId === undefined) return null;
            const target = String(opinionId);
            return filteredOpinions.find(op => String(op.id) === target) || allOpinions.find(op => String(op.id) === target) || null;
        }

        function escapeHtml(value) {
            return String(value || '')
                .replaceAll('&', '&amp;')
                .replaceAll('<', '&lt;')
                .replaceAll('>', '&gt;');
        }

        function caseLinkFor(value) {
            const citation = String(value || '').trim();
            if (!citation) return null;
            const params = new URLSearchParams({ citation });
            return `/api/resolve_case?${params.toString()}`;
        }

        function statuteLinkFor(value) {
            const text = String(value || '').replace(/(?:Ã‚)?Â§/g, '§').replace(/\s+/g, ' ').trim();
            const usc = text.match(/(\d+)\s*U\.?\s*S\.?\s*C\.?\s*§+\s*([0-9A-Za-z._-]+)/i);
            if (usc) return `https://www.law.cornell.edu/uscode/text/${usc[1]}/${usc[2]}`;
            return null;
        }

        function ruleLinkFor(value) {
            const text = String(value || '').replace(/\s+/g, ' ').trim();
            const frap = text.match(/Fed\.\s*R\.\s*App\.\s*P\.\s*(\d+)/i);
            if (frap) return `https://www.law.cornell.edu/rules/frap/rule_${frap[1]}`;
            const frcp = text.match(/Fed\.\s*R\.\s*Civ\.\s*P\.\s*(\d+)/i);
            if (frcp) return `https://www.law.cornell.edu/rules/frcp/rule_${frcp[1]}`;
            const frcrp = text.match(/Fed\.\s*R\.\s*Crim\.\s*P\.\s*(\d+)/i);
            if (frcrp) return `https://www.law.cornell.edu/rules/frcrmp/rule_${frcrp[1]}`;
            const fre = text.match(/Fed\.\s*R\.\s*Evid\.\s*(\d+)/i);
            if (fre) return `https://www.law.cornell.edu/rules/fre/rule_${fre[1]}`;
            const bare = text.match(/^Rule\s*(\d+)/i);
            if (bare) return `https://www.law.cornell.edu/rules/frcp/rule_${bare[1]}`;
            return null;
        }

        function regulationLinkFor(value) {
            const text = String(value || '').replace(/(?:Ã‚)?Â§/g, '§').replace(/\s+/g, ' ').trim();
            const cfr = text.match(/(\d+)\s*C\.?\s*F\.?\s*R\.?\s*§+\s*([0-9]+(?:\.[0-9A-Za-z-]+)*)/i);
            if (cfr) return `https://www.ecfr.gov/current/title-${cfr[1]}/section-${cfr[2]}`;

            const amendment = text.match(/^(First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|Eleventh|Twelfth|Thirteenth|Fourteenth|Fifteenth)\s+Amendment$/i);
            if (amendment) {
                const slugByName = {
                    first: 'first_amendment',
                    second: 'second_amendment',
                    third: 'third_amendment',
                    fourth: 'fourth_amendment',
                    fifth: 'fifth_amendment',
                    sixth: 'sixth_amendment',
                    seventh: 'seventh_amendment',
                    eighth: 'eighth_amendment',
                    ninth: 'ninth_amendment',
                    tenth: 'tenth_amendment',
                    eleventh: 'eleventh_amendment',
                    twelfth: 'twelfth_amendment',
                    thirteenth: 'thirteenth_amendment',
                    fourteenth: 'fourteenth_amendment',
                    fifteenth: 'fifteenth_amendment',
                };
                const key = amendment[1].toLowerCase();
                const slug = slugByName[key];
                if (slug) return `https://www.law.cornell.edu/constitution/${slug}`;
            }

            if (/U\.?\s*S\.?\s*Const\.?/i.test(text)) {
                return 'https://www.law.cornell.edu/constitution';
            }

            return null;
        }

        function listItems(items, category = '') {
            if (!items || !items.length) return '<div class="preview-text">None</div>';
            return `<ul class="authority-list">${items.slice(0, 20).map(v => {
                const text = String(v || '');
                const escaped = escapeHtml(text);
                let link = null;
                if (category === 'cases') link = caseLinkFor(text);
                if (category === 'statutes') link = statuteLinkFor(text);
                if (category === 'rules') link = ruleLinkFor(text);
                if (category === 'regulations') link = regulationLinkFor(text);
                if (link) return `<li><a href="${link}" target="_blank">${escaped}</a></li>`;
                return `<li>${escaped}</li>`;
            }).join('')}</ul>`;
        }

        function proxiedPdfUrl(url) {
            const value = String(url || '').trim();
            if (!value) return '';
            return `/api/pdf?url=${encodeURIComponent(value)}`;
        }

        function buildPdfEmbed(url, title = 'Supreme Court PDF') {
            const rawUrl = String(url || '').trim();
            if (!rawUrl) return '<div class="preview-text">PDF unavailable.</div>';
            const escapedRawUrl = escapeHtml(rawUrl);
            const escapedTitle = escapeHtml(title);
            return `<object class="pdf-frame" data="${escapedRawUrl}" type="application/pdf"><iframe class="pdf-frame" src="${escapedRawUrl}" title="${escapedTitle}"></iframe></object>
                    <div style="margin-top:8px;"><a href="${escapedRawUrl}" target="_blank">Open original PDF in new tab</a></div>`;
        }

        function buildDetail(op) {
            const opinionKey = String(op.id || '');
            const authorities = op.authorities || {};
            const citations = op.citations || {};
            const caseItems = (authorities.cases && authorities.cases.length) ? authorities.cases : (citations.cases || []);
            const statuteItems = (authorities.statutes && authorities.statutes.length) ? authorities.statutes : (citations.statutes || []);
            const ruleItems = (authorities.rules && authorities.rules.length) ? authorities.rules : (citations.rules || []);
            const regulationItems = (authorities.regulations && authorities.regulations.length) ? authorities.regulations : (citations.regulations || []);
            const constitutionalItems = authorities.constitutional || [];
            const pdf = buildPdfEmbed(proxiedPdfUrl(op.pdf_url), 'Supreme Court PDF');
            return `
                <div class="right-title">${escapeHtml(op.title || 'Untitled Opinion')}</div>
                <div class="meta"><strong>Issued:</strong> ${escapeHtml(op.issue_date || op.date || 'Unknown')}</div>
                <div class="meta"><strong>Docket:</strong> ${escapeHtml(op.docket || 'N/A')}</div>
                <div class="meta"><strong>Citation:</strong> ${escapeHtml(op.citation || 'N/A')}</div>
                <div class="meta"><strong>Author:</strong> ${escapeHtml(op.justice || 'N/A')}</div>
                <div class="preview-text">${escapeHtml(op.summary || 'No summary available.')}</div>
                <div class="authority-grid">
                    <div class="authority-box">
                        <div class="authority-head">Cases</div>
                        ${listItems(caseItems, 'cases')}
                    </div>
                    <div class="authority-box">
                        <div class="authority-head">Statutes</div>
                        ${listItems(statuteItems, 'statutes')}
                    </div>
                    <div class="authority-box">
                        <div class="authority-head">Rules</div>
                        ${listItems(ruleItems, 'rules')}
                    </div>
                    <div class="authority-box">
                        <div class="authority-head">Regulations / Constitutional</div>
                        ${listItems([...(regulationItems || []), ...(constitutionalItems || [])], 'regulations')}
                    </div>
                </div>
                <div class="authority-head" style="margin-bottom:8px;">Original PDF</div>
                <div class="source-panel">
                    <div class="meta"><a href="${escapeHtml(op.pdf_url)}" target="_blank">Open original Supreme Court PDF</a></div>
                    ${pdf}
                </div>
            `;
        }

        function displayResults() {
            const resultsDiv = document.getElementById('results');
            const detailDiv = document.getElementById('detail');

            if (!filteredOpinions.length) {
                resultsDiv.innerHTML = '<div class="no-results">No opinions found.</div>';
                detailDiv.innerHTML = '<div class="empty-state">No opinion selected.</div>';
                return;
            }

            const mobileView = window.matchMedia('(max-width: 1080px)').matches;

            resultsDiv.innerHTML = filteredOpinions.map(op => {
                const active = selectedOpinionId === op.id ? 'active' : '';
                const inlineDetail = mobileView && selectedOpinionId === op.id ? `<div class="inline-detail-mobile">${buildDetail(op)}</div>` : '';
                return `
                    <div class="opinion-item ${active}" onclick="selectOpinion(${op.id})">
                        <div class="opinion-item-title">${op.title || 'Untitled Opinion'}</div>
                        <div class="opinion-item-date">Issued: ${op.issue_date || op.date || 'Unknown'} | Docket: ${op.docket || 'N/A'}</div>
                        <div class="opinion-item-preview">${(op.summary || '').substring(0, 190)}${(op.summary || '').length > 190 ? '...' : ''}</div>
                        ${inlineDetail}
                    </div>
                `;
            }).join('');

            const selected = filteredOpinions.find(op => op.id === selectedOpinionId) || filteredOpinions[0];
            selectedOpinionId = selected.id;
            detailDiv.innerHTML = buildDetail(selected);
        }

        document.getElementById('search-input').addEventListener('keypress', e => {
            if (e.key === 'Enter') search();
        });

        displayAll();
    </script>
</body>
</html>
"""
    )

    html_doc = html_doc.replace("__EMBEDDED_OPINIONS__", embedded_json)
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html_doc)

    print(f"[OK] Created: {HTML_FILE}")


if __name__ == "__main__":
    print("Building U.S. Supreme Court index...\n")
    count = export_to_json()
    create_searchable_html(count)
    if os.getenv("ATLAS_NO_BROWSER", "0") == "1":
        print("\n[OK] Complete! Browser launch skipped (ATLAS_NO_BROWSER=1)")
    else:
        print("\n[OK] Complete! Opening http://127.0.0.1:8080/supreme_opinions_index.html")
        webbrowser.open("http://127.0.0.1:8080/supreme_opinions_index.html")
