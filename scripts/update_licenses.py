"""
update_licenses.py
AI-Powered Software License Directory — Auto-Updater
Uses an AI model to verify and update software licensing information.

Supports: OpenAI (GPT), Google Generative AI (Gemini), or Anthropic (Claude)
Set AI_PROVIDER env var to: "openai", "gemini", or "anthropic"
"""

import os
import sys
import json
import time
import re
import difflib
import requests
import pandas as pd
from datetime import date
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

AI_PROVIDER   = os.getenv("AI_PROVIDER", "openai")   # openai | gemini | anthropic
OPENAI_KEY    = os.getenv("OPENAI_API_KEY", "")
GEMINI_KEY    = os.getenv("GEMINI_API_KEY", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

ROOT = Path(__file__).parent.parent
CSV_PATH = ROOT / "data" / "software_list.csv"
RECENT_PATH = ROOT / "data" / "recent_updates.json"

VALID_CATEGORIES = [
    "Open Source",
    "Free for Home",
    "License Required",
    "License for Commercial",
    "Community Version",
]

# ── AI Prompt ─────────────────────────────────────────────────────────────────

def build_prompt(software_name: str, official_url: str) -> str:
    return f"""
You are a software licensing expert. Your task is to look up the current licensing
status of "{software_name}" (official site: {official_url}) and return a structured JSON response.

Classify the software into EXACTLY ONE of these five categories:
1. "Open Source"           — Full source code available, permissive or copyleft license (MIT, GPL, Apache, etc.)
2. "Free for Home"         — Free for personal/home use only; commercial use requires payment
3. "License Required"      — Requires a paid license for any use (individual or business)
4. "License for Commercial" — Free or low-cost personal use, but a separate commercial license is needed for business
5. "Community Version"     — A free community/open-core edition exists alongside paid enterprise tiers

Return ONLY valid JSON, no markdown, no explanation:
{{
  "software_name": "{software_name}",
  "category": "<one of the 5 categories above>",
  "license_status": "<concise human-readable status, e.g. 'Free - Open Source' or 'Subscription Required'>",
  "notes": "<one sentence summarising the key licensing detail or limitation>",
  "confidence": "<high|medium|low>"
}}
"""

# ── AI Provider Wrappers ───────────────────────────────────────────────────────

def query_openai(prompt: str) -> dict:
    import openai
    client = openai.OpenAI(api_key=OPENAI_KEY)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=300,
    )
    return json.loads(response.choices[0].message.content)


def query_gemini(prompt: str) -> dict:
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_KEY)
    model = genai.GenerativeModel("gemini-1.5-flash")
    response = model.generate_content(prompt)
    text = response.text.strip().lstrip("```json").rstrip("```").strip()
    return json.loads(text)


def query_anthropic(prompt: str) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    return json.loads(text)


def query_ai(software_name: str, official_url: str) -> dict | None:
    prompt = build_prompt(software_name, official_url)
    try:
        if AI_PROVIDER == "openai":
            return query_openai(prompt)
        elif AI_PROVIDER == "gemini":
            return query_gemini(prompt)
        elif AI_PROVIDER == "anthropic":
            return query_anthropic(prompt)
        else:
            print(f"[ERROR] Unknown AI_PROVIDER: {AI_PROVIDER}")
            return None
    except json.JSONDecodeError as e:
        print(f"  [WARN] JSON parse error for {software_name}: {e}")
        return None
    except Exception as e:
        print(f"  [ERROR] AI query failed for {software_name}: {e}")
        return None


# ── URL Reachability Check ─────────────────────────────────────────────────────

def is_url_reachable(url: str, timeout: int = 8) -> bool:
    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True)
        return resp.status_code < 400
    except Exception:
        return False


# ── Core Update Logic ──────────────────────────────────────────────────────────

def notes_materially_changed(old: str, new: str) -> bool:
    old_text = re.sub(r"\s+", " ", str(old or "")).strip().casefold()
    new_text = re.sub(r"\s+", " ", str(new or "")).strip().casefold()
    if old_text == new_text:
        return False
    critical = re.compile(r"\b(not|no|only|commercial|business|personal|home|free|paid|trial|subscription|license|licence|limit|watermark|seat|user|gb|monthly|annual|open|closed|deprecated|discontinued|enterprise|community|requires|required|allows|allowed|prohibited|permitted|unlimited|restricted|source)\b")
    if set(critical.findall(old_text)) != set(critical.findall(new_text)):
        return True
    return difflib.SequenceMatcher(None, old_text, new_text).ratio() < 0.82


def update_row(row: pd.Series) -> pd.Series:
    name = row["Software Name"]
    url  = row.get("Official URL", "")

    print(f"  Checking: {name}")

    # Verify URL is reachable
    if url and not is_url_reachable(url):
        print(f"    [WARN] URL unreachable: {url}")

    result = query_ai(name, url)

    if result is None:
        print(f"    [SKIP] No AI result — keeping existing data")
        return row

    # Validate category
    category = result.get("category", row["Category"])
    if category not in VALID_CATEGORIES:
        print(f"    [WARN] Invalid category '{category}' — keeping original")
        category = row["Category"]

    confidence = str(result.get("confidence", "?")).lower().strip()
    if confidence != "high":
        print(f"    [SKIP] Confidence is {confidence or 'unknown'}; preserving current record")
        return row

    old_category = str(row.get("Category", ""))
    old_status = str(row.get("License Status", ""))
    old_notes = str(row.get("Notes", ""))
    new_status = str(result.get("license_status", old_status) or old_status).strip()
    suggested_notes = str(result.get("notes", old_notes) or old_notes).strip()
    category_changed = category != old_category
    status_changed = new_status != old_status
    notes_changed = notes_materially_changed(old_notes, suggested_notes)

    row["Category"] = category
    row["License Status"] = new_status
    # Avoid rewriting notes on every run for cosmetic paraphrases. Apply them when
    # licensing classification/status changes or when the notes change materially.
    if category_changed or status_changed or notes_changed:
        row["Notes"] = suggested_notes
    else:
        row["Notes"] = old_notes

    after = (str(row.get("Category", "")), str(row.get("License Status", "")), str(row.get("Notes", "")))
    before = (old_category, old_status, old_notes)
    if after != before:
        row["Last Updated"] = str(date.today())

    print(f"    → {category} | {row['License Status']} (confidence: {confidence})")
    return row


def run():
    print("=" * 60)
    print(f"AI License Updater  |  Provider: {AI_PROVIDER.upper()}")
    print(f"CSV: {CSV_PATH}")
    print("=" * 60)

    if not CSV_PATH.exists():
        print(f"[ERROR] CSV not found at {CSV_PATH}")
        sys.exit(1)

    df = pd.read_csv(CSV_PATH)
    print(f"\nLoaded {len(df)} software entries.\n")

    updated_rows = []
    recent_events = []
    for i, row in df.iterrows():
        original = row.copy()
        updated_row = update_row(row)
        updated_rows.append(updated_row)
        changed = any(str(original.get(field, "")) != str(updated_row.get(field, "")) for field in ("Category", "License Status", "Notes", "Official URL"))
        if changed:
            name = str(updated_row.get("Software Name", "")).strip()
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            recent_events.append({
                "name": name, "slug": slug, "type": "UPDATED", "date": str(date.today()),
                "category": str(updated_row.get("Category", "")), "url": str(updated_row.get("Official URL", "")),
            })
        time.sleep(1.2)  # Respect API rate limits

    updated_df = pd.DataFrame(updated_rows)
    updated_df.to_csv(CSV_PATH, index=False)
    if recent_events:
        try:
            existing_events = json.loads(RECENT_PATH.read_text(encoding="utf-8")) if RECENT_PATH.exists() else []
            if not isinstance(existing_events, list):
                existing_events = []
        except (OSError, json.JSONDecodeError):
            existing_events = []
        RECENT_PATH.write_text(json.dumps((existing_events + recent_events)[-100:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nDone. Saved CSV to {CSV_PATH}")
    print(f"   Processed {len(updated_df)} entries; materially changed {len(recent_events)} on {date.today()}")


if __name__ == "__main__":
    run()
