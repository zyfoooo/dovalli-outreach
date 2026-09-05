#!/usr/bin/env python3
"""
Dovalli outreach automation.
Runs on GitHub Actions cron every weekday at 4:30 PM ET.

Flow:
1. Fetch Notion prospects that are due for outreach today
2. For each, use Claude to draft a personalized email
3. Send via Resend
4. Update Notion status + follow-up date
"""

import os
import sys
import json
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from anthropic import Anthropic

# ---- Config ----
NOTION_TOKEN = os.environ["NOTION_TOKEN"].strip()
NOTION_DB_ID = os.environ["NOTION_PROSPECTS_DB_ID"].strip()
RESEND_KEY = os.environ["RESEND_API_KEY"].strip()
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"].strip()
FROM_EMAIL = os.environ.get("FROM_EMAIL", "hello@dovalli.com").strip()
FROM_NAME = os.environ.get("FROM_NAME", "Andrew").strip()
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "10"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

anthropic = Anthropic(api_key=ANTHROPIC_KEY)

TODAY = date.today().isoformat()
FOLLOWUP_3_DAYS = (date.today() + timedelta(days=3)).isoformat()
FOLLOWUP_7_DAYS = (date.today() + timedelta(days=7)).isoformat()


# ---- Notion helpers ----

def notion_query_prospects() -> list:
    """Fetch prospects needing outreach today (paginated)."""
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query"
    body = {
        "page_size": 100,
        "filter": {
            "or": [
                {"and": [
                    {"property": "Status", "select": {"equals": "Cold"}},
                    {"property": "Email", "rich_text": {"is_not_empty": True}},
                ]},
                {"and": [
                    {"property": "Status", "select": {"equals": "Emailed"}},
                    {"property": "Next Follow-up", "date": {"on_or_before": TODAY}},
                    {"property": "Email", "rich_text": {"is_not_empty": True}},
                ]},
            ]
        },
    }
    results = []
    cursor = None
    while True:
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(url, headers=NOTION_HEADERS, json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        body.pop("start_cursor", None)
        if cursor:
            body["start_cursor"] = cursor
    return results


def get_prop(page: dict, name: str) -> Optional[dict]:
    return page.get("properties", {}).get(name)


def prop_text(page: dict, name: str) -> str:
    p = get_prop(page, name)
    if not p:
        return ""
    if p.get("type") == "title":
        parts = p.get("title", [])
    elif p.get("type") == "rich_text":
        parts = p.get("rich_text", [])
    else:
        return ""
    return "".join(x.get("plain_text", "") for x in parts).strip()


def prop_email(page: dict, name: str) -> str:
    p = get_prop(page, name)
    if not p:
        return ""
    # Handle both native email type and rich_text (Andrew's DB uses rich_text)
    if p.get("type") == "email":
        return (p.get("email") or "").strip()
    if p.get("type") == "rich_text":
        parts = p.get("rich_text", [])
        return "".join(x.get("plain_text", "") for x in parts).strip()
    if p.get("type") == "title":
        parts = p.get("title", [])
        return "".join(x.get("plain_text", "") for x in parts).strip()
    return ""


def prop_select(page: dict, name: str) -> str:
    p = get_prop(page, name)
    if not p or not p.get("select"):
        return ""
    return p["select"].get("name", "")


def notion_update_page(page_id: str, properties: dict) -> None:
    url = f"https://api.notion.com/v1/pages/{page_id}"
    r = requests.patch(url, headers=NOTION_HEADERS, json={"properties": properties}, timeout=30)
    r.raise_for_status()


# ---- Email drafting via Claude ----

FIRST_EMAIL_PROMPT = """You are drafting a cold outreach email for Dovalli, an AI automation agency that builds 24/7 AI assistants for real estate agents' websites.

The AI captures leads and answers buyer questions on their site so they never miss a lead.

Tone: warm, conversational, brief. Not salesy. Reads like a real person, not marketing copy.

Recipient: {name} (real estate agent).

Constraints:
- Subject line: 4-6 words, lowercase preferred, no clickbait, no exclamation marks
- Body: 3-5 short sentences MAX
- No "I hope this finds you well" or similar filler
- One clear reason we reached out (mention they're a real estate agent + why AI helps)
- One clear call-to-action: point them to dovalli.com to see it themselves (self-serve, no call needed)
- Sign off: just "— {sender}"
- No emojis, no exclamation marks

Return your response as JSON: {{"subject": "...", "body": "..."}}
Nothing else."""


FOLLOWUP_EMAIL_PROMPT = """You are drafting a follow-up email for Dovalli, an AI automation agency for real estate agents.

You already sent one email to {name} {days_ago} days ago and got no reply.

Tone: brief, no pressure, respectful of their time. Not pushy.

Constraints:
- Subject: reply to previous or fresh 3-4 word subject
- Body: 2-3 sentences max
- Acknowledge you're following up, briefly restate the value, easy out
- Sign off: just "— {sender}"
- No emojis, no exclamation marks

Return your response as JSON: {{"subject": "...", "body": "..."}}
Nothing else."""


def draft_email(name: str, is_followup: bool, days_since: int = 0) -> dict:
    prompt = (
        FOLLOWUP_EMAIL_PROMPT.format(name=name, days_ago=days_since, sender=FROM_NAME)
        if is_followup
        else FIRST_EMAIL_PROMPT.format(name=name, sender=FROM_NAME)
    )
    msg = anthropic.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    # Strip common code-fence wrapping if present
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    # Claude sometimes adds explanation after the JSON — parse only the first
    # valid object using raw_decode, which returns (obj, end_index).
    try:
        obj, _end = json.JSONDecoder().raw_decode(text)
        return obj
    except json.JSONDecodeError:
        # Fallback: try to extract {...} block via brace matching
        start = text.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        return json.loads(text[start:i + 1])
        raise


# ---- Resend ----

def send_email(to_email: str, to_name: str, subject: str, body: str) -> str:
    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {RESEND_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "from": f"{FROM_NAME} <{FROM_EMAIL}>",
        "to": [to_email],
        "subject": subject,
        "text": body,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json().get("id", "")


# ---- Main ----

def main():
    # DST-safe time check: only run if it's 4:30 PM ET (16:00-16:59 window)
    # Skips the second cron entry that fires an hour off.
    # workflow_dispatch (manual trigger) bypasses this check.
    now_et = datetime.now(ZoneInfo("America/New_York"))
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if not is_manual and now_et.hour != 16:
        print(f"Skipping: current ET hour is {now_et.hour}, only run at 16 (4 PM ET).")
        return

    print(f"[{datetime.now().isoformat()}] Dovalli outreach starting (ET: {now_et.isoformat()})")
    print(f"  today: {TODAY}, daily_limit: {DAILY_LIMIT}, dry_run: {DRY_RUN}")

    prospects = notion_query_prospects()
    print(f"  fetched {len(prospects)} candidates")

    # Prioritize: follow-ups first (already engaged), then new cold outreach
    followups = [p for p in prospects if prop_select(p, "Status") == "Emailed"]
    cold = [p for p in prospects if prop_select(p, "Status") == "Cold"]
    queue = (followups + cold)[:DAILY_LIMIT]

    print(f"  queue: {len(queue)} ({len(followups)} follow-ups, {len(cold[:DAILY_LIMIT - len(followups)])} cold)")

    sent = 0
    errors = 0

    for page in queue:
        page_id = page["id"]
        name = prop_text(page, "Name")
        email = prop_email(page, "Email")
        status = prop_select(page, "Status")

        if not email or not name:
            print(f"  SKIP: missing name={name!r} email={email!r} ({page_id[:8]})")
            continue

        is_followup = status == "Emailed"

        try:
            draft = draft_email(name, is_followup)
            subject = draft["subject"].strip()
            body = draft["body"].strip()

            print(f"  -> {name} <{email}> ({'follow-up' if is_followup else 'cold'})")
            print(f"     subject: {subject}")

            if DRY_RUN:
                print("     [DRY_RUN — not sending, not updating]")
                sent += 1
                continue

            send_email(email, name, subject, body)

            # Update Notion
            props: dict = {
                "Status": {"select": {"name": "Emailed"}},
                "Date Contacted": {"date": {"start": TODAY}},
                "Next Follow-up": {"date": {"start": FOLLOWUP_3_DAYS if not is_followup else FOLLOWUP_7_DAYS}},
            }
            notion_update_page(page_id, props)
            sent += 1

        except Exception as e:
            errors += 1
            print(f"     ERROR: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

    print(f"[{datetime.now().isoformat()}] done. sent={sent}, errors={errors}")
    if errors > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
