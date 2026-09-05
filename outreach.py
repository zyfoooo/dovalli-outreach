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
FOLLOWUP_LIMIT = int(os.environ.get("FOLLOWUP_LIMIT", "5"))
COLD_LIMIT = int(os.environ.get("COLD_LIMIT", "5"))
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

# Auto-mark Lost after this many days since first contact (~3 follow-ups worth)
MAX_FOLLOWUP_DAYS = int(os.environ.get("MAX_FOLLOWUP_DAYS", "20"))


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


def prop_date(page: dict, name: str) -> Optional[date]:
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


def notion_update_page(page_id: str, properties: dict) -> None:
    url = f"https://api.notion.com/v1/pages/{page_id}"
    r = requests.patch(url, headers=NOTION_HEADERS, json={"properties": properties}, timeout=30)
    r.raise_for_status()


# ---- Email drafting via Claude ----

FIRST_EMAIL_ANGLES = {
    "lost_leads": {
        "hook": "the pain angle — leads visit their site at 9pm on a Saturday and leave because nobody answered. Agents are actively losing money to slow response times.",
        "cta": "point them to dovalli.com to see a live demo they can talk to",
    },
    "competitive": {
        "hook": "the competitive angle — top-producing agents in their area are already using AI to respond in seconds. Don't get outpaced.",
        "cta": "point them to dovalli.com to see the same tool the top 1% are using",
    },
    "time_freedom": {
        "hook": "the time-back angle — real estate is 24/7 already. Take your evenings back, let the AI qualify visitors while you sleep.",
        "cta": "invite them to see it at dovalli.com — takes 60 seconds",
    },
}


def build_first_email_prompt(name: str, angle_key: str) -> str:
    angle = FIRST_EMAIL_ANGLES[angle_key]
    return f"""You are drafting a cold outreach email for Dovalli, an AI automation agency that builds 24/7 AI assistants for real estate agents' websites.

The AI captures leads and answers buyer questions on their site so they never miss a lead.

Tone: warm, conversational, brief. Not salesy. Reads like a real person, not marketing copy.

Recipient: {name} (real estate agent). Use their first name only in the greeting.

ANGLE FOR THIS EMAIL: {angle['hook']}
CTA: {angle['cta']}

Subject line rules:
- 4-6 words, lowercase preferred, no clickbait, no exclamation marks
- Should hint at the angle above without being generic

BODY STRUCTURE (use this EXACT structure with blank lines between each section):

Hey [first name],

[Opening sentence — reference the angle above in a specific, concrete way]

[Middle 1-2 sentences — what Dovalli does in plain terms tied to the angle]

[Closing sentence — the CTA above]

— {FROM_NAME}

Rules for the body:
- Use \\n\\n between paragraphs (critical for readability)
- Each paragraph is 1-2 sentences MAX, never a wall of text
- No filler like "I hope this finds you well"
- No emojis, no exclamation marks
- Vary the exact wording each time

Return your response as JSON with this exact shape:
{{"subject": "...", "body": "Hey Sarah,\\n\\nOpening line here.\\n\\nMiddle line here.\\n\\nCTA line.\\n\\n— {FROM_NAME}"}}

Only return the JSON. No explanation before or after."""


FOLLOWUP_EMAIL_PROMPT = """You are drafting a follow-up email for Dovalli, an AI automation agency for real estate agents.

You already sent one email to {name} {days_ago} days ago and got no reply.

Tone: brief, no pressure, respectful of their time. Not pushy.

Subject line rules:
- 2-4 words, lowercase, casual, no exclamation marks
- Do NOT use the phrase "quick follow-up" or "quick follow up" — overused
- Vary each time. Think: a note from a real person, not a template. Could reference the previous topic, ask a light question, or use a phrase like "one more thing", "still here", "worth a look", "circling back", "was thinking", "in case you missed it" — but rotate freely, not the same one twice

BODY STRUCTURE (use this EXACT structure with blank lines between each section):

Hey [first name],

[One sentence acknowledging you're following up — reference the previous email casually]

[One sentence restating the value in plain terms — no repeating the exact opener from before]

[Easy out — something like "if the timing is off, no worries, wish you the best on your listings"]

— {sender}

Rules for the body:
- Use \\n\\n between paragraphs (critical for readability)
- Each paragraph is 1 sentence
- No emojis, no exclamation marks
- Vary phrasing each time

Return your response as JSON with this exact shape:
{{"subject": "...", "body": "Hey Sarah,\\n\\nFollow-up line.\\n\\nValue line.\\n\\nEasy out.\\n\\n— {sender}"}}

Only return the JSON. No explanation before or after."""


def draft_email(name: str, is_followup: bool, days_since: int = 0) -> tuple:
    """Returns (draft_dict, angle_used) so we can track which angle was sent."""
    import random
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
    # Strip common code-fence wrapping if present
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    # Claude sometimes adds explanation after the JSON — parse only the first
    # valid object using raw_decode, which returns (obj, end_index).
    try:
        obj, _end = json.JSONDecoder().raw_decode(text)
        return obj, angle_used
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
                        return json.loads(text[start:i + 1]), angle_used
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

    # Split: reserve slots for both follow-ups (warm) and cold outreach (pipeline growth).
    # If one bucket is smaller than its slot count, unused slots go to the other bucket.
    followups = [p for p in prospects if prop_select(p, "Status") == "Emailed"]
    cold = [p for p in prospects if prop_select(p, "Status") == "Cold"]

    followup_slots = min(FOLLOWUP_LIMIT, len(followups))
    cold_slots = min(COLD_LIMIT, len(cold))
    # Redistribute unused slots
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

        # Auto-Lost: if this is a follow-up and it's been MAX_FOLLOWUP_DAYS+ since first contact,
        # stop sending and mark Lost instead.
        if is_followup:
            first_contact = prop_date(page, "Date Contacted")
            if first_contact and (date.today() - first_contact).days >= MAX_FOLLOWUP_DAYS:
                days_since = (date.today() - first_contact).days
                print(f"  -> {name} <{email}> MARK LOST ({days_since} days since first contact, no reply)")
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

    print(f"[{datetime.now().isoformat()}] done. sent={sent}, marked_lost={marked_lost}, errors={errors}")
    if errors > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
