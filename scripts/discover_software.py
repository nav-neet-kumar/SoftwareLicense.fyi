"""Token-conscious software discovery with local duplicate checks and ambiguity clarification."""
import difflib, hashlib, json, os, re, sys, time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
import pandas as pd
import requests
from google import genai

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "software_list.csv"
RECENT_PATH = ROOT / "data" / "recent_updates.json"
SOURCES_PATH = ROOT / "data" / "software_sources.json"
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MODEL_CHAIN = [m.strip() for m in os.getenv("GEMINI_MODELS", f"{MODEL},gemini-2.5-flash-lite,gemini-2.0-flash").split(",") if m.strip()]
MODEL_CHAIN = list(dict.fromkeys(MODEL_CHAIN))
TARGET = int(os.getenv("NEW_PER_RUN", "15"))
CATEGORIES = {"Open Source", "Free for Home", "License Required", "License for Commercial", "Community Version"}
COLUMNS = ["Software Name", "Category", "License Status", "Official URL", "Last Updated", "Notes"]
KEY = os.getenv("GEMINI_API_KEY", "")
CLIENT = genai.Client(api_key=KEY) if KEY else None
USAGE = {"requests": 0, "retries": 0, "prompt_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def parse_json(raw):
    text = str(raw or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", text)
        if not match:
            raise
        return json.loads(match.group(1))


def ask(prompt):
    if CLIENT is None:
        raise RuntimeError("GEMINI_API_KEY is missing")
    errors = []
    for model in MODEL_CHAIN:
        for attempt in range(2):
            try:
                USAGE["requests"] += 1
                response = CLIENT.models.generate_content(model=model, contents=prompt, config={"temperature": 0.1, "response_mime_type": "application/json"})
                usage = getattr(response, "usage_metadata", None)
                if usage:
                    USAGE["prompt_tokens"] += int(getattr(usage, "prompt_token_count", 0) or 0)
                    USAGE["output_tokens"] += int(getattr(usage, "candidates_token_count", 0) or 0)
                    USAGE["total_tokens"] += int(getattr(usage, "total_token_count", 0) or 0)
                if not getattr(response, "text", None):
                    raise ValueError("Gemini returned empty output")
                print(f"[MODEL] Success using {model}")
                return response.text.strip()
            except Exception as exc:
                message = str(exc).lower()
                errors.append(f"{model}: {exc}")
                if attempt == 0 and any(x in message for x in ("503", "unavailable", "timeout", "temporarily")):
                    USAGE["retries"] += 1
                    time.sleep(2)
                    continue
                print(f"[MODEL FALLBACK] {model} failed: {str(exc)[:180]}")
                break
    raise RuntimeError("All configured Gemini models failed. " + " | ".join(errors[-len(MODEL_CHAIN):])[:900])


def norm(value):
    value = str(value or "").casefold()
    value = re.sub(r"\b(incorporated|inc|corporation|corp|limited|ltd|llc|gmbh|company|co)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def domain(url):
    host = (urlparse(str(url or "")).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def local_match(candidate, rows):
    key = norm(candidate.get("software_name", ""))
    exact = [r for r in rows if norm(r["Software Name"]) == key]
    if exact:
        return "same", exact[:3]
    near = []
    for row in rows:
        other = norm(row["Software Name"])
        score = difflib.SequenceMatcher(None, key, other).ratio() if other else 0
        if score >= 0.76 or (len(key) >= 5 and (key in other or other in key)):
            near.append((score, row))
    near.sort(key=lambda x: x[0], reverse=True)
    return ("ambiguous", [r for _, r in near[:4]]) if near else ("new", [])


def clarify(candidate, matches):
    payload = {
        "candidate": {"name": candidate.get("software_name", ""), "url": candidate.get("official_url", ""), "notes": str(candidate.get("notes", ""))[:200]},
        "possible_existing_matches": [{"name": r["Software Name"], "url": r["Official URL"], "category": r["Category"], "notes": str(r["Notes"])[:140]} for r in matches],
    }
    prompt = """Are these the same software product? Similar names or shared vendor domains alone do not prove identity. Distinguish separate products such as Visual Studio and Visual Studio Code. Return JSON only: decision (SAME_PRODUCT, DIFFERENT_PRODUCT, UNCERTAIN), matched_name, reason. Input: """ + json.dumps(payload, ensure_ascii=False)
    answer = parse_json(ask(prompt))
    decision = str(answer.get("decision", "UNCERTAIN")).upper()
    if decision not in {"SAME_PRODUCT", "DIFFERENT_PRODUCT", "UNCERTAIN"}:
        decision = "UNCERTAIN"
    print(f"[CLARIFY] {candidate.get('software_name')}: {decision} — {str(answer.get('reason', ''))[:120]}")
    return decision


def discover(batch):
    prompt = f"""Suggest up to 24 real, distinct software products for a software-license directory (batch {batch}). Choose varied categories. Do not invent products, URLs, or license facts. Return a JSON array with software_name, category (one of {sorted(CATEGORIES)}), license_status, official_url (HTTPS), evidence_url, and notes. Use unknown if facts cannot be established. Python will compare candidates against the local CSV; do not request any existing-directory list."""
    data = parse_json(ask(prompt))
    if not isinstance(data, list):
        raise ValueError("Discovery response must be a JSON array")
    return [x for x in data if isinstance(x, dict)]


def verify_batch(candidates):
    compact = [{"software_name": c.get("software_name", ""), "official_url": c.get("official_url", ""), "suggested_category": c.get("category", ""), "notes": str(c.get("notes", ""))[:180], "evidence_url": c.get("evidence_url", "")} for c in candidates]
    prompt = """Verify these software records. Return a JSON array with one object per candidate: software_name, category, license_status, official_url, evidence_url, notes, confidence (high|medium|low). Category must be one of """ + json.dumps(sorted(CATEGORIES)) + """. Do not invent license terms or commercial-use permissions. Use high confidence only when evidence supports the status; keep notes concise. Candidates: """ + json.dumps(compact, ensure_ascii=False)
    data = parse_json(ask(prompt))
    if not isinstance(data, list):
        raise ValueError("Verification response must be a JSON array")
    return [x for x in data if isinstance(x, dict)]


def reachable(url):
    if not url.startswith("https://"):
        return False
    try:
        response = requests.get(url, timeout=8, allow_redirects=True, stream=True, headers={"User-Agent": "IsItFreeBot/1.0"})
        result = response.status_code < 400 and response.url.startswith("https://")
        response.close()
        return result
    except requests.RequestException:
        return False


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or hashlib.sha1(name.encode()).hexdigest()[:10]


def write_recent(rows):
    try:
        events = json.loads(RECENT_PATH.read_text(encoding="utf-8")) if RECENT_PATH.exists() else []
        if not isinstance(events, list):
            events = []
    except (OSError, json.JSONDecodeError):
        events = []
    try:
        sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8")) if SOURCES_PATH.exists() else {}
        if not isinstance(sources, dict):
            sources = {}
    except (OSError, json.JSONDecodeError):
        sources = {}
    for row in rows:
        slug = slugify(row["Software Name"])
        evidence = str(row.get("Evidence URL", "")).strip()
        events.append({"name": row["Software Name"], "slug": slug, "type": "NEW", "date": str(date.today()), "category": row["Category"], "url": row["Official URL"], "source": evidence})
        if evidence.startswith("https://"):
            sources[slug] = evidence
    RECENT_PATH.write_text(json.dumps(events[-100:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    SOURCES_PATH.write_text(json.dumps(sources, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run():
    print(f"Software discovery | model_fallback_chain={MODEL_CHAIN} | target={TARGET}")
    if CLIENT is None or not CSV_PATH.exists():
        print("[ERROR] Missing GEMINI_API_KEY or software CSV")
        return 1
    df = pd.read_csv(CSV_PATH, keep_default_na=False)
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        print(f"[ERROR] CSV schema missing columns: {missing}")
        return 1
    rows = df.to_dict("records")
    existing_keys = {norm(r["Software Name"]) for r in rows}
    accepted, accepted_keys, errors = [], set(), []
    for batch in range(1, 4):
        if len(accepted) >= TARGET:
            break
        try:
            candidates = discover(batch)
        except Exception as exc:
            errors.append(str(exc)); print(f"[ERROR] Discovery batch {batch}: {exc}")
            if "All configured Gemini models failed" in str(exc):
                break
            continue
        shortlist = []
        for candidate in candidates:
            name = str(candidate.get("software_name", "")).strip()
            url = str(candidate.get("official_url", "")).strip()
            key = norm(name)
            if not key or key in existing_keys or key in accepted_keys:
                print(f"[SKIP] Exact duplicate/invalid name: {name or '(empty)'}"); continue
            if not url.startswith("https://") or not domain(url):
                print(f"[SKIP] Invalid HTTPS URL: {name}"); continue
            status, matches = local_match(candidate, rows)
            if status == "same":
                print(f"[SKIP] Existing product: {name}"); continue
            if status == "ambiguous":
                print(f"[SKIP] Ambiguous possible duplicate; manual review needed: {name}")
                continue
            shortlist.append(candidate)
        if not shortlist:
            continue
        try:
            verified = verify_batch(shortlist)
        except Exception as exc:
            errors.append(str(exc)); print(f"[ERROR] Batch verification: {exc}")
            if any(x in str(exc).lower() for x in ("429", "resource_exhausted", "quota")):
                break
            continue
        verified_by_name = {norm(x.get("software_name", "")): x for x in verified}
        for candidate in shortlist:
            name = str(candidate.get("software_name", "")).strip()
            item = verified_by_name.get(norm(name))
            if not item:
                print(f"[SKIP] Verification omitted {name}"); continue
            category, url = str(item.get("category", "")).strip(), str(item.get("official_url", "")).strip()
            confidence = str(item.get("confidence", "")).lower().strip()
            status, notes = str(item.get("license_status", "")).strip(), str(item.get("notes", "")).strip()
            evidence = str(item.get("evidence_url", "")).strip()
            if category not in CATEGORIES or confidence != "high" or not status or not notes:
                print(f"[SKIP] Incomplete/low-confidence verification: {name}"); continue
            if domain(url) != domain(str(candidate.get("official_url", ""))) or not reachable(url):
                print(f"[SKIP] Official URL failed validation: {name}"); continue
            if not evidence.startswith("https://"):
                print(f"[SKIP] No official HTTPS licensing/pricing evidence URL: {name}"); continue
            accepted.append({"Software Name": name, "Category": category, "License Status": status[:240], "Official URL": url, "Last Updated": str(date.today()), "Notes": notes[:500], "Evidence URL": evidence})
            accepted_keys.add(norm(name))
            print(f"[ACCEPT] {name} ({category})")
            if len(accepted) >= TARGET:
                break
    print("[USAGE] " + " ".join(f"{k}={v}" for k, v in USAGE.items()))
    if not accepted:
        print(f"[ERROR] No new entries passed verification (target was {TARGET}); CSV unchanged.")
        if errors: print("[ERROR] Last API failure: " + errors[-1][:300])
        return 1
    if len(accepted) < TARGET:
        print(f"[PARTIAL] Verified {len(accepted)}/{TARGET}; saving safe entries instead of discarding them.")
    updated = pd.concat([df, pd.DataFrame(accepted, columns=COLUMNS)], ignore_index=True)
    tmp = CSV_PATH.with_suffix(".csv.tmp")
    updated.to_csv(tmp, index=False)
    tmp.replace(CSV_PATH)
    write_recent(accepted)
    print(f"[SUCCESS] Added {len(accepted)} verified entries; total={len(updated)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
