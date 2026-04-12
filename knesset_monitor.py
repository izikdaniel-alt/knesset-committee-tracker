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
from datetime import datetime, timedelta, timezone
from urllib.parse import quote as url_quote
from zoneinfo import ZoneInfo
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
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

GEMINI_MODEL = "models/gemini-2.5-flash"
BATCH_SIZE = 150          # max sessions per Gemini call to stay within token limits
MAX_GEMINI_RETRIES = 3

HEBREW_DAYS = ["יום שני", "יום שלישי", "יום רביעי", "יום חמישי", "יום שישי", "יום שבת", "יום ראשון"]
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
HISTORY_FILE = Path("docs/history.json")
MAX_HISTORY_RUNS = 30

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

DIRECT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _make_http_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


# ---------------------------------------------------------------------------
# 1. Fetch data from Knesset API
# ---------------------------------------------------------------------------

def fetch_sessions() -> list[dict]:
    today = datetime.now(ISRAEL_TZ)
    end = today + timedelta(days=90)

    knesset_url = KNESSET_API.format(
        start=today.strftime("%Y-%m-%dT00:00:00"),
        end=end.strftime("%Y-%m-%dT23:59:59"),
    )

    if SCRAPER_API_KEY:
        fetch_url = f"http://api.scraperapi.com?api_key={SCRAPER_API_KEY}&url={url_quote(knesset_url, safe='')}&country_code=il"
        headers = {}
        log.info("Using ScraperAPI proxy…")
    else:
        fetch_url = knesset_url
        headers = DIRECT_HEADERS
        log.warning("SCRAPER_API_KEY not set — calling Knesset API directly (may be blocked on CI).")

    log.info("Fetching sessions from Knesset API…")
    log.info("Knesset URL: %s", knesset_url)

    http = _make_http_session()
    resp = None
    for attempt in range(2):
        try:
            resp = http.get(fetch_url, headers=headers, timeout=60)
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

        date_iso = dt.strftime("%Y-%m-%d") if dt else ""
        # Knesset /Date(ms)/ encodes Israel local time; attach ISRAEL_TZ then convert to UTC for ICS
        datetime_utc = (
            dt.replace(tzinfo=ISRAEL_TZ).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            if dt else ""
        )

        sessions.append({
            "title":        title,
            "committee":    committee_name,
            "datetime":     dt_str,
            "date_iso":     date_iso,
            "datetime_utc": datetime_utc,
            "link":         link,
            "session_id":   session_id,
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

def generate_dashboard(results: dict, all_sessions: list[dict], history: list[dict], generated_at: datetime) -> str:
    relevant_sessions = results.get("relevant_sessions", [])
    total_relevant    = len(relevant_sessions)
    total_scanned     = results.get("total_scanned", 0)
    date_str          = generated_at.strftime("%d/%m/%Y %H:%M")
    today_iso         = generated_at.strftime("%Y-%m-%d")
    end_iso           = (generated_at + timedelta(days=90)).strftime("%Y-%m-%d")

    count_transport = sum(1 for s in relevant_sessions if s.get("category") == "תחבורה")
    count_energy    = sum(1 for s in relevant_sessions if s.get("category") == "אנרגיה")

    # Relevance map keyed by link
    relevance_map = {s["link"]: s for s in relevant_sessions if s.get("link")}

    enriched = []
    for s in all_sessions:
        rel = relevance_map.get(s.get("link", "#"), {})
        enriched.append({
            **s,
            "is_relevant": bool(rel),
            "category":    rel.get("category", ""),
            "relevance":   rel.get("relevance", ""),
        })

    all_sessions_js = json.dumps(enriched, ensure_ascii=False).replace("</script>", "<\\/script>")
    history_js      = json.dumps(history[:10], ensure_ascii=False).replace("</script>", "<\\/script>")

    # ── Committee options for advanced search ───────────────────────────────
    committees = sorted({s.get("committee", "") for s in all_sessions if s.get("committee", "")})
    committee_opts = "\n".join(
        f'              <option value="{_html.escape(c)}">{_html.escape(c)}</option>'
        for c in committees
    )

    # ── Table rows (plain string concatenation — NOT nested inside outer f-string) ──
    rows_html = ""
    if enriched:
        for idx, s in enumerate(enriched):
            is_rel    = s.get("is_relevant", False)
            raw_cat   = s.get("category", "")
            cat       = _html.escape(raw_cat)
            committee = _html.escape(s.get("committee", ""))
            title     = _html.escape(s.get("title", ""))
            dt        = _html.escape(s.get("datetime", ""))
            date_iso  = _html.escape(s.get("date_iso", ""))
            cat_cls   = ("badge-transport" if raw_cat == "תחבורה"
                         else "badge-energy" if raw_cat == "אנרגיה"
                         else "badge-default")
            search_str  = _html.escape(f"{s.get('title','')} {s.get('committee','')} {raw_cat}")
            link        = _html.escape(s.get("link") or "#")
            rel_str     = "true" if is_rel else "false"
            cat_display = cat if cat else "—"
            rows_html += (
                f'<tr class="session-row" data-idx="{idx}" data-cat="{cat}"'
                f' data-committee="{committee}" data-search="{search_str}"'
                f' data-date="{date_iso}" data-relevant="{rel_str}">'
                f'<td class="td-title-cell"><span class="td-title">{title}</span></td>'
                f'<td data-sort="{committee}"><span class="badge badge-committee">{committee}</span></td>'
                f'<td class="td-date" data-sort="{date_iso}">{dt}</td>'
                f'<td><span class="badge {cat_cls}">{cat_display}</span></td>'
                f'<td class="td-action">'
                f'<a href="{link}" target="_blank" rel="noopener" class="btn-open"'
                f' onclick="event.stopPropagation()">פתח ←</a>'
                f'<button class="btn-cal" onclick="addToCalendar({idx});event.stopPropagation()">📅 יומן</button>'
                f'</td></tr>\n'
            )
    else:
        rows_html = (
            '<tr><td colspan="5" class="no-results">'
            '<div class="no-results-icon">📋</div>'
            '<p>אין דיונים ב-90 הימים הקרובים</p>'
            '</td></tr>'
        )

    # ── Skeleton rows ────────────────────────────────────────────────────────
    skel_row = (
        '<tr class="skel-row">'
        '<td><div class="skel skel-lg"></div></td>'
        '<td><div class="skel skel-sm"></div></td>'
        '<td><div class="skel skel-md"></div></td>'
        '<td><div class="skel skel-sm"></div></td>'
        '<td><div class="skel skel-btn-s"></div></td>'
        '</tr>'
    )
    skel_rows = "\n".join([skel_row] * 5)

    # ── History modal rows ────────────────────────────────────────────────────
    history_modal_body = ""
    if history:
        for run in history[:10]:
            run_date    = _html.escape(run.get("date", ""))
            run_rel     = run.get("relevant_sessions", [])
            run_scanned = run.get("total_scanned", 0)
            rel_count   = len(run_rel)
            inner = ""
            if run_rel:
                for rs in run_rel:
                    rs_title   = _html.escape((rs.get("title") or "")[:90])
                    rs_cat     = rs.get("category", "")
                    rs_link    = _html.escape(rs.get("link") or "#")
                    badge_cls  = "badge-transport" if rs_cat == "תחבורה" else "badge-energy"
                    rs_cat_esc = _html.escape(rs_cat)
                    inner += (
                        f'<div class="h-item">'
                        f'<a href="{rs_link}" target="_blank">{rs_title}</a>'
                        f'<span class="badge {badge_cls}" style="font-size:.62rem;padding:2px 8px">{rs_cat_esc}</span>'
                        f'</div>'
                    )
            else:
                inner = '<div class="h-item h-empty">אין דיונים רלוונטיים</div>'
            history_modal_body += (
                f'<details class="h-run">'
                f'<summary class="h-sum">'
                f'<span class="h-date">{run_date}</span>'
                f'<span class="h-stats">{rel_count} רלוונטיים · {run_scanned} נסרקו</span>'
                f'</summary>'
                f'<div class="h-body">{inner}</div>'
                f'</details>\n'
            )
    else:
        history_modal_body = '<p class="h-empty" style="padding:24px;text-align:center;color:var(--txt-3)">אין היסטוריית סריקות עדיין</p>'

    # ── GitHub Actions URL ───────────────────────────────────────────────────
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

    # ── Full HTML (outer f-string; all {{}} here are CSS/JS literal braces) ─
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
    body {{ font-family: 'Heebo', sans-serif; background: var(--bg); color: var(--txt);
            min-height: 100vh; -webkit-font-smoothing: antialiased; }}

    /* ─── LOADING BAR ─── */
    #loading-bar {{
      position: fixed; top: 0; left: 0; right: 0; height: 3px;
      background: linear-gradient(90deg, var(--blue), #60A5FA, var(--blue));
      background-size: 200% 100%;
      animation: loading-slide 1.2s linear infinite;
      z-index: 9999; transition: opacity .3s;
    }}
    @keyframes loading-slide {{ 0% {{ background-position: 200% 0; }} 100% {{ background-position: -200% 0; }} }}

    /* ─── SKELETON ─── */
    @keyframes shimmer {{
      0%   {{ background-position: -600px 0; }}
      100% {{ background-position:  600px 0; }}
    }}
    .skel {{
      border-radius: 6px; height: 14px;
      background: linear-gradient(90deg, #E2E8F0 25%, #F1F5F9 50%, #E2E8F0 75%);
      background-size: 600px 100%;
      animation: shimmer 1.4s infinite linear;
    }}
    .skel-lg  {{ width: 80%; }} .skel-md {{ width: 55%; }} .skel-sm {{ width: 38%; }}
    .skel-btn-s {{ width: 60px; height: 28px; border-radius: 7px; }}
    .skel-row td {{ padding: 18px 20px; border-bottom: 1px solid var(--border-lt); }}
    #real-tbody {{ opacity: 0; transition: opacity .25s; }}
    #real-tbody.visible {{ opacity: 1; }}

    /* ─── HEADER ─── */
    .hdr {{
      background: var(--hdr); height: 60px; padding: 0 28px;
      display: flex; align-items: center; justify-content: space-between;
      position: sticky; top: 0; z-index: 200;
      border-bottom: 1px solid rgba(255,255,255,.06);
    }}
    .hdr-brand {{ display: flex; align-items: center; gap: 10px; }}
    .hdr-logo {{ width: 34px; height: 34px; background: var(--blue); border-radius: 8px;
                 display: grid; place-items: center; font-size: 1rem; flex-shrink: 0; }}
    .hdr-name {{ font-size: .98rem; font-weight: 700; color: #F1F5F9; }}
    .hdr-sub  {{ font-size: .7rem; color: #64748B; margin-top: 1px; }}
    .hdr-right {{ display: flex; align-items: center; gap: 10px; }}
    .hdr-ts {{ font-size: .72rem; color: #64748B; display: flex; flex-direction: column; align-items: flex-end; }}
    .hdr-ts span {{ color: #94A3B8; font-size: .8rem; font-weight: 500; }}
    .btn-hdr {{
      display: inline-flex; align-items: center; gap: 6px;
      border-radius: 8px; padding: 7px 14px;
      font-size: .8rem; font-weight: 600; font-family: 'Heebo', sans-serif;
      text-decoration: none; transition: background .15s, border-color .15s; white-space: nowrap;
      cursor: pointer; border: 1px solid;
    }}
    .btn-hdr-blue {{
      background: rgba(37,99,235,.15); border-color: rgba(37,99,235,.4); color: #93C5FD;
    }}
    .btn-hdr-blue:hover {{ background: rgba(37,99,235,.28); border-color: #3B82F6; color: #BFDBFE; }}
    .btn-hdr-ghost {{
      background: rgba(255,255,255,.06); border-color: rgba(255,255,255,.12); color: #94A3B8;
    }}
    .btn-hdr-ghost:hover {{ background: rgba(255,255,255,.12); color: #CBD5E1; }}

    /* ─── HERO KPI ─── */
    .hero {{ background: var(--hdr); border-bottom: 1px solid rgba(255,255,255,.06); padding: 20px 28px 24px; }}
    .hero-grid {{ max-width: 1200px; margin: 0 auto; display: grid; grid-template-columns: repeat(3,1fr); gap: 16px; }}
    .kpi {{ background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.08);
            border-radius: var(--radius); padding: 20px 22px; display: flex; align-items: center; gap: 16px;
            position: relative; overflow: hidden; }}
    .kpi::before {{ content: ''; position: absolute; top: 0; right: 0; width: 4px; height: 100%;
                    border-radius: 0 var(--radius) var(--radius) 0; }}
    .kpi-total::before  {{ background: var(--blue); }}
    .kpi-transp::before {{ background: var(--emerald); }}
    .kpi-energy::before {{ background: var(--amber); }}
    .kpi-ico {{ width: 44px; height: 44px; border-radius: 10px; display: grid; place-items: center;
                font-size: 1.25rem; flex-shrink: 0; }}
    .kpi-total  .kpi-ico {{ background: rgba(37,99,235,.18); }}
    .kpi-transp .kpi-ico {{ background: rgba(5,150,105,.18); }}
    .kpi-energy .kpi-ico {{ background: rgba(217,119,6,.18); }}
    .kpi-val  {{ font-size: 2.4rem; font-weight: 900; line-height: 1; color: #F1F5F9; }}
    .kpi-lbl  {{ font-size: .75rem; color: #94A3B8; margin-top: 4px; font-weight: 500; }}
    .kpi-hint {{ font-size: .68rem; color: #64748B; margin-top: 3px; }}

    /* ─── MAIN ─── */
    .main {{ max-width: 1200px; margin: 0 auto; padding: 24px 20px 56px; }}

    /* ─── PANEL ─── */
    .panel {{ background: var(--surface); border-radius: 14px; border: 1px solid var(--border);
              box-shadow: var(--shadow); overflow: hidden; margin-bottom: 20px; }}

    /* ─── TOOLBAR (category pills row) ─── */
    .toolbar {{
      padding: 14px 20px; border-bottom: 1px solid var(--border-lt);
      display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    }}
    .toolbar-title {{ font-size: .9rem; font-weight: 700; color: var(--txt); flex: 1; min-width: 120px; }}
    .toolbar-title small {{ display: block; font-size: .7rem; color: var(--txt-3); font-weight: 400; margin-top: 1px; }}

    /* Category pills */
    .filters {{ display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }}
    .fpill {{
      border: 1px solid var(--border); border-radius: 20px; padding: 5px 14px;
      font-size: .78rem; font-weight: 500; cursor: pointer; font-family: 'Heebo', sans-serif;
      background: var(--surface); color: var(--txt-2); transition: all .15s; white-space: nowrap;
      display: inline-flex; align-items: center; gap: 5px;
    }}
    .fpill .fc {{ font-size: .7rem; background: var(--border-lt); border-radius: 20px; padding: 1px 7px; min-width: 20px; text-align: center; }}
    .fpill:hover {{ border-color: var(--blue); color: var(--blue); }}
    .fpill.active {{ background: var(--blue); border-color: var(--blue); color: #fff; font-weight: 600; }}
    .fpill.active .fc {{ background: rgba(255,255,255,.2); color: #fff; }}
    .fpill[data-filter="תחבורה"].active {{ background: var(--emerald); border-color: var(--emerald); }}
    .fpill[data-filter="אנרגיה"].active  {{ background: var(--amber);   border-color: var(--amber);   }}

    /* ─── ADVANCED SEARCH PANEL ─── */
    .adv-search {{
      background: #F8FAFC; border-bottom: 1px solid var(--border);
      padding: 0; overflow: hidden;
      max-height: 0; transition: max-height .3s ease, padding .3s ease;
    }}
    .adv-search.open {{ max-height: 200px; padding: 16px 20px; }}
    .adv-search-inner {{
      display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-end;
    }}
    .adv-field {{ display: flex; flex-direction: column; gap: 4px; flex: 1; min-width: 140px; }}
    .adv-label {{ font-size: .72rem; font-weight: 600; color: var(--txt-3); letter-spacing: .03em; }}
    .adv-inp, .adv-sel {{
      padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px;
      font-family: 'Heebo', sans-serif; font-size: .84rem; color: var(--txt);
      background: #fff; outline: none; transition: border-color .15s, box-shadow .15s;
      direction: rtl; width: 100%;
    }}
    .adv-inp:focus, .adv-sel:focus {{
      border-color: var(--blue); box-shadow: 0 0 0 3px rgba(37,99,235,.1);
    }}
    .adv-inp::placeholder {{ color: var(--txt-3); }}
    .adv-date-pair {{ display: flex; gap: 6px; align-items: center; }}
    .adv-date-pair span {{ font-size: .75rem; color: var(--txt-3); }}
    .adv-actions {{ display: flex; gap: 8px; flex-shrink: 0; align-items: flex-end; padding-bottom: 0; }}
    .btn-search {{
      padding: 8px 20px; background: var(--blue); color: #fff; border: none;
      border-radius: 8px; font-family: 'Heebo', sans-serif; font-size: .84rem;
      font-weight: 700; cursor: pointer; transition: background .15s; white-space: nowrap;
    }}
    .btn-search:hover {{ background: var(--blue-dk); }}
    .btn-reset {{
      padding: 8px 14px; background: transparent; color: var(--txt-3);
      border: 1px solid var(--border); border-radius: 8px;
      font-family: 'Heebo', sans-serif; font-size: .82rem; cursor: pointer;
      transition: all .15s; white-space: nowrap;
    }}
    .btn-reset:hover {{ color: var(--txt-2); border-color: var(--txt-3); }}

    /* Table */
    .tbl-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 580px; }}
    thead tr {{ background: #FAFBFC; }}
    th {{
      padding: 11px 20px; font-size: .7rem; font-weight: 700; color: var(--txt-3);
      text-align: right; border-bottom: 1px solid var(--border);
      white-space: nowrap; letter-spacing: .04em; text-transform: uppercase;
    }}
    th.sortable {{ cursor: pointer; user-select: none; }}
    th.sortable:hover {{ color: var(--blue); }}
    th.sort-asc  .sort-arrow::after {{ content: ' ▲'; font-size: .6rem; }}
    th.sort-desc .sort-arrow::after {{ content: ' ▼'; font-size: .6rem; }}
    td {{ padding: 15px 20px; border-bottom: 1px solid var(--border-lt); vertical-align: middle; }}
    .session-row {{ cursor: pointer; transition: background .1s; }}
    .session-row:last-child td {{ border-bottom: none; }}
    .session-row:hover td {{ background: #F8FAFF; }}
    .session-row[data-relevant="false"] td {{ opacity: .58; }}
    .session-row[data-relevant="false"]:hover td {{ opacity: .82; background: #FAFAFA; }}
    .td-title-cell {{ max-width: 380px; }}
    .td-title {{
      font-size: .9rem; font-weight: 600; color: var(--txt); line-height: 1.5;
      display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
    }}
    .td-date   {{ font-size: .82rem; color: var(--txt-2); white-space: nowrap; font-weight: 500; }}
    .td-action {{ text-align: center; white-space: nowrap; }}
    #no-results-row {{ display: none; }}

    /* Badges */
    .badge {{ display: inline-block; border-radius: 20px; padding: 3px 11px;
              font-size: .72rem; font-weight: 600; white-space: nowrap; }}
    .badge-committee {{ background: var(--border-lt); color: var(--txt-2); }}
    .badge-transport  {{ background: var(--emerald-lt); color: var(--emerald); }}
    .badge-energy     {{ background: var(--amber-lt);   color: var(--amber);   }}
    .badge-default    {{ background: var(--border-lt);  color: var(--txt-3);   }}

    /* Action buttons */
    .btn-open {{
      display: inline-flex; align-items: center;
      background: var(--blue); color: #fff; border-radius: 7px;
      padding: 6px 14px; font-size: .78rem; font-weight: 600;
      font-family: 'Heebo', sans-serif; text-decoration: none;
      transition: background .15s; white-space: nowrap;
    }}
    .btn-open:hover {{ background: var(--blue-dk); }}
    .btn-cal {{
      display: inline-flex; align-items: center; margin-right: 6px;
      background: transparent; color: var(--txt-2); border: 1px solid var(--border);
      border-radius: 7px; padding: 5px 12px; font-size: .75rem; font-weight: 500;
      font-family: 'Heebo', sans-serif; cursor: pointer; transition: all .15s; white-space: nowrap;
    }}
    .btn-cal:hover {{ border-color: var(--emerald); color: var(--emerald); background: var(--emerald-lt); }}

    /* No results */
    .no-results {{ text-align: center; padding: 56px 24px !important; color: var(--txt-3); }}
    .no-results-icon {{ font-size: 2rem; margin-bottom: 10px; }}
    .no-results p {{ font-size: .92rem; }}

    /* ─── MODALS (shared) ─── */
    .modal-overlay {{
      display: none; position: fixed; inset: 0;
      background: rgba(0,0,0,.48); backdrop-filter: blur(3px);
      z-index: 500; align-items: center; justify-content: center; padding: 16px;
    }}
    .modal-overlay.open {{ display: flex; }}
    .modal {{
      background: var(--surface); border-radius: 18px; width: 100%; max-width: 560px;
      max-height: 90vh; overflow-y: auto; box-shadow: 0 24px 64px rgba(0,0,0,.25);
      animation: modalIn .2s cubic-bezier(.34,1.3,.64,1);
    }}
    @keyframes modalIn {{
      from {{ opacity:0; transform: translateY(16px) scale(.97); }}
      to   {{ opacity:1; transform: translateY(0)   scale(1);   }}
    }}
    .modal-head {{
      padding: 20px 24px 16px; border-bottom: 1px solid var(--border-lt);
      display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
      position: sticky; top: 0; background: var(--surface); z-index: 1;
    }}
    .modal-head-title {{ font-size: .88rem; font-weight: 700; color: var(--txt); }}
    .modal-close {{
      background: var(--bg); border: none; border-radius: 8px; width: 32px; height: 32px;
      cursor: pointer; font-size: 1rem; color: var(--txt-3); flex-shrink: 0;
      display: grid; place-items: center; transition: background .15s;
    }}
    .modal-close:hover {{ background: var(--border); color: var(--txt); }}
    .modal-body {{ padding: 20px 24px 24px; }}

    /* Detail modal */
    .modal-title {{ font-size: 1.05rem; font-weight: 700; color: var(--txt); line-height: 1.5; margin-bottom: 16px; }}
    .modal-meta {{ display: flex; flex-direction: column; gap: 10px; margin-bottom: 16px; }}
    .modal-meta-row {{ display: flex; align-items: center; gap: 10px; font-size: .85rem; color: var(--txt-2); }}
    .modal-meta-ico {{ font-size: 1rem; }}
    .modal-relevance {{
      background: var(--bg); border-radius: 10px; padding: 12px 16px;
      font-size: .85rem; color: var(--txt-2); line-height: 1.6; margin-bottom: 18px; font-style: italic;
    }}
    .modal-actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .btn-modal-open {{
      flex: 1; display: flex; align-items: center; justify-content: center; gap: 6px;
      background: var(--blue); color: #fff; border-radius: 10px; padding: 11px 18px;
      font-size: .88rem; font-weight: 700; font-family: 'Heebo', sans-serif; text-decoration: none;
      transition: background .15s;
    }}
    .btn-modal-open:hover {{ background: var(--blue-dk); }}
    .btn-modal-cal {{
      flex: 1; display: flex; align-items: center; justify-content: center; gap: 6px;
      background: transparent; color: var(--txt-2); border: 1px solid var(--border);
      border-radius: 10px; padding: 11px 18px; font-size: .88rem; font-weight: 600;
      font-family: 'Heebo', sans-serif; cursor: pointer; transition: all .15s;
    }}
    .btn-modal-cal:hover {{ border-color: var(--emerald); color: var(--emerald); background: var(--emerald-lt); }}

    /* History modal */
    .modal-history {{ max-width: 640px; }}
    .h-run {{
      border: 1px solid var(--border); border-radius: 10px; margin-bottom: 8px; overflow: hidden;
    }}
    .h-sum {{
      padding: 12px 16px; cursor: pointer; list-style: none;
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      font-size: .82rem; color: var(--txt-2); user-select: none; transition: background .12s;
    }}
    .h-sum::-webkit-details-marker {{ display: none; }}
    .h-run[open] .h-sum {{ background: var(--border-lt); }}
    .h-sum:hover {{ background: var(--border-lt); }}
    .h-date {{ font-weight: 600; color: var(--txt); }}
    .h-stats {{ font-size: .74rem; color: var(--txt-3); }}
    .h-body {{ padding: 8px 16px 12px; border-top: 1px solid var(--border-lt); }}
    .h-item {{
      display: flex; align-items: center; justify-content: space-between; gap: 10px;
      padding: 6px 0; border-bottom: 1px solid var(--border-lt); font-size: .82rem;
    }}
    .h-item:last-child {{ border-bottom: none; }}
    .h-item a {{ color: var(--blue); text-decoration: none; flex: 1; }}
    .h-item a:hover {{ text-decoration: underline; }}
    .h-empty {{ color: var(--txt-3); font-style: italic; }}

    /* ─── FOOTER ─── */
    .footer {{ text-align: center; padding: 20px 24px 16px; color: var(--txt-3); font-size: .72rem; }}
    .sysinfo {{
      display: inline-flex; align-items: center; gap: 18px;
      background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
      padding: 8px 18px; font-size: .72rem; color: var(--txt-2); flex-wrap: wrap; justify-content: center;
    }}
    .sysinfo-item {{ display: flex; align-items: center; gap: 5px; white-space: nowrap; }}
    .sysinfo-dot {{ width: 6px; height: 6px; border-radius: 50%; background: var(--emerald); }}

    /* ─── RESPONSIVE ─── */
    @media (max-width: 768px) {{
      .hdr {{ padding: 0 14px; }} .hdr-sub {{ display: none; }}
      .hero {{ padding: 14px 14px 18px; }} .hero-grid {{ gap: 10px; }}
      .kpi {{ padding: 14px; gap: 12px; }} .kpi-val {{ font-size: 1.9rem; }}
      .main {{ padding: 14px 10px 48px; }}
      .toolbar {{ flex-direction: column; align-items: stretch; gap: 8px; }}
      .adv-search-inner {{ flex-direction: column; }}
      .adv-date-pair {{ flex-wrap: wrap; }}
      th, td {{ padding: 11px 12px; }} .td-title-cell {{ max-width: 200px; }}
      .modal {{ border-radius: 14px; }} .btn-hdr-blue span {{ display: none; }}
    }}
    @media (max-width: 480px) {{
      .kpi-hint {{ display: none; }} .kpi-ico {{ width: 36px; height: 36px; font-size: 1rem; }}
      .kpi-val {{ font-size: 1.6rem; }} .hero-grid {{ grid-template-columns: 1fr 1fr; gap: 8px; }}
      .kpi-energy {{ grid-column: 1 / -1; }}
    }}
  </style>
</head>
<body>

  <div id="loading-bar"></div>

  <!-- ═══ HEADER ═══ -->
  <header class="hdr">
    <div class="hdr-brand">
      <div class="hdr-logo">🏛️</div>
      <div>
        <div class="hdr-name">מוניטור ועדות הכנסת</div>
        <div class="hdr-sub">תחבורה ואנרגיה · 90 הימים הקרובים</div>
      </div>
    </div>
    <div class="hdr-right">
      <div class="hdr-ts">
        <span style="font-size:.68rem;color:#64748B;">עודכן</span>
        <span>{date_str}</span>
      </div>
      <button class="btn-hdr btn-hdr-ghost" onclick="openHistoryModal()">📅 <span>היסטוריה</span></button>
      <a href="{gh_actions_url}" target="_blank" rel="noopener" class="btn-hdr btn-hdr-blue">🔄 <span>רענן</span></a>
    </div>
  </header>

  <!-- ═══ HERO KPIs ═══ -->
  <section class="hero">
    <div class="hero-grid">
      <div class="kpi kpi-total">
        <div class="kpi-ico">📋</div>
        <div>
          <div class="kpi-val" id="kpi-total">{total_scanned}</div>
          <div class="kpi-lbl">דיונים מוצגים</div>
          <div class="kpi-hint">מתוך {total_scanned} שנסרקו</div>
        </div>
      </div>
      <div class="kpi kpi-transp">
        <div class="kpi-ico">🚗</div>
        <div>
          <div class="kpi-val" id="kpi-transp">{count_transport}</div>
          <div class="kpi-lbl">דיוני תחבורה</div>
          <div class="kpi-hint">תחבורה ציבורית, רכב, כבישים</div>
        </div>
      </div>
      <div class="kpi kpi-energy">
        <div class="kpi-ico">⚡</div>
        <div>
          <div class="kpi-val" id="kpi-energy">{count_energy}</div>
          <div class="kpi-lbl">דיוני אנרגיה</div>
          <div class="kpi-hint">חשמל, גז, אנרגיות מתחדשות</div>
        </div>
      </div>
    </div>
  </section>

  <!-- ═══ MAIN TABLE ═══ -->
  <main class="main">
    <div class="panel">

      <!-- Toolbar: title + category pills + search toggle -->
      <div class="toolbar">
        <div class="toolbar-title">
          לוח דיונים
          <small>לחץ על שורה לפרטים · לחץ על כותרת עמודה למיון</small>
        </div>
        <div class="filters">
          <button class="fpill active" data-filter="all">הכל <span class="fc" id="cnt-all">{total_scanned}</span></button>
          <button class="fpill" data-filter="תחבורה">🚗 תחבורה <span class="fc" id="cnt-transp">{count_transport}</span></button>
          <button class="fpill" data-filter="אנרגיה">⚡ אנרגיה <span class="fc" id="cnt-energy">{count_energy}</span></button>
        </div>
        <button class="fpill" id="btn-adv-toggle" onclick="toggleAdvSearch()">🔍 חיפוש מתקדם</button>
      </div>

      <!-- Advanced Search (hidden by default) -->
      <div class="adv-search" id="adv-search">
        <div class="adv-search-inner">
          <div class="adv-field">
            <label class="adv-label" for="adv-text">חיפוש חופשי</label>
            <input class="adv-inp" id="adv-text" type="text" placeholder="נושא, מילת מפתח...">
          </div>
          <div class="adv-field" style="flex:0 0 auto;min-width:0;">
            <label class="adv-label">טווח תאריכים</label>
            <div class="adv-date-pair">
              <input class="adv-inp" id="adv-from" type="date" value="{today_iso}" style="width:140px;">
              <span>—</span>
              <input class="adv-inp" id="adv-to" type="date" value="{end_iso}" style="width:140px;">
            </div>
          </div>
          <div class="adv-field">
            <label class="adv-label" for="adv-committee">ועדה</label>
            <select class="adv-sel" id="adv-committee">
              <option value="">כל הוועדות</option>
{committee_opts}
            </select>
          </div>
          <div class="adv-actions">
            <button class="btn-search" onclick="FilterManager.runSearch()">חפש</button>
            <button class="btn-reset"  onclick="FilterManager.resetSearch()">נקה</button>
          </div>
        </div>
      </div>

      <!-- Table -->
      <div class="tbl-wrap">
        <table>
          <thead>
            <tr>
              <th class="sortable" data-col="title" onclick="FilterManager.sortBy('title')">
                נושא הדיון <span class="sort-arrow"></span>
              </th>
              <th class="sortable" data-col="committee" onclick="FilterManager.sortBy('committee')">
                ועדה <span class="sort-arrow"></span>
              </th>
              <th class="sortable sort-asc" data-col="date" onclick="FilterManager.sortBy('date')">
                תאריך ושעה <span class="sort-arrow"></span>
              </th>
              <th>תחום</th>
              <th></th>
            </tr>
          </thead>
          <tbody id="skel-tbody">{skel_rows}</tbody>
          <tbody id="real-tbody">{rows_html}
            <tr id="no-results-row">
              <td colspan="5" class="no-results">
                <div class="no-results-icon">🔍</div>
                <p>לא נמצאו דיונים התואמים את החיפוש</p>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </main>

  <!-- ═══ DETAIL MODAL ═══ -->
  <div class="modal-overlay" id="overlay" onclick="closeDetailModal()">
    <div class="modal" onclick="event.stopPropagation()">
      <div class="modal-head">
        <div id="m-badge"></div>
        <button class="modal-close" onclick="closeDetailModal()">✕</button>
      </div>
      <div class="modal-body">
        <div class="modal-title" id="m-title"></div>
        <div class="modal-meta">
          <div class="modal-meta-row"><span class="modal-meta-ico">🏛️</span><span id="m-committee"></span></div>
          <div class="modal-meta-row"><span class="modal-meta-ico">🗓️</span><span id="m-date"></span></div>
        </div>
        <div class="modal-relevance" id="m-relevance" style="display:none"></div>
        <div class="modal-actions">
          <a class="btn-modal-open" id="m-link" target="_blank" rel="noopener">פתח בכנסת ←</a>
          <button class="btn-modal-cal" id="m-cal" onclick="addToCalendar(currentModalIdx)">📅 הוסף ליומן</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══ HISTORY MODAL ═══ -->
  <div class="modal-overlay" id="history-overlay" onclick="closeHistoryModal()">
    <div class="modal modal-history" onclick="event.stopPropagation()">
      <div class="modal-head">
        <div class="modal-head-title">📅 היסטוריית סריקות</div>
        <button class="modal-close" onclick="closeHistoryModal()">✕</button>
      </div>
      <div class="modal-body">
        {history_modal_body}
      </div>
    </div>
  </div>

  <footer class="footer">
    <div class="sysinfo">
      <div class="sysinfo-item"><span class="sysinfo-dot"></span><span>סריקה אחרונה: <strong>{date_str}</strong></span></div>
      <div class="sysinfo-item">🔍 נסרקו: <strong>{total_scanned}</strong></div>
      <div class="sysinfo-item">✅ רלוונטיים: <strong>{total_relevant}</strong></div>
    </div>
  </footer>

  <script>
    const ALL_SESSIONS = {all_sessions_js};
    const HISTORY      = {history_js};

    // ── FilterManager ────────────────────────────────────────────────────────
    const FilterManager = (() => {{
      let activeCategory = 'all';   // instant: category pill
      let searchText     = '';      // committed on Search button
      let searchCommittee = '';     // committed on Search button
      let searchFrom     = '';      // committed on Search button
      let searchTo       = '';      // committed on Search button
      let sortCol        = 'date';
      let sortDir        = 'asc';

      const tbody = () => document.getElementById('real-tbody');

      function apply() {{
        const rows = Array.from(tbody().querySelectorAll('.session-row'));

        // Sort
        rows.sort((a, b) => {{
          let va, vb;
          if (sortCol === 'date') {{
            va = a.dataset.date || ''; vb = b.dataset.date || '';
          }} else if (sortCol === 'committee') {{
            va = a.dataset.committee || ''; vb = b.dataset.committee || '';
          }} else {{
            va = a.dataset.search || ''; vb = b.dataset.search || '';
          }}
          const c = va.localeCompare(vb, 'he');
          return sortDir === 'asc' ? c : -c;
        }});
        rows.forEach(r => tbody().appendChild(r));

        // Visibility
        let visTotal = 0, visTr = 0, visEn = 0;
        rows.forEach(r => {{
          const cat      = r.dataset.cat       || '';
          const comm     = r.dataset.committee || '';
          const search   = r.dataset.search    || '';
          const date     = r.dataset.date      || '';
          const relevant = r.dataset.relevant  === 'true';

          const matchCat  = activeCategory === 'all'
            ? true
            : (activeCategory === 'תחבורה' || activeCategory === 'אנרגיה')
              ? relevant && cat === activeCategory
              : true;
          const matchText = !searchText     || search.includes(searchText);
          const matchComm = !searchCommittee || comm === searchCommittee;
          const matchFrom = !searchFrom     || date >= searchFrom;
          const matchTo   = !searchTo       || date <= searchTo;

          const show = matchCat && matchText && matchComm && matchFrom && matchTo;
          r.style.display = show ? '' : 'none';
          if (show) {{
            visTotal++;
            if (relevant && cat === 'תחבורה') visTr++;
            if (relevant && cat === 'אנרגיה')  visEn++;
          }}
        }});

        // Update counters
        const countAll   = activeCategory === 'all'      ? visTotal : rows.filter(r => r.style.display !== 'none').length;
        const countTr    = rows.filter(r => r.style.display !== 'none' && r.dataset.relevant === 'true' && r.dataset.cat === 'תחבורה').length;
        const countEn    = rows.filter(r => r.style.display !== 'none' && r.dataset.relevant === 'true' && r.dataset.cat === 'אנרגיה').length;
        const countVis   = rows.filter(r => r.style.display !== 'none').length;

        document.getElementById('cnt-all').textContent    = countVis;
        document.getElementById('cnt-transp').textContent = countTr;
        document.getElementById('cnt-energy').textContent = countEn;
        document.getElementById('kpi-total').textContent  = countVis;
        document.getElementById('kpi-transp').textContent = countTr;
        document.getElementById('kpi-energy').textContent = countEn;

        // No-results row
        document.getElementById('no-results-row').style.display = countVis === 0 ? '' : 'none';
      }}

      return {{
        setCategory(cat) {{
          activeCategory = cat;
          apply();
        }},
        runSearch() {{
          searchText      = (document.getElementById('adv-text').value || '').trim();
          searchCommittee = document.getElementById('adv-committee').value || '';
          searchFrom      = document.getElementById('adv-from').value || '';
          searchTo        = document.getElementById('adv-to').value   || '';
          apply();
        }},
        resetSearch() {{
          document.getElementById('adv-text').value      = '';
          document.getElementById('adv-committee').value = '';
          document.getElementById('adv-from').value      = '{today_iso}';
          document.getElementById('adv-to').value        = '{end_iso}';
          searchText = ''; searchCommittee = ''; searchFrom = ''; searchTo = '';
          apply();
        }},
        sortBy(col) {{
          sortDir = (sortCol === col && sortDir === 'asc') ? 'desc' : 'asc';
          sortCol = col;
          document.querySelectorAll('th.sortable').forEach(th => {{
            th.classList.remove('sort-asc', 'sort-desc');
            if (th.dataset.col === sortCol) th.classList.add('sort-' + sortDir);
          }});
          apply();
        }},
        init() {{
          document.getElementById('skel-tbody').style.display = 'none';
          document.getElementById('real-tbody').classList.add('visible');
          document.getElementById('loading-bar').style.opacity = '0';
          setTimeout(() => document.getElementById('loading-bar').remove(), 350);
          apply();
        }}
      }};
    }})();

    // ── Category pills ────────────────────────────────────────────────────────
    document.querySelectorAll('.fpill[data-filter]').forEach(btn => {{
      btn.addEventListener('click', () => {{
        document.querySelectorAll('.fpill[data-filter]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        FilterManager.setCategory(btn.dataset.filter);
      }});
    }});

    // ── Advanced search toggle ────────────────────────────────────────────────
    function toggleAdvSearch() {{
      const panel = document.getElementById('adv-search');
      const btn   = document.getElementById('btn-adv-toggle');
      const open  = panel.classList.toggle('open');
      btn.classList.toggle('active', open);
    }}

    // ── Row click → detail modal ──────────────────────────────────────────────
    let currentModalIdx = -1;

    document.querySelectorAll('.session-row').forEach(row => {{
      row.addEventListener('click', () => openDetailModal(parseInt(row.dataset.idx)));
    }});

    function openDetailModal(idx) {{
      const s = ALL_SESSIONS[idx];
      if (!s) return;
      currentModalIdx = idx;
      const cat = s.category || '';
      let badgeHtml = '';
      if      (cat === 'תחבורה') badgeHtml = '<span class="badge badge-transport">🚗 ' + cat + '</span>';
      else if (cat === 'אנרגיה') badgeHtml = '<span class="badge badge-energy">⚡ '   + cat + '</span>';
      document.getElementById('m-badge').innerHTML       = badgeHtml;
      document.getElementById('m-title').textContent     = s.title     || '';
      document.getElementById('m-committee').textContent = s.committee || '';
      document.getElementById('m-date').textContent      = s.datetime  || '';
      document.getElementById('m-link').href             = s.link      || '#';
      const relEl = document.getElementById('m-relevance');
      if (s.relevance) {{ relEl.textContent = s.relevance; relEl.style.display = 'block'; }}
      else               {{ relEl.style.display = 'none'; }}
      document.getElementById('overlay').classList.add('open');
      document.body.style.overflow = 'hidden';
    }}

    function closeDetailModal() {{
      document.getElementById('overlay').classList.remove('open');
      document.body.style.overflow = '';
      currentModalIdx = -1;
    }}

    // ── Add to Calendar (.ics download) ──────────────────────────────────────
    function addToCalendar(idx) {{
      const s = ALL_SESSIONS[idx];
      if (!s) return;
      const utc = s.datetime_utc || '';
      if (!utc) {{ alert('תאריך לא זמין לאירוע זה'); return; }}
      // Parse YYYYMMDDTHHmmSSZ
      const yr = parseInt(utc.substr(0,4)), mo = parseInt(utc.substr(4,2))-1,
            dy = parseInt(utc.substr(6,2)), hr = parseInt(utc.substr(9,2)),
            mn = parseInt(utc.substr(11,2));
      const t0 = new Date(Date.UTC(yr, mo, dy, hr, mn, 0));
      const t1 = new Date(t0.getTime() + 7200000); // +2 hours
      function icsDate(d) {{
        return d.toISOString().replace(/[-:]/g,'').split('.')[0]+'Z';
      }}
      const summary  = '[ועדת ' + (s.committee||'') + '] - ' + (s.title||'');
      const desc     = (s.title||'') + ' | קישור: ' + (s.link||'');
      const location = 'כנסת ישראל\\, ירושלים';
      const uid      = 'knesset-' + (s.session_id||idx) + '@knesset-monitor';
      const ics = [
        'BEGIN:VCALENDAR', 'VERSION:2.0',
        'PRODID:-//Knesset Monitor//IL',
        'CALSCALE:GREGORIAN', 'METHOD:PUBLISH',
        'BEGIN:VEVENT',
        'DTSTART:' + icsDate(t0),
        'DTEND:'   + icsDate(t1),
        'SUMMARY:' + summary,
        'DESCRIPTION:' + desc,
        'LOCATION:' + location,
        'UID:' + uid,
        'END:VEVENT', 'END:VCALENDAR'
      ].join('\\r\\n');
      const blob = new Blob([ics], {{type:'text/calendar;charset=utf-8'}});
      const a = document.createElement('a');
      a.href     = URL.createObjectURL(blob);
      a.download = 'knesset-' + (s.date_iso||'session').replace(/-/g,'') + '.ics';
      document.body.appendChild(a);
      a.click();
      setTimeout(() => {{ URL.revokeObjectURL(a.href); a.remove(); }}, 1000);
    }}

    // ── History modal ─────────────────────────────────────────────────────────
    function openHistoryModal() {{
      document.getElementById('history-overlay').classList.add('open');
      document.body.style.overflow = 'hidden';
    }}
    function closeHistoryModal() {{
      document.getElementById('history-overlay').classList.remove('open');
      document.body.style.overflow = '';
    }}

    // ── Keyboard ──────────────────────────────────────────────────────────────
    document.addEventListener('keydown', e => {{
      if (e.key === 'Escape') {{ closeDetailModal(); closeHistoryModal(); }}
    }});

    // ── Boot ──────────────────────────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', () => FilterManager.init());
  </script>

</body>
</html>"""
    return dashboard_html


def write_dashboard(results: dict, all_sessions: list[dict], history: list[dict], generated_at: datetime) -> None:
    dashboard_html = generate_dashboard(results, all_sessions, history, generated_at)
    docs_dir = Path("docs")
    docs_dir.mkdir(exist_ok=True)
    out = docs_dir / "index.html"
    out.write_text(dashboard_html, encoding="utf-8")
    log.info("Dashboard written to %s", out)


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------

def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_history(history: list[dict], results: dict, generated_at: datetime) -> list[dict]:
    entry = {
        "date": generated_at.strftime("%d/%m/%Y %H:%M"),
        "date_iso": generated_at.strftime("%Y-%m-%d"),
        "relevant_sessions": results.get("relevant_sessions", []),
        "total_scanned": results.get("total_scanned", 0),
    }
    history = [entry] + [h for h in history if h.get("date_iso") != entry["date_iso"]]
    history = history[:MAX_HISTORY_RUNS]
    HISTORY_FILE.parent.mkdir(exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("History saved (%d entries).", len(history))
    return history


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

    generated_at = datetime.now(ISRAEL_TZ)

    sessions = fetch_sessions()
    results = analyse_with_gemini(sessions)

    if args.preview:
        print_preview(results, generated_at)
    else:
        history = load_history()
        history = save_history(history, results, generated_at)
        write_dashboard(results, sessions, history, generated_at)
        send_email(results, generated_at)
        log.info("Done.")


if __name__ == "__main__":
    main()
