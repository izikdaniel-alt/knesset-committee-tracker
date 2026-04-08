# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

Automated monitor for upcoming Knesset committee sessions. Fetches sessions from the Knesset OData API, filters them for transportation & energy topics using the Gemini AI API, generates an HTML dashboard (`docs/index.html`), and sends an email report via Gmail SMTP.

## Running the Script

```bash
# Install dependencies
pip install -r requirements.txt

# Full run: generates dashboard + sends email
python knesset_monitor.py

# Preview mode: prints to terminal only, no file write or email
python knesset_monitor.py --preview
```

## Required Environment Variables (`.env`)

```
GEMINI_API_KEY=...
GMAIL_USER=your_gmail@gmail.com
GMAIL_APP_PASS=xxxx xxxx xxxx xxxx
RECIPIENT_EMAIL=recipient@example.com
GITHUB_PAGES_URL=https://izikdaniel-alt.github.io/knesset-committee-tracker
```

## Architecture

Everything lives in `knesset_monitor.py` as four sequential steps:

1. **`fetch_sessions()`** — Queries the Knesset OData API for sessions in the next 30 days, parses `/Date(ms)/` timestamps, and returns a flat list of session dicts.
2. **`analyse_with_claude()`** (misnamed; actually calls Gemini) — Sends the session list as JSON in a Hebrew prompt to `gemini-2.0-flash-lite`. Returns structured JSON with `relevant_sessions`, `total_scanned`, and `has_results`. Falls back to returning all sessions if the API fails.
3. **`generate_dashboard()`** — Builds a self-contained RTL HTML file with KPI cards, filter buttons, a searchable table, and a detail modal. The session data is embedded as a JSON blob in a `<script>` tag. All API-sourced values are escaped with `html.escape()`.
4. **`send_email()`** — Sends an HTML email via Gmail SMTP with STARTTLS.

## Deployment

- GitHub Actions workflow (`.github/workflows/monitor.yml`) runs every Sunday and Wednesday at 05:00 UTC (07:00 Israel time), commits the updated `docs/index.html`, and pushes.
- The dashboard is served via GitHub Pages from the `docs/` folder on the `main` branch.
- GitHub Secrets mirror the `.env` variables (use `GEMINI_API_KEY` as the secret name, not `ANTHROPIC_API_KEY`).
