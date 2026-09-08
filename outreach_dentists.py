#!/usr/bin/env python3
"""
Dovalli DENTIST outreach automation.
Runs on GitHub Actions cron. Same infrastructure as outreach.py (realtor version)
but reads from Dentists CRM and uses dentist-tuned prompts.

Flow:
1. Fetch dentist prospects due for outreach today
2. Use Claude to draft personalized emails (rotating angles)
3. Send via Resend
4. Update Notion status + follow-up date
"""

import os
import sys
import json
import random
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from anthropic import Anthropic

# ---- Config ----
NOTION_TOKEN = os.environ["NOTION_TOKEN"].strip()
NOTION_DB_ID = os.environ["NOTION_DENTIST_DB_ID"].strip()
RESEND_KEY = os.environ["RESEND_API_KEY"].strip()
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"].strip()
FROM_EMAIL = os.environ.get("FROM_EMAIL", "hello@dovalli.com").strip()
FROM_NAME = os.environ.get("FROM_NAME", "Andrew").strip()
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "10"))
FOLLOWUP_LIMIT = int(os.environ.get("FOLLOWUP_LIMIT", "5"))
COLD_LIMIT = int(os.environ.get("COLD_LIMIT", "5"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
MAX_FOLLOWUP_DAYS = int(os.environ.get("MAX_FOLLOWUP_DAYS", "20"))

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


def get_prop(page, name):
    return page.get("properties", {}).get(name)


def prop_text(page, name):
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


def prop_email(page, name):
    p = get_prop(page, name)
    if not p:
        return ""
    if p.get("type") == "email":
        return (p.get("email") or "").strip()
    if p.get("type") == "rich_text":
        parts = p.get("rich_text", [])
        return "".join(x.get("plain_text", "") for x in parts).strip()
    if p.get("type") == "title":
        parts = p.get("title", [])
        return "".join(x.get("plain_text", "") for x in parts).strip()
    return ""


def prop_select(page, name):
    p = get_prop(page, name)
    if not p or not p.get("select"):
        return ""
    return p["select"].get("name", "")


def prop_date(page, name):
    p = get_prop(page, name)
    if not p or not p.get("date"):
        return None
    start = p["date"].get("start")
    if not start:
        return None
    try:
        return date.fromisoformat(start[:10])
    except ValueError:
        return None


def notion_update_page(page_id, properties):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    r = requests.patch(url, headers=NOTION_HEADERS, json={"properties": properties}, timeout=30)
    r.raise_for_status()


# ---- DENTIST-TUNED prompts ----

FIRST_EMAIL_ANGLES = {
    "missed_calls": {
        "hook": "the missed-calls angle — emergency toothache calls at 9pm on a Saturday go straight to voicemail, and the patient calls the next practice on Google. A dental practice loses $1K-3K per missed emergency. Dovalli's AI answers 24/7 and captures the appointment.",
        "cta": "point them to dovalli.com to see a demo they can talk to",
    },
    "no_shows": {
        "hook": "the no-show angle — 15-20% appointment no-shows kill the schedule. Front desk spends hours calling to confirm. Dovalli's AI handles reminders and confirmations automatically and cuts no-shows in half.",
        "cta": "point them to dovalli.com to see how it works",
    },
    "front_desk_overload": {
        "hook": "the front-desk angle — insurance verification, appointment questions, and new patient forms eat 30-40% of front desk time. Dovalli's AI handles the routine questions so staff can focus on patients in the chair.",
        "cta": "invite them to see it at dovalli.com — takes 60 seconds",
    },
}


def build_first_email_prompt(name: str, angle_key: str) -> str:
    angle = FIRST_EMAIL_ANGLES[angle_key]
    return f"""You are drafting a cold outreach email for Dovalli, an AI automation agency that helps dental practices capture leads and reduce no-shows via a 24/7 AI assistant on their website (and eventually their phone).

Tone: warm, conversational, brief. Not salesy. Reads like a real person, not marketing copy.

Recipient: {name} (dental practice contact). Use only what's before a comma or "DDS/DMD" for the greeting — first name if obvious, otherwise the practice name.

ANGLE FOR THIS EMAIL: {angle['hook']}
CTA: {angle['cta']}

Subject line rules:
- 4-6 words, lowercase preferred, no clickbait, no exclamation marks
- Should hint at the angle above without being generic

BODY STRUCTURE (use this EXACT structure with blank lines between each section):

Hey [first name or practice],

[Opening sentence — reference the angle above in a specific, concrete way for a dental practice]

[Middle 1-2 sentences — what Dovalli does in plain terms tied to the angle]

[Closing sentence — the CTA above]

— {FROM_NAME}

Rules for the body:
- Use \\n\\n between paragraphs (critical for readability)
- Each paragraph is 1-2 sentences MAX
- No filler like "I hope this finds you well"
- No emojis, no exclamation marks
- Vary the exact wording each time
- Avoid dental jargon — write plainly

Return your response as JSON with this exact shape:
{{"subject": "...", "body": "Hey Sarah,\\n\\nOpening line here.\\n\\nMiddle line here.\\n\\nCTA line.\\n\\n— {FROM_NAME}"}}

Only return the JSON. No explanation before or after."""


FOLLOWUP_EMAIL_PROMPT = """You are drafting a follow-up email for Dovalli, an AI automation agency for dental practices.

You already sent one email to {name} {days_ago} days ago and got no reply.

Tone: brief, no pressure, respectful of their time. Not pushy.

Subject line rules:
- 2-4 words, lowercase, casual, no exclamation marks
- Do NOT use the phrase "quick follow-up" or "quick follow up" — overused
- Vary each time. Real-person phrasing.

BODY STRUCTURE (use this EXACT structure with blank lines between each section):

Hey [first name or practice],

[One sentence acknowledging you're following up]

[One sentence restating the value for a dental practice — new patient capture, no-show reduction, or 24/7 answering]

[Easy out — something like "if the timing is off, no worries"]

— {sender}

Rules for the body:
- Use \\n\\n between paragraphs (critical for readability)
- Each paragraph is 1 sentence
- No emojis, no exclamation marks
- Vary phrasing each time

Return your response as JSON with this exact shape:
{{"subject": "...", "body": "Hey Sarah,\\n\\nFollow-up line.\\n\\nValue line.\\n\\nEasy out.\\n\\n— {sender}"}}

Only return the JSON. No explanation before or after."""


def draft_email(name: str, is_followup: bool, days_since: int = 0):
    if is_followup:
        prompt = FOLLOWUP_EMAIL_PROMPT.format(name=name, days_ago=days_since, sender=FROM_NAME)
        angle_used = "followup"
    else:
        angle_used = random.choice(list(FIRST_EMAIL_ANGLES.keys()))
        prompt = build_first_email_prompt(name, angle_used)
    msg = anthropic.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        obj, _end = json.JSONDecoder().raw_decode(text)
        return obj, angle_used
    except json.JSONDecodeError:
        start = text.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        return json.loads(text[start:i + 1]), angle_used
        raise


# ---- Resend ----

def send_email(to_email, to_name, subject, body):
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
    now_et = datetime.now(ZoneInfo("America/New_York"))
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

    # Wide time window
    if not is_manual and not (15 <= now_et.hour <= 20):
        print(f"Skipping: current ET hour is {now_et.hour}, only run 3-8 PM ET.")
        return

    print(f"[{datetime.now().isoformat()}] Dovalli DENTIST outreach starting (ET: {now_et.isoformat()})")
    print(f"  today: {TODAY}, daily_limit: {DAILY_LIMIT}, dry_run: {DRY_RUN}")

    # Dedup: check if we already sent today
    if not is_manual and not DRY_RUN:
        try:
            dedup_url = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query"
            dedup_body = {
                "page_size": 1,
                "filter": {"property": "Date Contacted", "date": {"equals": TODAY}}
            }
            r = requests.post(dedup_url, headers=NOTION_HEADERS, json=dedup_body, timeout=30)
            r.raise_for_status()
            existing = r.json().get("results", [])
            if existing:
                print(f"Skipping: already sent dentist outreach today.")
                return
        except Exception as e:
            print(f"Warning: dedup check failed ({e}), proceeding anyway.")

    prospects = notion_query_prospects()
    print(f"  fetched {len(prospects)} candidates")

    followups = [p for p in prospects if prop_select(p, "Status") == "Emailed"]
    cold = [p for p in prospects if prop_select(p, "Status") == "Cold"]

    followup_slots = min(FOLLOWUP_LIMIT, len(followups))
    cold_slots = min(COLD_LIMIT, len(cold))
    if followup_slots < FOLLOWUP_LIMIT:
        cold_slots = min(cold_slots + (FOLLOWUP_LIMIT - followup_slots), len(cold), DAILY_LIMIT - followup_slots)
    if cold_slots < COLD_LIMIT:
        followup_slots = min(followup_slots + (COLD_LIMIT - cold_slots), len(followups), DAILY_LIMIT - cold_slots)

    queue = followups[:followup_slots] + cold[:cold_slots]
    queue = queue[:DAILY_LIMIT]

    print(f"  queue: {len(queue)} ({followup_slots} follow-ups, {cold_slots} cold) | pool: {len(followups)} follow-ups, {len(cold)} cold")

    sent = 0
    errors = 0
    marked_lost = 0

    for page in queue:
        page_id = page["id"]
        name = prop_text(page, "Name")
        email = prop_email(page, "Email")
        status = prop_select(page, "Status")

        if not email or not name:
            print(f"  SKIP: missing name={name!r} email={email!r} ({page_id[:8]})")
            continue

        is_followup = status == "Emailed"

        if is_followup:
            first_contact = prop_date(page, "Date Contacted")
            if first_contact and (date.today() - first_contact).days >= MAX_FOLLOWUP_DAYS:
                days_since = (date.today() - first_contact).days
                print(f"  -> {name} <{email}> MARK LOST ({days_since} days, no reply)")
                if not DRY_RUN:
                    try:
                        notion_update_page(page_id, {
                            "Status": {"select": {"name": "Lost"}},
                            "Next Follow-up": {"date": None},
                        })
                        marked_lost += 1
                    except Exception as e:
                        errors += 1
                        print(f"     ERROR marking lost: {e}")
                else:
                    marked_lost += 1
                continue

        try:
            draft, angle = draft_email(name, is_followup)
            subject = draft["subject"].strip()
            body = draft["body"].strip()

            print(f"  -> {name} <{email}> ({'follow-up' if is_followup else 'cold'} · angle={angle})")
            print(f"     subject: {subject}")

            if DRY_RUN:
                print("     [DRY_RUN — not sending, not updating]")
                sent += 1
                continue

            send_email(email, name, subject, body)

            props = {
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

    print(f"[{datetime.now().isoformat()}] done. sent={sent}, marked_lost={marked_lost}, errors={errors}")
    if errors > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
