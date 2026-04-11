"""
Knesset Committee Monitor — Transportation & Energy
Fetches upcoming committee sessions, filters by topic using Gemini,
generates an HTML dashboard and sends an email report.
"""

import argparse
import html as _html
import json
import logging
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from google import genai
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASS")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")
GITHUB_PAGES_URL = os.getenv("GITHUB_PAGES_URL", "")

GEMINI_MODEL = "models/gemini-2.5-flash"
BATCH_SIZE = 150          # max sessions per Gemini call to stay within token limits
MAX_GEMINI_RETRIES = 3

HEBREW_DAYS = ["יום שני", "יום שלישי", "יום רביעי", "יום חמישי", "יום שישי", "יום שבת", "יום ראשון"]

KNESSET_API = (
    "https://knesset.gov.il/Odata/ParliamentInfo.svc/KNS_CommitteeSession"
    "?$filter=KnessetNum eq 25"
    " and StartDate ge datetime'{start}'"
    " and StartDate le datetime'{end}'"
    "&$expand=KNS_Committee,KNS_CmtSessionItems"
    "&$orderby=StartDate asc"
    "&$format=json"
)

KNESSET_SESSION_URL = (
    "https://main.knesset.gov.il/Activity/committees/Pages/"
    "AllCommitteesAgenda.aspx?Tab=3&ItemID={session_id}"
)

ANALYSIS_PROMPT = """\
אתה אנליסט מומחה לניתוח מידע פרלמנטרי וממשלתי.
תפקידך לסרוק את כלל הדיונים העתידיים בוועדות הכנסת בלבד ולבצע סינון קפדני.

זיהוי תוכן: הצג אך ורק דיונים הקשורים באופן ישיר או עקיף לתחומי:
- mobility: יבואני רכב, מותגים, כלי רכב, משאיות, אוטובוסים, תחבורה ציבורית, נהיגה אוטונומית,
  רכב חשמלי, נהגים, כבישים, בטיחות בדרכים, רכבת, חלקי חילוף, השכרה, ליסינג וכו'
- אנרגיה: משק החשמל, גז טבעי, אנרגיות מתחדשות, אגירה, סוללות,
  מחירי החשמל, חיפושי נפט, תשתיות אנרגיה, פאנלים סולאריים

החזר תשובה ב-JSON בלבד, ללא טקסט נוסף, במבנה הבא:
{{
  "relevant_sessions": [
    {{
      "title": "כותרת הדיון",
      "committee": "שם הוועדה",
      "datetime": "DD/MM/YYYY HH:MM",
      "link": "הקישור המלא",
      "category": "תחבורה" או "אנרגיה",
      "relevance": "משפט קצר מדוע זה רלוונטי"
    }}
  ],
  "total_scanned": 123,
  "has_results": true
}}

אם אין דיונים רלוונטיים, החזר רשימה ריקה עם has_results: false.

להלן רשימת הדיונים:
{sessions_json}
"""


# ---------------------------------------------------------------------------
# HTTP session with retry
# ---------------------------------------------------------------------------

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "max-age=0",
    "Referer": "https://main.knesset.gov.il/",
    "Origin": "https://main.knesset.gov.il",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


def _make_http_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(BROWSER_HEADERS)
    return session


# ---------------------------------------------------------------------------
# 1. Fetch data from Knesset API
# ---------------------------------------------------------------------------

def fetch_sessions() -> list[dict]:
    today = datetime.now()
    end = today + timedelta(days=30)

    url = KNESSET_API.format(
        start=today.strftime("%Y-%m-%dT00:00:00"),
        end=end.strftime("%Y-%m-%dT23:59:59"),
    )

    log.info("Fetching sessions from Knesset API…")
    log.info("Request URL: %s", url)

    http = _make_http_session()
    resp = None
    for attempt in range(2):
        try:
            resp = http.get(url, timeout=30)
            log.info("HTTP %d from Knesset API (attempt %d)", resp.status_code, attempt + 1)
            if not resp.ok:
                log.error("API error %d — response: %s", resp.status_code, resp.text[:200])
                resp.raise_for_status()
            body = resp.text.strip()
            if not body or body.lower().startswith("<!doctype html"):
                if attempt == 0:
                    log.error("Received HTML/Empty instead of JSON (attempt %d): %s", attempt + 1, body[:200])
                    time.sleep(2)
                    continue
                log.critical("CRITICAL: Still blocked by Knesset WAF. Need to switch to a different endpoint.")
                log.error("Blocked response body: %s", body[:200])
                return []
            break
        except requests.RequestException as exc:
            log.error("API request failed (attempt %d): %s", attempt + 1, exc)
            if attempt == 0:
                time.sleep(2)
                continue
            return []
    else:
        return []

    try:
        data = resp.json()
    except ValueError as exc:
        log.error("JSON parse error: %s — body: %s", exc, resp.text[:200])
        return []

    raw = data.get("value", [])
    sessions = []
    for item in raw:
        session_id     = item.get("CommitteeSessionID")
        committee_obj  = item.get("KNS_Committee") or {}
        committee_name = committee_obj.get("Name", "")
        agenda_items   = item.get("KNS_CmtSessionItems") or []
        agenda_names   = [i.get("Name", "") for i in agenda_items if i.get("Name")]
        title          = " ; ".join(agenda_names) if agenda_names else (item.get("Name") or "ישיבת ועדה")
        start_raw      = item.get("StartDate", "")

        dt_str = ""
        dt = None
        if start_raw:
            if "/Date(" in start_raw:
                try:
                    ts_ms = int(start_raw.split("(")[1].split(")")[0].split("+")[0].split("-")[0])
                    dt = datetime.utcfromtimestamp(ts_ms / 1000)
                except (ValueError, IndexError):
                    pass
            else:
                try:
                    dt = datetime.fromisoformat(start_raw)
                except ValueError:
                    pass
        if dt:
            dt_str = f"{HEBREW_DAYS[dt.weekday()]}, {dt.strftime('%d/%m/%Y %H:%M')}"
        else:
            dt_str = start_raw

        try:
            link = KNESSET_SESSION_URL.format(session_id=int(session_id)) if session_id else "#"
        except (ValueError, TypeError):
            link = "#"

        sessions.append({
            "title": title,
            "committee": committee_name,
            "datetime": dt_str,
            "link": link,
            "session_id": session_id,
        })

    log.info("Fetched %d sessions.", len(sessions))
    return sessions


# ---------------------------------------------------------------------------
# 2. Analyse with Gemini
# ---------------------------------------------------------------------------

def analyse_with_gemini(sessions: list[dict]) -> dict:
    if not sessions:
        return {"relevant_sessions": [], "total_scanned": 0, "has_results": False}

    client = genai.Client(api_key=GEMINI_API_KEY)
    all_relevant: list[dict] = []

    chunks = [sessions[i:i + BATCH_SIZE] for i in range(0, len(sessions), BATCH_SIZE)]
    if len(chunks) > 1:
        log.info("Splitting %d sessions into %d chunks of up to %d", len(sessions), len(chunks), BATCH_SIZE)

    for chunk in chunks:
        chunk_result = _analyse_chunk(client, chunk)
        all_relevant.extend(chunk_result.get("relevant_sessions", []))

    return {
        "relevant_sessions": all_relevant,
        "total_scanned": len(sessions),
        "has_results": bool(all_relevant),
    }


def _analyse_chunk(client: genai.Client, sessions: list[dict]) -> dict:
    sessions_for_prompt = [
        {k: v for k, v in s.items() if k != "session_id"}
        for s in sessions
    ]
    prompt = ANALYSIS_PROMPT.format(
        sessions_json=json.dumps(sessions_for_prompt, ensure_ascii=False, indent=2)
    )

    log.info("Sending %d sessions to Gemini for analysis…", len(sessions))

    raw_text = ""
    for attempt in range(MAX_GEMINI_RETRIES):
        try:
            response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
            raw_text = response.text.strip()
            break
        except Exception as exc:
            log.warning("Gemini attempt %d/%d failed: %s", attempt + 1, MAX_GEMINI_RETRIES, exc)
            if attempt < MAX_GEMINI_RETRIES - 1:
                time.sleep(2 ** attempt)
    else:
        log.error("Gemini API failed after %d attempts. Falling back to showing all sessions.", MAX_GEMINI_RETRIES)
        return {
            "relevant_sessions": [
                {k: v for k, v in s.items() if k != "session_id"} | {"category": "תחבורה", "relevance": "Pending AI Analysis"}
                for s in sessions
            ],
            "total_scanned": len(sessions),
            "has_results": True,
        }

    # Strip markdown code fences if present
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.IGNORECASE)
    raw_text = re.sub(r"\s*```$", "", raw_text).strip()

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        log.error("JSON parse error: %s\nRaw:\n%.500s", exc, raw_text)
        return {"relevant_sessions": [], "total_scanned": len(sessions), "has_results": False}

    result.setdefault("total_scanned", len(sessions))
    result.setdefault("has_results", bool(result.get("relevant_sessions")))
    return result


# ---------------------------------------------------------------------------
# 3. Generate HTML Dashboard
# ---------------------------------------------------------------------------

def generate_dashboard(results: dict, generated_at: datetime) -> str:
    sessions = results.get("relevant_sessions", [])
    total_relevant = len(sessions)
    total_scanned = results.get("total_scanned", 0)
    date_str = generated_at.strftime("%d/%m/%Y %H:%M")

    # KPI stats
    today_str = generated_at.strftime("%d/%m/%Y")
    count_transport = sum(1 for s in sessions if s.get("category") == "תחבורה")
    count_energy    = sum(1 for s in sessions if s.get("category") == "אנרגיה")
    sessions_today  = sum(1 for s in sessions if today_str in s.get("datetime", ""))

    # Derive GitHub Actions URL from GITHUB_PAGES_URL
    gh_actions_url = "#"
    if GITHUB_PAGES_URL and "github.io" in GITHUB_PAGES_URL:
        try:
            cleaned  = GITHUB_PAGES_URL.rstrip("/").replace("https://", "").replace("http://", "")
            parts    = cleaned.split("/")
            username = parts[0].replace(".github.io", "")
            repo     = parts[1] if len(parts) > 1 else ""
            if username and repo:
                gh_actions_url = f"https://github.com/{username}/{repo}/actions"
        except Exception:
            pass

    # Embed sessions as JSON for the modal
    sessions_js = json.dumps(sessions, ensure_ascii=False).replace("</script>", "<\\/script>")

    rows_html = ""
    if sessions:
        for idx, s in enumerate(sessions):
            raw_cat   = s.get("category", "")
            cat       = _html.escape(raw_cat)
            committee = _html.escape(s.get("committee", ""))
            title     = _html.escape(s.get("title", ""))
            dt        = _html.escape(s.get("datetime", ""))
            cat_cls   = "badge-transport" if raw_cat == "תחבורה" else ("badge-energy" if raw_cat == "אנרגיה" else "badge-default")
            search_str = _html.escape(f"{s.get('title','')} {s.get('committee','')} {raw_cat}")
            link      = _html.escape(s.get("link") or "#")
            rows_html += f"""
        <tr class="session-row" data-idx="{idx}" data-cat="{cat}" data-search="{search_str}">
          <td class="td-title-cell"><span class="td-title">{title}</span></td>
          <td><span class="badge badge-committee">{committee}</span></td>
          <td class="td-date">{dt}</td>
          <td><span class="badge {cat_cls}">{cat if cat else '—'}</span></td>
          <td class="td-action">
            <a href="{link}" target="_blank" rel="noopener" class="btn-open" onclick="event.stopPropagation()">פתח ←</a>
          </td>
        </tr>"""
    else:
        rows_html = """
        <tr><td colspan="5" class="no-results">
          <div class="no-results-icon">📋</div>
          <p>אין דיונים רלוונטיים ב-30 הימים הקרובים</p>
        </td></tr>"""

    dashboard_html = f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>מוניטור ועדות הכנסת</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Heebo:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg:        #F1F5F9;
      --surface:   #FFFFFF;
      --border:    #E2E8F0;
      --border-lt: #F1F5F9;
      --txt:       #0F172A;
      --txt-2:     #475569;
      --txt-3:     #94A3B8;
      --blue:      #2563EB;
      --blue-dk:   #1D4ED8;
      --blue-lt:   #EFF6FF;
      --emerald:   #059669;
      --emerald-lt:#ECFDF5;
      --amber:     #D97706;
      --amber-lt:  #FFFBEB;
      --hdr:       #0F172A;
      --radius:    12px;
      --shadow:    0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.05);
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Heebo', sans-serif;
      background: var(--bg);
      color: var(--txt);
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
    }}

    /* ─── HEADER ─── */
    .hdr {{
      background: var(--hdr);
      height: 60px;
      padding: 0 28px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 200;
      border-bottom: 1px solid rgba(255,255,255,.06);
    }}
    .hdr-brand {{ display: flex; align-items: center; gap: 10px; }}
    .hdr-logo {{
      width: 34px; height: 34px;
      background: var(--blue);
      border-radius: 8px;
      display: grid; place-items: center;
      font-size: 1rem; flex-shrink: 0;
    }}
    .hdr-name {{ font-size: .98rem; font-weight: 700; color: #F1F5F9; }}
    .hdr-sub  {{ font-size: .7rem;  color: #64748B; margin-top: 1px; }}
    .hdr-right {{ display: flex; align-items: center; gap: 14px; }}
    .hdr-ts {{
      font-size: .72rem; color: #64748B;
      display: flex; flex-direction: column; align-items: flex-end;
    }}
    .hdr-ts span {{ color: #94A3B8; font-size: .8rem; font-weight: 500; }}
    .btn-refresh {{
      display: inline-flex; align-items: center; gap: 6px;
      background: rgba(37,99,235,.15);
      border: 1px solid rgba(37,99,235,.4);
      color: #93C5FD;
      border-radius: 8px;
      padding: 7px 14px;
      font-size: .8rem; font-weight: 600;
      font-family: 'Heebo', sans-serif;
      text-decoration: none;
      transition: background .15s, border-color .15s;
      white-space: nowrap;
    }}
    .btn-refresh:hover {{ background: rgba(37,99,235,.28); border-color: #3B82F6; color: #BFDBFE; }}

    /* ─── HERO KPI STRIP ─── */
    .hero {{
      background: var(--hdr);
      border-bottom: 1px solid rgba(255,255,255,.06);
      padding: 20px 28px 24px;
    }}
    .hero-grid {{
      max-width: 1200px; margin: 0 auto;
      display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px;
    }}
    .kpi {{
      background: rgba(255,255,255,.04);
      border: 1px solid rgba(255,255,255,.08);
      border-radius: var(--radius);
      padding: 20px 22px;
      display: flex; align-items: center; gap: 16px;
      position: relative; overflow: hidden;
    }}
    .kpi::before {{
      content: '';
      position: absolute; top: 0; right: 0;
      width: 4px; height: 100%;
      border-radius: 0 var(--radius) var(--radius) 0;
    }}
    .kpi-total::before  {{ background: var(--blue); }}
    .kpi-transp::before {{ background: var(--emerald); }}
    .kpi-energy::before {{ background: var(--amber); }}
    .kpi-ico {{
      width: 44px; height: 44px; border-radius: 10px;
      display: grid; place-items: center; font-size: 1.25rem; flex-shrink: 0;
    }}
    .kpi-total  .kpi-ico {{ background: rgba(37,99,235,.18); }}
    .kpi-transp .kpi-ico {{ background: rgba(5,150,105,.18); }}
    .kpi-energy .kpi-ico {{ background: rgba(217,119,6,.18); }}
    .kpi-val  {{ font-size: 2.4rem; font-weight: 900; line-height: 1; color: #F1F5F9; }}
    .kpi-lbl  {{ font-size: .75rem; color: #94A3B8; margin-top: 4px; font-weight: 500; }}
    .kpi-hint {{ font-size: .68rem; color: #64748B; margin-top: 3px; }}

    /* ─── MAIN ─── */
    .main {{ max-width: 1200px; margin: 0 auto; padding: 24px 20px 56px; }}

    /* ─── PANEL ─── */
    .panel {{
      background: var(--surface);
      border-radius: 14px;
      border: 1px solid var(--border);
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .toolbar {{
      padding: 16px 22px;
      border-bottom: 1px solid var(--border-lt);
      display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    }}
    .toolbar-title {{
      font-size: .9rem; font-weight: 700; color: var(--txt);
      flex: 1; min-width: 100px;
    }}
    .toolbar-title small {{ display: block; font-size: .72rem; color: var(--txt-3); font-weight: 400; margin-top: 1px; }}

    /* Search */
    .search-wrap {{ position: relative; flex: 1; max-width: 260px; }}
    .search-ico {{
      position: absolute; right: 10px; top: 50%;
      transform: translateY(-50%); font-size: .85rem; pointer-events: none;
    }}
    .search-inp {{
      width: 100%; padding: 8px 34px 8px 12px;
      border: 1px solid var(--border); border-radius: 8px;
      font-family: 'Heebo', sans-serif; font-size: .85rem;
      color: var(--txt); background: var(--bg);
      outline: none; transition: border-color .15s, box-shadow .15s;
      direction: rtl;
    }}
    .search-inp:focus {{ border-color: var(--blue); box-shadow: 0 0 0 3px rgba(37,99,235,.1); background: #fff; }}
    .search-inp::placeholder {{ color: var(--txt-3); }}

    /* Filter pills */
    .filters {{ display: flex; gap: 6px; flex-wrap: wrap; }}
    .fpill {{
      border: 1px solid var(--border); border-radius: 20px;
      padding: 5px 14px; font-size: .78rem; font-weight: 500;
      cursor: pointer; font-family: 'Heebo', sans-serif;
      background: var(--surface); color: var(--txt-2);
      transition: all .15s; white-space: nowrap;
      display: inline-flex; align-items: center; gap: 5px;
    }}
    .fpill .fc {{ font-size: .7rem; background: var(--border-lt); border-radius: 20px; padding: 0 6px; }}
    .fpill:hover {{ border-color: var(--blue); color: var(--blue); }}
    .fpill.active {{ background: var(--blue); border-color: var(--blue); color: #fff; font-weight: 600; }}
    .fpill.active .fc {{ background: rgba(255,255,255,.2); color: #fff; }}
    .fpill[data-filter="תחבורה"].active {{ background: var(--emerald); border-color: var(--emerald); }}
    .fpill[data-filter="אנרגיה"].active  {{ background: var(--amber);   border-color: var(--amber);   }}

    /* Table */
    .tbl-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 580px; }}
    thead tr {{ background: #FAFBFC; }}
    th {{
      padding: 11px 20px; font-size: .7rem; font-weight: 700;
      color: var(--txt-3); text-align: right;
      border-bottom: 1px solid var(--border);
      white-space: nowrap; letter-spacing: .04em; text-transform: uppercase;
    }}
    td {{ padding: 15px 20px; border-bottom: 1px solid var(--border-lt); vertical-align: middle; }}
    .session-row {{ cursor: pointer; transition: background .1s; }}
    .session-row:last-child td {{ border-bottom: none; }}
    .session-row:hover td {{ background: #F8FAFF; }}
    .session-row:active td {{ background: #EFF6FF; }}

    .td-title-cell {{ max-width: 400px; }}
    .td-title {{
      font-size: .9rem; font-weight: 600; color: var(--txt); line-height: 1.5;
      display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
    }}
    .td-date {{ font-size: .82rem; color: var(--txt-2); white-space: nowrap; font-weight: 500; }}
    .td-action {{ text-align: center; white-space: nowrap; }}

    /* Badges */
    .badge {{
      display: inline-block; border-radius: 20px;
      padding: 3px 11px; font-size: .72rem; font-weight: 600; white-space: nowrap;
    }}
    .badge-committee {{ background: var(--border-lt); color: var(--txt-2); }}
    .badge-transport  {{ background: var(--emerald-lt); color: var(--emerald); }}
    .badge-energy     {{ background: var(--amber-lt);   color: var(--amber);   }}
    .badge-default    {{ background: var(--border-lt);  color: var(--txt-3);   }}

    /* Open button */
    .btn-open {{
      display: inline-flex; align-items: center;
      background: var(--blue); color: #fff; border-radius: 7px;
      padding: 6px 14px; font-size: .78rem; font-weight: 600;
      font-family: 'Heebo', sans-serif; text-decoration: none;
      transition: background .15s, box-shadow .15s; white-space: nowrap;
    }}
    .btn-open:hover {{ background: var(--blue-dk); box-shadow: 0 4px 12px rgba(37,99,235,.3); }}

    /* No results */
    .no-results {{ text-align: center; padding: 64px 24px !important; color: var(--txt-3); }}
    .no-results-icon {{ font-size: 2.2rem; margin-bottom: 12px; }}
    .no-results p {{ font-size: .95rem; }}

    /* ─── MODAL ─── */
    .modal-overlay {{
      display: none; position: fixed; inset: 0;
      background: rgba(0,0,0,.45); backdrop-filter: blur(3px);
      z-index: 500; align-items: center; justify-content: center; padding: 16px;
    }}
    .modal-overlay.open {{ display: flex; }}
    .modal {{
      background: var(--surface); border-radius: 18px;
      width: 100%; max-width: 540px;
      max-height: 90vh; overflow-y: auto;
      box-shadow: 0 24px 64px rgba(0,0,0,.25);
      animation: modalIn .22s cubic-bezier(.34,1.3,.64,1);
      position: relative;
    }}
    @keyframes modalIn {{
      from {{ opacity:0; transform: translateY(18px) scale(.97); }}
      to   {{ opacity:1; transform: translateY(0)    scale(1);   }}
    }}
    .modal-head {{
      padding: 22px 24px 18px;
      border-bottom: 1px solid var(--border-lt);
      display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
    }}
    .modal-close {{
      background: var(--bg); border: none; border-radius: 8px;
      width: 32px; height: 32px; cursor: pointer;
      font-size: 1rem; color: var(--txt-3); flex-shrink: 0;
      display: grid; place-items: center; transition: background .15s;
    }}
    .modal-close:hover {{ background: var(--border); color: var(--txt); }}
    .modal-body {{ padding: 22px 24px 26px; }}
    .modal-title {{
      font-size: 1.05rem; font-weight: 700; color: var(--txt);
      line-height: 1.5; margin-bottom: 18px;
    }}
    .modal-meta {{
      display: flex; flex-direction: column; gap: 10px; margin-bottom: 18px;
    }}
    .modal-meta-row {{
      display: flex; align-items: center; gap: 10px;
      font-size: .85rem; color: var(--txt-2);
    }}
    .modal-meta-ico {{ font-size: 1rem; }}
    .modal-relevance {{
      background: var(--bg); border-radius: 10px; padding: 14px 16px;
      font-size: .85rem; color: var(--txt-2); line-height: 1.6;
      margin-bottom: 20px; font-style: italic;
    }}
    .btn-modal-open {{
      display: flex; align-items: center; justify-content: center; gap: 6px;
      background: var(--blue); color: #fff; border-radius: 10px;
      padding: 12px 20px; font-size: .9rem; font-weight: 700;
      font-family: 'Heebo', sans-serif; text-decoration: none;
      transition: background .15s, box-shadow .15s;
    }}
    .btn-modal-open:hover {{ background: var(--blue-dk); box-shadow: 0 6px 18px rgba(37,99,235,.35); }}

    /* ─── FOOTER ─── */
    .footer {{
      text-align: center; padding: 20px 24px 16px;
      color: var(--txt-3); font-size: .72rem;
    }}
    .sysinfo {{
      display: inline-flex; align-items: center; gap: 18px;
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 10px; padding: 8px 18px;
      font-size: .72rem; color: var(--txt-2); flex-wrap: wrap; justify-content: center;
    }}
    .sysinfo-item {{ display: flex; align-items: center; gap: 5px; white-space: nowrap; }}
    .sysinfo-dot {{ width: 6px; height: 6px; border-radius: 50%; background: var(--emerald); }}

    /* ─── RESPONSIVE ─── */
    @media (max-width: 768px) {{
      .hdr {{ padding: 0 16px; }}
      .hdr-sub, .hdr-ts .label {{ display: none; }}
      .hero {{ padding: 14px 16px 18px; }}
      .hero-grid {{ gap: 10px; }}
      .kpi {{ padding: 14px 14px; }}
      .kpi-val {{ font-size: 1.9rem; }}
      .main {{ padding: 16px 12px 48px; }}
      .toolbar {{ flex-direction: column; align-items: stretch; }}
      .search-wrap {{ max-width: 100%; }}
      th, td {{ padding: 12px 14px; }}
      .td-title-cell {{ max-width: 220px; }}
      .modal {{ border-radius: 14px; }}
    }}
    @media (max-width: 480px) {{
      .kpi-hint {{ display: none; }}
      .kpi {{ gap: 10px; }}
      .kpi-ico {{ width: 36px; height: 36px; font-size: 1rem; }}
      .kpi-val {{ font-size: 1.6rem; }}
      .btn-refresh {{ padding: 6px 10px; font-size: .75rem; }}
    }}
  </style>
</head>
<body>

  <!-- ═══ HEADER ═══ -->
  <header class="hdr">
    <div class="hdr-brand">
      <div class="hdr-logo">🏛️</div>
      <div>
        <div class="hdr-name">מוניטור ועדות הכנסת</div>
        <div class="hdr-sub">תחבורה ואנרגיה · 30 הימים הקרובים</div>
      </div>
    </div>
    <div class="hdr-right">
      <div class="hdr-ts">
        <span class="label" style="font-size:.68rem;color:#64748B;">עודכן לאחרונה</span>
        <span>{date_str}</span>
      </div>
      <a href="{gh_actions_url}" target="_blank" rel="noopener" class="btn-refresh">
        🔄 רענן נתונים
      </a>
    </div>
  </header>

  <!-- ═══ HERO KPIs ═══ -->
  <section class="hero">
    <div class="hero-grid">
      <div class="kpi kpi-total">
        <div class="kpi-ico">📋</div>
        <div>
          <div class="kpi-val">{total_relevant}</div>
          <div class="kpi-lbl">סה״כ דיונים</div>
          <div class="kpi-hint">30 הימים הקרובים</div>
        </div>
      </div>
      <div class="kpi kpi-transp">
        <div class="kpi-ico">🚗</div>
        <div>
          <div class="kpi-val">{count_transport}</div>
          <div class="kpi-lbl">דיוני תחבורה</div>
          <div class="kpi-hint">תחבורה ציבורית, רכב, כבישים</div>
        </div>
      </div>
      <div class="kpi kpi-energy">
        <div class="kpi-ico">⚡</div>
        <div>
          <div class="kpi-val">{count_energy}</div>
          <div class="kpi-lbl">דיוני אנרגיה</div>
          <div class="kpi-hint">חשמל, גז, אנרגיות מתחדשות</div>
        </div>
      </div>
    </div>
  </section>

  <!-- ═══ MAIN TABLE ═══ -->
  <main class="main">
    <div class="panel">
      <div class="toolbar">
        <div class="toolbar-title">
          לוח דיונים
          <small>לחץ על שורה לפרטים נוספים</small>
        </div>
        <div class="search-wrap">
          <span class="search-ico">🔍</span>
          <input class="search-inp" id="searchInput" type="text" placeholder="חיפוש לפי נושא, ועדה...">
        </div>
        <div class="filters">
          <button class="fpill active" data-filter="all">הכל <span class="fc" id="cnt-all">{total_relevant}</span></button>
          <button class="fpill" data-filter="תחבורה">🚗 תחבורה <span class="fc" id="cnt-transp">{count_transport}</span></button>
          <button class="fpill" data-filter="אנרגיה">⚡ אנרגיה <span class="fc" id="cnt-energy">{count_energy}</span></button>
        </div>
      </div>

      <div class="tbl-wrap">
        <table>
          <thead>
            <tr>
              <th>נושא הדיון</th>
              <th>ועדה</th>
              <th>תאריך ושעה</th>
              <th>תחום</th>
              <th></th>
            </tr>
          </thead>
          <tbody id="tbody">
            {rows_html}
          </tbody>
        </table>
      </div>
    </div>
  </main>

  <!-- ═══ DETAIL MODAL ═══ -->
  <div class="modal-overlay" id="overlay" onclick="closeModal()">
    <div class="modal" onclick="event.stopPropagation()">
      <div class="modal-head">
        <div id="m-badge"></div>
        <button class="modal-close" onclick="closeModal()">✕</button>
      </div>
      <div class="modal-body">
        <div class="modal-title" id="m-title"></div>
        <div class="modal-meta">
          <div class="modal-meta-row"><span class="modal-meta-ico">🏛️</span><span id="m-committee"></span></div>
          <div class="modal-meta-row"><span class="modal-meta-ico">🗓️</span><span id="m-date"></span></div>
        </div>
        <div class="modal-relevance" id="m-relevance" style="display:none"></div>
        <a class="btn-modal-open" id="m-link" target="_blank" rel="noopener">פתח בכנסת ←</a>
      </div>
    </div>
  </div>

  <footer class="footer">
    <div class="sysinfo">
      <div class="sysinfo-item">
        <span class="sysinfo-dot"></span>
        <span>סריקה אחרונה: <strong>{date_str}</strong></span>
      </div>
      <div class="sysinfo-item">🔍 דיונים שנסרקו: <strong>{total_scanned}</strong></div>
      <div class="sysinfo-item">✅ דיונים רלוונטיים: <strong>{total_relevant}</strong></div>
    </div>
  </footer>

  <script>
    const SESSIONS = {sessions_js};
    let activeFilter = 'all';
    let searchTerm   = '';
    const rows = Array.from(document.querySelectorAll('.session-row'));

    function applyFilters() {{
      let visAll = 0, visTr = 0, visEn = 0;
      rows.forEach(r => {{
        const matchCat    = activeFilter === 'all' || r.dataset.cat === activeFilter;
        const matchSearch = !searchTerm || r.dataset.search.includes(searchTerm);
        const show = matchCat && matchSearch;
        r.style.display = show ? '' : 'none';
        if (show) {{
          visAll++;
          if (r.dataset.cat === 'תחבורה') visTr++;
          if (r.dataset.cat === 'אנרגיה')  visEn++;
        }}
      }});
      document.getElementById('cnt-all').textContent    = visAll;
      document.getElementById('cnt-transp').textContent = visTr;
      document.getElementById('cnt-energy').textContent = visEn;
    }}

    document.querySelectorAll('.fpill').forEach(btn => {{
      btn.addEventListener('click', () => {{
        document.querySelectorAll('.fpill').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        activeFilter = btn.dataset.filter;
        applyFilters();
      }});
    }});

    document.getElementById('searchInput').addEventListener('input', e => {{
      searchTerm = e.target.value.trim();
      applyFilters();
    }});

    rows.forEach(row => {{
      row.addEventListener('click', () => openModal(parseInt(row.dataset.idx)));
    }});

    function openModal(idx) {{
      const s = SESSIONS[idx];
      if (!s) return;
      const cat = s.category || '';
      let badgeHtml = '';
      if (cat === 'תחבורה') badgeHtml = '<span class="badge badge-transport">🚗 ' + cat + '</span>';
      else if (cat === 'אנרגיה') badgeHtml = '<span class="badge badge-energy">⚡ ' + cat + '</span>';
      document.getElementById('m-badge').innerHTML     = badgeHtml;
      document.getElementById('m-title').textContent   = s.title || '';
      document.getElementById('m-committee').textContent = s.committee || '';
      document.getElementById('m-date').textContent    = s.datetime || '';
      document.getElementById('m-link').href           = s.link || '#';
      const relEl = document.getElementById('m-relevance');
      if (s.relevance && s.relevance !== 'Pending AI Analysis') {{
        relEl.textContent = s.relevance;
        relEl.style.display = 'block';
      }} else {{
        relEl.style.display = 'none';
      }}
      document.getElementById('overlay').classList.add('open');
      document.body.style.overflow = 'hidden';
    }}

    function closeModal() {{
      document.getElementById('overlay').classList.remove('open');
      document.body.style.overflow = '';
    }}

    document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeModal(); }});
  </script>

</body>
</html>"""
    return dashboard_html


def write_dashboard(results: dict, generated_at: datetime) -> None:
    dashboard_html = generate_dashboard(results, generated_at)
    docs_dir = Path("docs")
    docs_dir.mkdir(exist_ok=True)
    out = docs_dir / "index.html"
    out.write_text(dashboard_html, encoding="utf-8")
    log.info("Dashboard written to %s", out)


# ---------------------------------------------------------------------------
# 4. Generate email HTML
# ---------------------------------------------------------------------------

def generate_email_html(results: dict, generated_at: datetime) -> str:
    sessions = results.get("relevant_sessions", [])
    total_transport = sum(1 for s in sessions if s.get("category") == "תחבורה")
    total_energy = sum(1 for s in sessions if s.get("category") == "אנרגיה")
    date_str = generated_at.strftime("%d/%m/%Y")
    dashboard_url = _html.escape(GITHUB_PAGES_URL.rstrip("/")) if GITHUB_PAGES_URL else "#"

    cards_html = ""
    if sessions:
        for s in sessions:
            cat = s.get("category", "")
            if cat == "תחבורה":
                tag_bg, tag_color = "#E6F1FB", "#0C447C"
            else:
                tag_bg, tag_color = "#FAEEDA", "#633806"
            title     = _html.escape(s.get("title", ""))
            committee = _html.escape(s.get("committee", ""))
            dt        = _html.escape(s.get("datetime", ""))
            relevance = _html.escape(s.get("relevance", ""))
            cat_esc   = _html.escape(cat)
            link      = _html.escape(s.get("link") or "#")
            cards_html += f"""
      <div style="background:#fff;border-radius:10px;padding:20px 24px;
                  margin-bottom:14px;border:1px solid #E2E8F0;direction:rtl;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px;">
          <strong style="font-size:1rem;color:#1A2B3C;flex:1;min-width:200px;">{title}</strong>
          <span style="background:{tag_bg};color:{tag_color};border-radius:20px;
                       padding:3px 12px;font-size:0.8rem;font-weight:600;white-space:nowrap;">{cat_esc}</span>
        </div>
        <div style="margin-top:8px;font-size:0.85rem;color:#4A5568;">
          {committee} &nbsp;|&nbsp; {dt}
        </div>
        <div style="margin-top:8px;font-size:0.85rem;color:#6B7A8D;font-style:italic;">
          {relevance}
        </div>
        <div style="margin-top:14px;">
          <a href="{link}" target="_blank"
             style="background:#1B3A5C;color:#fff;text-decoration:none;
                    border-radius:6px;padding:7px 18px;font-size:0.85rem;">
            לדיון בכנסת ←
          </a>
        </div>
      </div>"""
    else:
        cards_html = """
      <div style="text-align:center;padding:40px;color:#6B7A8D;font-size:1rem;">
        No relevant committee discussions found today.
      </div>"""

    email_html = f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F4F6F9;font-family:'Segoe UI',Arial,sans-serif;">
  <div style="max-width:640px;margin:32px auto;background:#F4F6F9;">

    <!-- Header -->
    <div style="background:#1B3A5C;border-radius:12px 12px 0 0;padding:28px 32px;color:#fff;">
      <h1 style="margin:0;font-size:1.3rem;">🏛️ מוניטור ועדות הכנסת</h1>
      <p style="margin:6px 0 0;color:#A8C4E0;font-size:0.9rem;">תחבורה ואנרגיה — {date_str}</p>
    </div>

    <!-- Summary cards -->
    <div style="background:#fff;padding:24px 32px;border-right:1px solid #E2E8F0;
                border-left:1px solid #E2E8F0;display:flex;gap:12px;flex-wrap:wrap;">
      <div style="flex:1;min-width:120px;text-align:center;padding:14px;
                  background:#F7FAFD;border-radius:10px;">
        <div style="font-size:2rem;font-weight:800;color:#1B3A5C;">{len(sessions)}</div>
        <div style="font-size:0.78rem;color:#6B7A8D;margin-top:4px;">סה״כ רלוונטיים</div>
      </div>
      <div style="flex:1;min-width:120px;text-align:center;padding:14px;
                  background:#E6F1FB;border-radius:10px;">
        <div style="font-size:2rem;font-weight:800;color:#185FA5;">{total_transport}</div>
        <div style="font-size:0.78rem;color:#0C447C;margin-top:4px;">תחבורה</div>
      </div>
      <div style="flex:1;min-width:120px;text-align:center;padding:14px;
                  background:#FAEEDA;border-radius:10px;">
        <div style="font-size:2rem;font-weight:800;color:#854F0B;">{total_energy}</div>
        <div style="font-size:0.78rem;color:#633806;margin-top:4px;">אנרגיה</div>
      </div>
    </div>

    <!-- Session cards -->
    <div style="background:#F8FAFC;padding:20px 32px;border:1px solid #E2E8F0;
                border-top:none;">
      {cards_html}
    </div>

    <!-- Dashboard link -->
    <div style="background:#fff;padding:20px 32px;text-align:center;
                border:1px solid #E2E8F0;border-top:none;">
      <a href="{dashboard_url}" target="_blank"
         style="background:#185FA5;color:#fff;text-decoration:none;
                border-radius:8px;padding:10px 28px;font-size:0.9rem;display:inline-block;">
        צפייה ב-Dashboard המלא ←
      </a>
    </div>

    <!-- Footer -->
    <div style="background:#F0F4F9;border-radius:0 0 12px 12px;padding:16px 32px;
                text-align:center;color:#8A98A8;font-size:0.78rem;border:1px solid #E2E8F0;border-top:none;">
      נשלח אוטומטית &nbsp;|&nbsp; GitHub Actions &nbsp;|&nbsp; {date_str}
    </div>

  </div>
</body>
</html>"""
    return email_html


# ---------------------------------------------------------------------------
# 5. Send email
# ---------------------------------------------------------------------------

def send_email(results: dict, generated_at: datetime) -> None:
    if not all([GMAIL_USER, GMAIL_APP_PASS, RECIPIENT_EMAIL]):
        log.warning("Email credentials not set — skipping email.")
        return

    n = len(results.get("relevant_sessions", []))
    log.info("Sending daily summary email (%d relevant session(s))…", n)
    subject = f"🏛️ דיוני ועדות הכנסת — תחבורה ואנרגיה | {generated_at.strftime('%d/%m/%Y')}"
    body_html = generate_email_html(results, generated_at)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    log.info("Sending email to %s…", RECIPIENT_EMAIL)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.login(GMAIL_USER, GMAIL_APP_PASS)
            server.sendmail(GMAIL_USER, RECIPIENT_EMAIL, msg.as_string())
        log.info("Email sent successfully.")
    except smtplib.SMTPException as exc:
        log.error("Failed to send email: %s", exc)


# ---------------------------------------------------------------------------
# 6. Preview (CLI)
# ---------------------------------------------------------------------------

def print_preview(results: dict, generated_at: datetime) -> None:
    sessions = results.get("relevant_sessions", [])
    print(f"\n{'='*60}")
    print(f"  מוניטור ועדות הכנסת — תחבורה ואנרגיה")
    print(f"  {generated_at.strftime('%d/%m/%Y %H:%M')}")
    print(f"{'='*60}")
    print(f"  סה\"כ נסרקו: {results.get('total_scanned', 0)} | רלוונטיים: {len(sessions)}")
    print(f"{'='*60}\n")

    if not sessions:
        print("  אין דיונים רלוונטיים בתחומי תחבורה ואנרגיה.\n")
        return

    for i, s in enumerate(sessions, 1):
        cat_marker = "[תחב]" if s.get("category") == "תחבורה" else "[אנר]"
        print(f"  {i}. {cat_marker} {s.get('title','')}")
        print(f"     ועדה: {s.get('committee','')}")
        print(f"     תאריך: {s.get('datetime','')}")
        print(f"     סיבה: {s.get('relevance','')}")
        print(f"     קישור: {s.get('link','')}")
        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Knesset Committee Monitor")
    parser.add_argument(
        "--preview", action="store_true",
        help="Print analysis to terminal without sending email or writing files"
    )
    args = parser.parse_args()

    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY is not set. Aborting.")
        sys.exit(1)

    generated_at = datetime.now()

    sessions = fetch_sessions()
    results = analyse_with_gemini(sessions)

    if args.preview:
        print_preview(results, generated_at)
    else:
        write_dashboard(results, generated_at)
        send_email(results, generated_at)
        log.info("Done.")


if __name__ == "__main__":
    main()
