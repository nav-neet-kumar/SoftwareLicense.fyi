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
# No record quota; limits below constrain resource consumption only.
TARGET = 0  # Legacy compatibility; intentionally unused.
MAX_DISCOVERY_BATCHES = max(1, int(os.getenv("MAX_DISCOVERY_BATCHES", "20")))
MAX_EMPTY_BATCHES = max(1, int(os.getenv("MAX_EMPTY_BATCHES", "3")))
STATE_PATH = ROOT / "data" / "discovery_state.json"
REPORT_PATH = ROOT / "data" / "discovery_report.json"
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


def ask(prompt, models=None):
    if CLIENT is None:
        raise RuntimeError("GEMINI_API_KEY is missing")
    errors = []
    for model in (models or MODEL_CHAIN):
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


ALIAS_GROUPS = [
    {"vs code", "visual studio code", "vscode"},
    {"vlc", "vlc media player"},
    {"gimp", "gnu image manipulation program"},
    {"firefox", "mozilla firefox"},
    {"google chrome", "chrome"},
    {"7 zip", "7zip"},
    {"libreoffice", "libre office"},
]
ALIAS_LOOKUP = {norm(alias): norm(max(group, key=len))
                for group in ALIAS_GROUPS for alias in group}


def canonical_name(value):
    key = norm(value)
    return ALIAS_LOOKUP.get(key, key)


def canonical_url(url):
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+", "/", parsed.path or "").rstrip("/")
    return host + path


def local_match(candidate, rows):
    key = canonical_name(candidate.get("software_name", ""))
    url_key = canonical_url(candidate.get("official_url", ""))
    exact, near = [], []
    for row in rows:
        other = canonical_name(row.get("Software Name", ""))
        existing_url = canonical_url(row.get("Official URL", ""))
        if key and key == other:
            exact.append(row)
            continue
        if url_key and existing_url and url_key == existing_url:
            exact.append(row)
            continue
        score = difflib.SequenceMatcher(None, key, other).ratio() if key and other else 0
        if score >= 0.78 or (len(key) >= 6 and (key in other or other in key)):
            near.append((score, row))
    if exact:
        return "same", exact[:4]
    near.sort(key=lambda item: item[0], reverse=True)
    return ("ambiguous", [row for _, row in near[:4]]) if near else ("new", [])



def clarify(candidate, matches):
    payload = {
        "candidate": {"name": candidate.get("software_name", ""), "url": candidate.get("official_url", ""),
                      "publisher": candidate.get("publisher", ""),
                      "description": str(candidate.get("description", candidate.get("notes", "")))[:180]},
        "possible_existing_matches": [
            {"name": r.get("Software Name", ""), "url": r.get("Official URL", ""),
             "category": r.get("Category", ""), "notes": str(r.get("Notes", ""))[:100]}
            for r in matches
        ],
    }
    prompt = """Determine whether the candidate is the same software product as any existing match.
Names, abbreviations, aliases and renamed products can refer to the same product.
Do not mark separate products as duplicates merely because they share a vendor or domain.
Examples: VS Code = Visual Studio Code; Visual Studio != Visual Studio Code; VSCodium is a distinct distribution.
Return JSON only: decision (SAME_PRODUCT, DIFFERENT_PRODUCT, UNCERTAIN), matched_name, reason.
When uncertain, choose UNCERTAIN. Input: """ + json.dumps(payload, ensure_ascii=False)
    # Use the last configured model as the dedicated final duplicate adjudicator;
    # if unavailable, ask() falls back through the remaining configured models.
    duplicate_chain = [MODEL_CHAIN[-1]] + list(reversed(MODEL_CHAIN[:-1])) if MODEL_CHAIN else None
    answer = parse_json(ask(prompt, models=duplicate_chain))
    decision = str(answer.get("decision", "UNCERTAIN")).upper()
    if decision not in {"SAME_PRODUCT", "DIFFERENT_PRODUCT", "UNCERTAIN"}:
        decision = "UNCERTAIN"
    print(f"[CLARIFY] {candidate.get('software_name')}: {decision} — {str(answer.get('reason', ''))[:120]}")
    return decision


def discover(batch, existing_names=()):
    # Send names only, not the full catalog records, to control token usage.
    names = list(dict.fromkeys(str(n).strip() for n in existing_names if str(n).strip()))
    catalog_context = json.dumps(names, ensure_ascii=False, separators=(",", ":"))
    prompt = f"""Discover genuinely new software products for IsItFree? (batch {batch}).
Return up to 30 real, distinct applications, software tools, desktop/mobile apps, SaaS products, or developer/admin utilities.
Existing catalog names (aliases may differ): {catalog_context}
STRICT RULES:
- Do not repeat existing products under abbreviations, alternate spellings, former names, vendor-prefixed names, or renamed names.
- Examples: VS Code = Visual Studio Code; VLC = VLC Media Player; Firefox = Mozilla Firefox; GIMP = GNU Image Manipulation Program.
- Distinct products from the same vendor are allowed: Visual Studio is not Visual Studio Code; VSCodium is a distinct distribution.
- Do not return multiple editions of one product as separate entries unless they are genuinely distinct catalog-worthy products.
- Prefer diverse, lesser-known products not already in the catalog.
- Never invent product names, official URLs, publishers, or licensing evidence.
Return JSON array only. Each item: software_name, official_url (HTTPS), publisher, description, category (one of {sorted(CATEGORIES)}), license_status, evidence_url, notes.
If licensing facts are uncertain, use unknown and provide the official product URL; verification happens separately."""
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


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_state():
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state_and_report(pending, report):
    report["pending_candidates"] = len(pending)
    report["token_usage"] = dict(USAGE)
    atomic_json(STATE_PATH, {"pending_candidates": pending, "updated_at": str(date.today())})
    atomic_json(REPORT_PATH, report)


def verify_resilient(candidates):
    """Fallback is handled by ask(); split failed batches so one bad response doesn't block peers."""
    if not candidates:
        return [], []
    try:
        return verify_batch(candidates), []
    except Exception as exc:
        if len(candidates) == 1:
            print(f"[PENDING] Verification failed for {candidates[0].get('software_name')}: {str(exc)[:160]}")
            return [], candidates
        midpoint = max(1, len(candidates) // 2)
        print(f"[VERIFY SPLIT] Batch of {len(candidates)} failed; retrying smaller batches.")
        left_ok, left_pending = verify_resilient(candidates[:midpoint])
        right_ok, right_pending = verify_resilient(candidates[midpoint:])
        return left_ok + right_ok, left_pending + right_pending


def persist_accepted(accepted):
    if not accepted:
        return
    df = pd.read_csv(CSV_PATH, keep_default_na=False)
    if "Evidence URL" not in df.columns:
        df["Evidence URL"] = ""
    for row in accepted:
        if "Last Verified Date" in df.columns and not row.get("Last Verified Date"):
            row["Last Verified Date"] = str(date.today())
        for column in df.columns:
            row.setdefault(column, "")
    updated = pd.concat([df, pd.DataFrame(accepted).reindex(columns=df.columns)], ignore_index=True)
    tmp = CSV_PATH.with_suffix(".csv.tmp")
    updated.to_csv(tmp, index=False)
    tmp.replace(CSV_PATH)
    write_recent(accepted)


def run():
    report = {
        "workflow": "Daily Software Discovery",
        "date": str(date.today()),
        "status": "running",
        "batches_attempted": 0,
        "candidates_discovered": 0,
        "duplicates_skipped": 0,
        "review_required": 0,
        "rejected": 0,
        "added": 0,
        "model_errors": [],
        "review_candidates": [],
        "token_usage": {},
    }
    old_state = load_state()
    pending = list(old_state.get("pending_candidates", [])) if isinstance(old_state.get("pending_candidates", []), list) else []
    if CLIENT is None or not CSV_PATH.exists():
        report["status"] = "technical_failure"
        report["model_errors"].append("Missing GEMINI_API_KEY or software CSV")
        save_state_and_report(pending, report)
        print("[ERROR] Missing GEMINI_API_KEY or software CSV")
        return 1

    df = pd.read_csv(CSV_PATH, keep_default_na=False)
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        report["status"] = "technical_failure"
        report["model_errors"].append(f"CSV schema missing columns: {missing}")
        save_state_and_report(pending, report)
        print(f"[ERROR] CSV schema missing columns: {missing}")
        return 1

    rows = df.to_dict("records")
    accepted_keys = set()
    technical_errors = []
    successful_discovery_calls = 0
    no_progress_batches = 0

    def process_batch(candidates):
        nonlocal rows, pending
        report["candidates_discovered"] += len(candidates)
        shortlist, batch_keys = [], set()
        for candidate in candidates:
            name = str(candidate.get("software_name", "")).strip()
            key = canonical_name(name)
            url = str(candidate.get("official_url", "")).strip()
            if not key or not url.startswith("https://") or not domain(url):
                report["rejected"] += 1
                print(f"[SKIP] Invalid name or HTTPS URL: {name or '(empty)'}")
                continue
            if key in batch_keys or key in accepted_keys:
                report["duplicates_skipped"] += 1
                print(f"[SKIP] Duplicate in current run: {name}")
                continue
            batch_keys.add(key)
            match_status, matches = local_match(candidate, rows)
            if match_status == "same":
                report["duplicates_skipped"] += 1
                print(f"[SKIP] Existing product/alias: {name}")
                continue
            if match_status == "ambiguous":
                try:
                    decision = clarify(candidate, matches)
                except Exception as exc:
                    technical_errors.append(str(exc))
                    pending.append(candidate)
                    print(f"[PENDING] Could not resolve possible duplicate: {name}")
                    continue
                if decision == "SAME_PRODUCT":
                    report["duplicates_skipped"] += 1
                    continue
                if decision == "UNCERTAIN":
                    report["review_required"] += 1
                    report["review_candidates"].append({"name": name, "url": url, "reason": "Product identity uncertain"})
                    continue
            shortlist.append(candidate)

        verified, failed = verify_resilient(shortlist)
        pending.extend(failed)
        verified_by_name = {}
        for item in verified:
            verified_by_name.setdefault(canonical_name(item.get("software_name", "")), item)

        added_now = []
        for candidate in shortlist:
            name = str(candidate.get("software_name", "")).strip()
            key = canonical_name(name)
            item = verified_by_name.get(key)
            if not item:
                if candidate not in pending:
                    pending.append(candidate)
                continue
            category = str(item.get("category", "")).strip()
            url = str(item.get("official_url", "")).strip()
            license_status = str(item.get("license_status", "")).strip()
            notes = str(item.get("notes", "")).strip()
            evidence = str(item.get("evidence_url", "")).strip()
            confidence = str(item.get("confidence", "")).lower().strip()
            if category not in CATEGORIES or confidence != "high" or not license_status or not notes:
                report["rejected"] += 1
                print(f"[SKIP] Incomplete or low-confidence verification: {name}")
                continue
            if domain(url) != domain(str(candidate.get("official_url", ""))) or not reachable(url):
                report["rejected"] += 1
                print(f"[SKIP] Official URL failed validation: {name}")
                continue
            if not evidence.startswith("https://") or not domain(evidence):
                report["rejected"] += 1
                print(f"[SKIP] No HTTPS license/evidence URL: {name}")
                continue
            final_status, final_matches = local_match({"software_name": name, "official_url": url}, rows + added_now)
            if final_status == "same":
                report["duplicates_skipped"] += 1
                continue
            if final_status == "ambiguous":
                try:
                    decision = clarify(candidate, final_matches)
                except Exception as exc:
                    technical_errors.append(str(exc))
                    pending.append(candidate)
                    continue
                if decision != "DIFFERENT_PRODUCT":
                    report["review_required"] += 1
                    report["review_candidates"].append({"name": name, "url": url, "reason": "Final duplicate check not confident"})
                    continue
            row = {
                "Software Name": name, "Category": category,
                "License Status": license_status[:240], "Official URL": url,
                "Last Updated": str(date.today()), "Notes": notes[:500],
                "Evidence URL": evidence,
            }
            if "Last Verified Date" in df.columns:
                row["Last Verified Date"] = str(date.today())
            added_now.append(row)
            accepted_keys.add(key)
            print(f"[ACCEPT] {name} ({category})")

        if added_now:
            persist_accepted(added_now)
            rows.extend(added_now)
            report["added"] += len(added_now)
        save_state_and_report(pending, report)
        return len(added_now)

    # Resume pending candidates before asking AI to discover more.
    if pending:
        old_pending, pending = pending, []
        print(f"[RESUME] Retrying {len(old_pending)} candidates left by a previous run.")
        process_batch(old_pending)

    for batch in range(1, MAX_DISCOVERY_BATCHES + 1):
        report["batches_attempted"] = batch
        try:
            candidates = discover(batch, [row.get("Software Name", "") for row in rows])
            successful_discovery_calls += 1
        except Exception as exc:
            technical_errors.append(str(exc))
            report["model_errors"].append(str(exc)[:300])
            print(f"[ERROR] Discovery batch {batch}: {str(exc)[:220]}")
            save_state_and_report(pending, report)
            break
        added = process_batch(candidates)
        no_progress_batches = no_progress_batches + 1 if added == 0 else 0
        if no_progress_batches >= MAX_EMPTY_BATCHES:
            print(f"[STOP] {MAX_EMPTY_BATCHES} consecutive batches produced no new verified entries.")
            break

    if technical_errors:
        report["model_errors"].extend(error[:300] for error in technical_errors[-5:])
    if report["added"] > 0:
        report["status"] = "partial_with_errors" if technical_errors else "success"
    elif successful_discovery_calls > 0:
        report["status"] = "no_additions"
    else:
        report["status"] = "technical_failure"
    report["completed_at"] = str(date.today())
    save_state_and_report(pending, report)
    print("[REPORT] " + json.dumps({
        "status": report["status"], "added": report["added"],
        "duplicates_skipped": report["duplicates_skipped"], "rejected": report["rejected"],
        "review_required": report["review_required"], "pending": len(pending),
        "batches_attempted": report["batches_attempted"], "token_usage": report["token_usage"],
    }))
    # Zero additions is a valid result; fail only when discovery could not run at all.
    return 1 if report["status"] == "technical_failure" else 0


if __name__ == "__main__":
    raise SystemExit(run())
