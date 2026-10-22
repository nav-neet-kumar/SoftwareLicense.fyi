"""Monthly AI audit of software data only: verify current licensing, update supported facts, and remove confirmed duplicates."""
import csv
import difflib
import html
import json
import os
import re
import shutil
import sys
import time
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests
from google import genai

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CSV_PATH = DATA / "software_list.csv"
SOURCES_PATH = DATA / "software_sources.json"
RECENT_PATH = DATA / "recent_updates.json"
CONTEXT_PATH = DATA / "software_context.json"
TODAY = str(date.today())
BATCH_SIZE = int(os.getenv("AUDIT_BATCH_SIZE", "25"))
MODELS = list(dict.fromkeys(m.strip() for m in os.getenv(
    "GEMINI_AUDIT_MODELS",
    "gemini-3.6-flash,gemini-2.5-flash,gemini-2.5-flash-lite"
).split(",") if m.strip()))
KEY = os.getenv("GEMINI_API_KEY", "")
CLIENT = genai.Client(api_key=KEY) if KEY else None
COLUMNS = ["Software Name", "Category", "License Status", "Official URL", "Last Updated", "Notes"]
ALLOWED_CATEGORIES = {"Open Source", "Free for Home", "License Required", "License for Commercial", "Community Version"}


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "nav", "footer", "header", "svg"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav", "footer", "header", "svg"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            value = re.sub(r"\s+", " ", data).strip()
            if value:
                self.parts.append(value)


def fetch_evidence(url):
    if not str(url).startswith("https://"):
        return {"url": str(url or ""), "ok": False, "text": ""}
    try:
        response = requests.get(str(url), timeout=8, allow_redirects=True,
                                headers={"User-Agent": "IsItFree-MonthlyAudit/1.0"})
        if response.status_code >= 400 or not response.url.startswith("https://"):
            return {"url": str(url), "ok": False, "text": ""}
        parser = TextExtractor()
        parser.feed(response.text[:500000])
        return {"url": response.url, "ok": True, "text": " ".join(parser.parts)[:1600]}
    except (requests.RequestException, ValueError):
        return {"url": str(url), "ok": False, "text": ""}


def ask_json(prompt):
    if CLIENT is None:
        raise RuntimeError("GEMINI_API_KEY is missing")
    errors = []
    for model in MODELS:
        try:
            response = CLIENT.models.generate_content(
                model=model,
                contents=prompt,
                config={"temperature": 0.1, "response_mime_type": "application/json"},
            )
            raw = (getattr(response, "text", None) or "").strip()
            if not raw:
                raise ValueError("Empty model response")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", raw)
                if not match:
                    raise
                data = json.loads(match.group(1))
            print(f"[MODEL] Audit batch succeeded using {model}")
            return data
        except Exception as exc:
            errors.append(f"{model}: {exc}")
            print(f"[MODEL FALLBACK] {model} failed: {str(exc)[:180]}")
    raise RuntimeError("All audit models failed: " + " | ".join(errors[-len(MODELS):])[:1000])


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")


def norm(name):
    value = str(name or "").casefold()
    value = re.sub(r"\b(incorporated|inc|corporation|corp|limited|ltd|llc|gmbh|company|co)\b", " ", value)
    value = re.sub(r"\b(community|ultimate|professional|enterprise|personal|home|pro|free|paid|standard|premium|edition|version)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def duplicate_candidates(rows):
    pairs = []
    for i, left in enumerate(rows):
        left_name = left.get("Software Name", "")
        left_key = norm(left_name)
        for j in range(i + 1, len(rows)):
            right = rows[j]
            right_key = norm(right.get("Software Name", ""))
            same_url = str(left.get("Official URL", "")).rstrip("/").casefold() == str(right.get("Official URL", "")).rstrip("/").casefold()
            ratio = difflib.SequenceMatcher(None, left_key, right_key).ratio() if left_key and right_key else 0
            if left_key == right_key or (same_url and ratio >= 0.55) or ratio >= 0.88:
                pairs.append({"left_index": i, "right_index": j,
                              "left": {k: left.get(k, "") for k in COLUMNS},
                              "right": {k: right.get(k, "") for k in COLUMNS}})
    return pairs


def resolve_duplicates(rows):
    pairs = duplicate_candidates(rows)
    confirmed = []
    # Keep prompts bounded; model must explicitly mark same product, not just same vendor.
    for start in range(0, len(pairs), 25):
        chunk = pairs[start:start + 25]
        if not chunk:
            continue
        prompt = (
            "You are deduplicating a software directory as of " + TODAY + ". "
            "Classify each pair as SAME_PRODUCT, DIFFERENT_PRODUCT, or UNCERTAIN. "
            "SAME_PRODUCT means the same underlying product listed under aliases or editions; "
            "different products from one vendor are DIFFERENT_PRODUCT (for example Visual Studio vs Visual Studio Code). "
            "Do not mark products duplicate merely because they share a vendor or URL. "
            "Return JSON array of objects with left_index, right_index, decision, canonical_name, reason. "
            "Use the provided pair indices exactly. Only use SAME_PRODUCT when confident. Pairs: "
            + json.dumps(chunk, ensure_ascii=False)
        )
        decisions = ask_json(prompt)
        if not isinstance(decisions, list):
            raise ValueError("Duplicate audit did not return a JSON array")
        for item in decisions:
            if not isinstance(item, dict) or str(item.get("decision", "")).upper() != "SAME_PRODUCT":
                continue
            try:
                i, j = int(item["left_index"]), int(item["right_index"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= i < len(rows) and 0 <= j < len(rows) and i < j:
                confirmed.append({"left": i, "right": j, "canonical_name": str(item.get("canonical_name", "")).strip(),
                                  "reason": str(item.get("reason", ""))[:300]})
    # Union-find prevents duplicate chains leaving aliases behind.
    parent = list(range(len(rows)))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[b] = a
    for item in confirmed:
        union(item["left"], item["right"])
    groups = {}
    for i in range(len(rows)):
        groups.setdefault(find(i), []).append(i)
    duplicate_groups = [indices for indices in groups.values() if len(indices) > 1]
    removed = set()
    for indices in duplicate_groups:
        # Prefer the established, shorter canonical name and older established record.
        # Prefer the oldest established listing; use shorter normalized name only as a tie-breaker.
        keep = sorted(indices, key=lambda i: (
            str(rows[i].get("Last Updated", "9999-99-99") or "9999-99-99"),
            len(norm(rows[i].get("Software Name", ""))),
            i
        ))[0]
        for i in indices:
            if i != keep:
                removed.add(i)
                print(f"[DUPLICATE REMOVED] {rows[i].get('Software Name')} -> kept {rows[keep].get('Software Name')}")
    return removed


def audit_rows(rows):
    changes = 0
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start + BATCH_SIZE]
        supplied = []
        for offset, row in enumerate(batch):
            evidence_url = str(row.get("Evidence URL", "")).strip()
            official = fetch_evidence(row.get("Official URL", ""))
            evidence = fetch_evidence(evidence_url) if evidence_url else {"url": "", "ok": False, "text": ""}
            supplied.append({
                "row_index": start + offset,
                "record": {k: row.get(k, "") for k in COLUMNS},
                "official_page": official,
                "license_evidence_page": evidence,
            })
        prompt = (
            "Audit these software directory records as of " + TODAY + ". Use only the supplied page text. "
            "Do not infer a license from a product homepage if the licensing terms are absent. "
            "If evidence is missing, inaccessible, stale, ambiguous, or insufficient, action must be KEEP_UNCHANGED. "
            "For clear, current evidence, action may be UPDATE with corrected category, license_status, official_url, notes, "
            "evidence_url and a concise reason. Never change a product name during this stage. "
            "Only update facts directly supported by supplied page text and use HTTPS URLs. "
            "Allowed categories: " + json.dumps(sorted(ALLOWED_CATEGORIES)) + ". "
            "Return JSON array with row_index, action (UPDATE|KEEP_UNCHANGED), category, license_status, official_url, "
            "evidence_url, notes, reason, confidence (high|medium|low). Only use UPDATE with high confidence and direct evidence. "
            "Records: " + json.dumps(supplied, ensure_ascii=False)
        )
        result = ask_json(prompt)
        if not isinstance(result, list):
            raise ValueError("Data audit did not return a JSON array")
        by_index = {int(x["row_index"]): x for x in result if isinstance(x, dict) and str(x.get("row_index", "")).isdigit()}
        for offset, row in enumerate(batch):
            item = by_index.get(start + offset)
            if not item or str(item.get("action", "")).upper() != "UPDATE" or str(item.get("confidence", "")).lower() != "high":
                continue
            category = str(item.get("category", "")).strip()
            status = str(item.get("license_status", "")).strip()
            official_url = str(item.get("official_url", "")).strip()
            evidence_url = str(item.get("evidence_url", "")).strip()
            notes = str(item.get("notes", "")).strip()
            if category not in ALLOWED_CATEGORIES or not status or not notes:
                continue
            if not official_url.startswith("https://") or not evidence_url.startswith("https://"):
                continue
            # Evidence URL must be reachable in this run; do not accept a model-invented citation.
            evidence_check = fetch_evidence(evidence_url)
            if not evidence_check["ok"]:
                continue
            changed = False
            for key, value in (("Category", category), ("License Status", status[:240]),
                               ("Official URL", official_url), ("Notes", notes[:500])):
                if str(row.get(key, "")) != value:
                    row[key] = value
                    changed = True
            if str(row.get("Evidence URL", "")) != evidence_url:
                row["Evidence URL"] = evidence_url
                changed = True
            if changed:
                row["Last Updated"] = TODAY
                changes += 1
                print(f"[UPDATED] {row.get('Software Name')}: {str(item.get('reason', 'verified correction'))[:180]}")
    return changes


def main():
    if not KEY or CLIENT is None:
        print("[ERROR] GEMINI_API_KEY is missing")
        return 1
    if not CSV_PATH.exists():
        print("[ERROR] software_list.csv is missing")
        return 1
    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    missing = [name for name in COLUMNS if name not in fieldnames]
    if missing:
        print(f"[ERROR] CSV is missing required columns: {missing}")
        return 1
    # Ensure optional evidence data can be preserved in the same CSV schema.
    if "Evidence URL" not in fieldnames:
        fieldnames.append("Evidence URL")
        for row in rows:
            row["Evidence URL"] = ""
    print(f"[START] Monthly audit date={TODAY}; rows={len(rows)}; models={MODELS}")
    # Audit first. If any API call fails, abort without writing partial changes.
    try:
        changed = audit_rows(rows)
        duplicates = resolve_duplicates(rows)
    except Exception as exc:
        print(f"[ERROR] Audit aborted without writing files: {exc}")
        return 1
    retained = [row for i, row in enumerate(rows) if i not in duplicates]
    removed_names = {str(rows[i].get("Software Name", "")) for i in duplicates}
    # Preserve data sidecars while removing stale keys for confirmed duplicate aliases.
    try:
        sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8")) if SOURCES_PATH.exists() else {}
        if not isinstance(sources, dict):
            sources = {}
    except (OSError, json.JSONDecodeError):
        sources = {}
    try:
        recent = json.loads(RECENT_PATH.read_text(encoding="utf-8")) if RECENT_PATH.exists() else []
        if not isinstance(recent, list):
            recent = []
    except (OSError, json.JSONDecodeError):
        recent = []
    try:
        contexts = json.loads(CONTEXT_PATH.read_text(encoding="utf-8")) if CONTEXT_PATH.exists() else {}
        if not isinstance(contexts, dict):
            contexts = {}
    except (OSError, json.JSONDecodeError):
        contexts = {}
    retained_slugs = {slug(r.get("Software Name", "")) for r in retained}
    sources = {k: v for k, v in sources.items() if k in retained_slugs}
    contexts = {k: v for k, v in contexts.items() if k in retained_slugs}
    recent = [event for event in recent if slug(event.get("name", "")) in retained_slugs]
    # Write files only after all model calls have succeeded.
    tmp = CSV_PATH.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(retained)
    tmp.replace(CSV_PATH)
    SOURCES_PATH.write_text(json.dumps(sources, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    CONTEXT_PATH.write_text(json.dumps(contexts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    RECENT_PATH.write_text(json.dumps(recent[-100:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[SUCCESS] Audited {len(rows)} records; updated={changed}; duplicate_rows_removed={len(duplicates)}; remaining={len(retained)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
