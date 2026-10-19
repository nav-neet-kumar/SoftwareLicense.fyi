"""Generate crawlable static software pages with cached AI context and shared site chrome."""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "software_list.csv"
CONTEXT_PATH = ROOT / "data" / "software_context.json"
SOURCES_PATH = ROOT / "data" / "software_sources.json"
INDEX_PATH = ROOT / "index.html"
OUTPUT_DIR = ROOT / "software"
SITEMAP_PATH = ROOT / "sitemap.xml"
SITE = "https://isitfree.webline.cloud"
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
BATCH_SIZE = int(os.getenv("PAGE_CONTEXT_BATCH_SIZE", "15"))
MAX_BATCHES = int(os.getenv("PAGE_CONTEXT_BATCHES_PER_RUN", "2"))


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return slug or hashlib.sha1(str(name).encode("utf-8")).hexdigest()[:10]


def esc(value):
    return html.escape(str(value or ""), quote=True)


def parse_json(raw):
    text = str(raw or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", text)
        if not match:
            raise
        return json.loads(match.group(1))


def input_hash(row):
    fields = [row.get("Software Name", ""), row.get("Category", ""), row.get("License Status", ""), row.get("Official URL", ""), row.get("Notes", ""), row.get("Evidence URL", "")]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode("utf-8")).hexdigest()


def fallback_context(row):
    name = str(row.get("Software Name", "Software"))
    category = str(row.get("Category", "Unclassified"))
    status = str(row.get("License Status", "Not verified"))
    notes = str(row.get("Notes", "")).strip()
    summary = notes or f"{name} is listed in the {category} category. Check the vendor's current license and pricing terms before relying on its availability or permitted uses."
    return {
        "summary": summary,
        "what_it_does": f"{name} is listed in the IsItFree? software directory. Visit the official website for product-specific capabilities and current documentation.",
        "best_for": "Review the official product description to confirm it fits your workflow, operating system, and organization.",
        "free_tier": status,
        "limitations": "Free-tier limits, trial restrictions, and included features can change. Confirm the current details with the vendor.",
        "licensing_notes": notes or "The directory has no additional verified licensing note for this entry. Review the official license or terms before commercial use.",
        "faq": [
            {"question": f"Is {name} free?", "answer": f"The directory currently classifies it as {category}: {status}. Check the official source for current terms."},
            {"question": "Can I use it commercially?", "answer": "Do not assume commercial permission from a free download or trial. Confirm the applicable license and terms with the vendor."}
        ],
    }


def load_contexts():
    try:
        data = json.loads(CONTEXT_PATH.read_text(encoding="utf-8")) if CONTEXT_PATH.exists() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def generate_context_batches(rows, contexts, max_batches):
    missing = [r for r in rows if contexts.get(slugify(r["Software Name"]), {}).get("input_hash") != input_hash(r)]
    if not missing:
        print("[AI CONTEXT] All software contexts are cached and current.")
        return contexts
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        print("[WARN] GEMINI_API_KEY unavailable; generating pages with CSV-derived fallback context.")
        return contexts
    try:
        from google import genai
        client = genai.Client(api_key=key)
    except Exception as exc:
        print(f"[WARN] Could not initialize Gemini: {exc}")
        return contexts

    completed_batches = 0
    attempted_batches = 0
    size = max(1, BATCH_SIZE)
    # With two batches, refresh the newest entries first and work backwards through older entries too.
    batch_groups = []
    if max_batches <= 1:
        batch_groups = [missing[:size]]
    elif len(missing) <= size:
        batch_groups = [missing]
    elif len(missing) <= 2 * size:
        batch_groups = [missing[:size], missing[size:]]
    else:
        batch_groups = [missing[:size], missing[-size:]]
        if max_batches > 2:
            middle = missing[size:-size]
            for offset in range(0, len(middle), size):
                batch_groups.append(middle[offset:offset + size])
    for batch in batch_groups:
        if attempted_batches >= max_batches:
            break
        attempted_batches += 1
        payload = [{
            "software_name": r["Software Name"], "category": r["Category"],
            "license_status": r["License Status"], "official_url": r["Official URL"], "evidence_url": r.get("Evidence URL", ""),
            "existing_notes": str(r["Notes"])[:350],
        } for r in batch]
        prompt = """Create accurate, concise, product-specific page context for each software record in this JSON input.
Use the provided notes as unverified hints, not as unquestionable facts. Do not invent pricing, platform support, license
permissions, free-tier limits, or features. When the provided information is insufficient, explicitly say the visitor should
check the official documentation. Return JSON array with one object per input, preserving software_name exactly, and fields:
software_name, summary (1-2 sentences), what_it_does, best_for, free_tier, limitations, licensing_notes, faq (2 objects with
question and answer). Each text field should be concise and useful for a reader. Do not include unsupported claims.
Records: """ + json.dumps(payload, ensure_ascii=False)
        response = None
        for attempt in range(3):
            try:
                response = client.models.generate_content(model=MODEL, contents=prompt, config={"temperature": 0.2, "response_mime_type": "application/json"})
                break
            except Exception as exc:
                message = str(exc).lower()
                if attempt == 2 or not any(x in message for x in ("429", "503", "unavailable", "resource_exhausted", "rate limit", "timeout")):
                    print(f"[WARN] AI context batch failed: {exc}")
                    response = None
                    break
                time.sleep(3 * (2 ** attempt))
        if not response or not getattr(response, "text", None):
            continue
        try:
            results = parse_json(response.text)
            if not isinstance(results, list):
                raise ValueError("Gemini response is not an array")
        except Exception as exc:
            print(f"[WARN] Invalid context JSON: {exc}")
            continue
        by_name = {str(item.get("software_name", "")).casefold().strip(): item for item in results if isinstance(item, dict)}
        for row in batch:
            slug = slugify(row["Software Name"])
            item = by_name.get(row["Software Name"].casefold().strip())
            if not item:
                continue
            required = ("summary", "what_it_does", "best_for", "free_tier", "limitations", "licensing_notes")
            if any(not str(item.get(field, "")).strip() for field in required):
                print(f"[WARN] Incomplete AI context for {row['Software Name']}; will use safe fallback.")
                continue
            faq = item.get("faq", [])
            contexts[slug] = {
                "input_hash": input_hash(row),
                "generated_at": str(date.today()),
                "summary": str(item["summary"])[:700],
                "what_it_does": str(item["what_it_does"])[:700],
                "best_for": str(item["best_for"])[:500],
                "free_tier": str(item["free_tier"])[:500],
                "limitations": str(item["limitations"])[:600],
                "licensing_notes": str(item["licensing_notes"])[:700],
                "faq": [{"question": str(x.get("question", ""))[:180], "answer": str(x.get("answer", ""))[:500]} for x in faq[:4] if isinstance(x, dict) and x.get("question") and x.get("answer")],
            }
        completed_batches += 1
        usage = getattr(response, "usage_metadata", None)
        if usage:
            print(f"[AI CONTEXT] batch={completed_batches} prompt_tokens={getattr(usage, 'prompt_token_count', 0)} output_tokens={getattr(usage, 'candidates_token_count', 0)}")
    CONTEXT_PATH.write_text(json.dumps(contexts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[AI CONTEXT] cached={len(contexts)} missing_remaining={sum(1 for r in rows if contexts.get(slugify(r['Software Name']), {}).get('input_hash') != input_hash(r))}")
    return contexts


def read_shell():
    source = INDEX_PATH.read_text(encoding="utf-8")
    style = re.search(r"<style>([\s\S]*?)</style>", source, re.I)
    nav = re.search(r"<nav\b[\s\S]*?</nav>", source, re.I)
    footer = re.search(r"<footer\b[\s\S]*?</footer>", source, re.I)
    if not style or not nav or not footer:
        raise RuntimeError("Could not extract the existing site's CSS, header, and footer from index.html")
    nav_html = re.sub(r'\s+onclick="[^"]*"', "", nav.group(0))
    def fix_link(match):
        attrs, body = match.group(1), match.group(2)
        label = re.sub(r"<[^>]+>", "", body).strip().casefold()
        if "nav-logo" in attrs:
            href = SITE + "/"
        elif "directory" in label or "software" == label:
            href = SITE + "/#page-directory"
        else:
            href = SITE + "/"
        attrs = re.sub(r'href="[^"]*"', 'href="' + href + '"', attrs)
        if "href=" not in attrs:
            attrs += ' href="' + href + '"'
        return "<a" + attrs + ">" + body + "</a>"
    nav_html = re.sub(r"<a([^>]*)>([\s\S]*?)</a>", fix_link, nav_html, flags=re.I)
    theme_button = '<button type="button" class="theme-toggle" onclick="toggleTheme()" aria-label="Toggle light and dark theme">☼ / ☾ Theme</button>'
    nav_html = re.sub(r"</nav>", theme_button + "</nav>", nav_html, count=1, flags=re.I)
    footer_html = footer.group(0)
    footer_html = re.sub(r'<span id="footerMeta"></span>', '<span>Software license directory · Informational use</span>', footer_html)
    return style.group(1), nav_html, footer_html


def render_page(row, context, related, style, nav, footer, total):
    name = str(row["Software Name"])
    slug = slugify(name)
    url = str(row.get("Official URL", ""))
    category = str(row.get("Category", "Unclassified"))
    status = str(row.get("License Status", "Not verified"))
    updated = str(row.get("Last Updated", ""))
    context = context if context and context.get("summary") else fallback_context(row)
    canonical = SITE + "/software/" + slug + "/"
    description = str(context.get("summary", ""))[:155]
    if not description:
        description = f"Check {name} licensing information, free-tier details, limitations, and official sources on IsItFree?"
    def section(title, key):
        return '<section class="detail-card"><h2>' + esc(title) + '</h2><p>' + esc(context.get(key, "")) + '</p></section>'
    related_html = "".join('<a class="related-link" href="' + SITE + '/software/' + esc(slugify(r["Software Name"])) + '/"><strong>' + esc(r["Software Name"]) + '</strong><span>' + esc(r["Category"]) + '</span></a>' for r in related)
    faq = context.get("faq", [])
    faq_html = "".join('<details class="faq-item"><summary>' + esc(item["question"]) + '</summary><p>' + esc(item["answer"]) + '</p></details>' for item in faq if item.get("question") and item.get("answer"))
    evidence = row.get("Evidence URL", "")
    source_html = '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">Official website ↗</a>'
    if evidence:
        source_html += ' <a href="' + esc(evidence) + '" target="_blank" rel="noopener noreferrer">License / pricing source ↗</a>'
    body = f"""<main class="software-main">
      <nav aria-label="Breadcrumb" class="breadcrumbs"><a href="{SITE}/">Home</a><span>›</span><a href="{SITE}/#page-directory">Software directory</a><span>›</span><span>{esc(name)}</span></nav>
      <header class="software-hero"><p class="eyebrow">SOFTWARE LICENSE GUIDE</p><h1>{esc(name)}</h1><p class="lead">{esc(context.get("summary", ""))}</p><div class="detail-badges"><span class="category-pill">{esc(category)}</span><span class="status-pill">{esc(status)}</span></div><div class="source-links">{source_html}</div></header>
      <div class="detail-grid">
        {section("What it does", "what_it_does")}
        {section("Who it may suit", "best_for")}
        {section("Free tier and license", "free_tier")}
        {section("Limitations to check", "limitations")}
        {section("Licensing notes", "licensing_notes")}
      </div>
      <section class="detail-card"><h2>Verification details</h2><p>Directory record last updated: <strong>{esc(updated or "Date unavailable")}</strong>. This date reflects the record's last recorded change, not a guarantee that the vendor's current terms were independently checked today.</p><p>Confirm licensing, pricing, and commercial-use rights in the official sources before making a business decision.</p></section>
      {('<section class="detail-card"><h2>Frequently asked questions</h2>' + faq_html + '</section>') if faq_html else ''}
      <section class="detail-card"><h2>Related software</h2><div class="related-grid">{related_html or '<p>More related entries will appear as the directory grows.</p>'}</div></section>
      <p class="directory-count">Explore the complete IsItFree? software directory for more licensing information.</p>
    </main>"""
    schema = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": name + " License Information",
        "url": canonical,
        "description": description,
        "isPartOf": {"@type": "WebSite", "name": "IsItFree?", "url": SITE + "/"},
        "mainEntity": {"@type": "SoftwareApplication", "name": name, "url": url, "description": description},
        "dateModified": updated if re.fullmatch(r"\d{4}-\d{2}-\d{2}", updated) else None,
    }
    schema = {k: v for k, v in schema.items() if v is not None}
    faq_schema = None
    if faq:
        faq_schema = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [{"@type": "Question", "name": x["question"], "acceptedAnswer": {"@type": "Answer", "text": x["answer"]}} for x in faq if x.get("question") and x.get("answer")]}
    schema_json = json.dumps(schema, ensure_ascii=False).replace("</", "<\\/")
    faq_schema_markup = ""
    if faq_schema:
        faq_json = json.dumps(faq_schema, ensure_ascii=False).replace("</", "<\\/")
        faq_schema_markup = '<script type="application/ld+json">' + faq_json + '</script>'
    head = f"""<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(name)} License & Free Plan Guide | IsItFree?</title>
<meta name="description" content="{esc(description)}"><meta name="robots" content="index,follow">
<link rel="canonical" href="{canonical}">
<meta property="og:type" content="article"><meta property="og:site_name" content="IsItFree?"><meta property="og:title" content="{esc(name)} License & Free Plan Guide"><meta property="og:description" content="{esc(description)}"><meta property="og:url" content="{canonical}">
<meta name="twitter:card" content="summary"><meta name="twitter:title" content="{esc(name)} License Guide"><meta name="twitter:description" content="{esc(description)}">
<script type="application/ld+json">{schema_json}</script>
{faq_schema_markup}"""
    extra_css = """
/* Generated software pages: light theme support */
.theme-toggle{border:1px solid #293446;background:#0e141f;color:#dce3ee;border-radius:9px;padding:7px 10px;font-size:.75rem;cursor:pointer;white-space:nowrap}
body[data-theme="light"]{--bg:#f5f7fb;--panel:#fff;--panel-2:#f0f3f8;--panel-3:#e8edf5;--line:#d9e0eb;--line-soft:#e6ebf2;--text:#172033;--muted:#586579;--muted-2:#6e7b8f;--blue:#315fc7;--green:#14835f;--purple:#7654bb;--red:#c43f58;--orange:#a96113;--cyan:#117b9a;color:var(--text);background:var(--bg)}
body[data-theme="light"]:before{background:linear-gradient(180deg,rgba(49,95,199,.05),transparent 35%),radial-gradient(circle at 85% 8%,rgba(107,89,203,.06),transparent 25rem)}
body[data-theme="light"] nav{background:rgba(255,255,255,.94);border-bottom-color:#dfe5ee}
body[data-theme="light"] .nav-links a{color:#586579}
body[data-theme="light"] .nav-links a:hover,body[data-theme="light"] .nav-links a.active{background:#eaf0fa;color:#172033}
body[data-theme="light"] .theme-toggle{background:#fff;border-color:#d4dce8;color:#29364b}
body[data-theme="light"] .software-hero{background:linear-gradient(135deg,#fff,#f0f4fa);border-color:#dce3ed}
body[data-theme="light"] .software-hero .eyebrow,body[data-theme="light"] .breadcrumbs a{color:#315fc7}
body[data-theme="light"] .software-hero h1,body[data-theme="light"] .detail-card h2,body[data-theme="light"] .faq-item summary,body[data-theme="light"] .related-link strong{color:#172033}
body[data-theme="light"] .software-hero .lead,body[data-theme="light"] .detail-card p,body[data-theme="light"] .related-link span,body[data-theme="light"] .breadcrumbs,body[data-theme="light"] .directory-count{color:#586579}
body[data-theme="light"] .detail-card,body[data-theme="light"] .related-link{background:#fff;border-color:#dce3ed}
body[data-theme="light"] .category-pill{background:#edf3ff;color:#315fc7;border-color:#d4e0f8}
body[data-theme="light"] .status-pill{background:#eaf8f1;color:#147351;border-color:#c6e8d7}
body[data-theme="light"] .source-links a{background:#f7f9fc;color:#315fc7;border-color:#d7dfeb}
body[data-theme="light"] .faq-item{border-color:#dce3ed}
body[data-theme="light"] footer{background:#edf1f7;border-color:#dce3ed;color:#56647a}
body[data-theme="light"] footer a{color:#315fc7}
@media(max-width:650px){.theme-toggle{padding:7px 8px;font-size:0}}
.software-main{width:min(1080px,100%);margin:0 auto;padding:34px 24px 76px}.breadcrumbs{position:static;height:auto;background:none;border:0;backdrop-filter:none;padding:0;margin:0 0 26px;display:flex;gap:9px;flex-wrap:wrap;color:var(--muted-2);font-size:.82rem}.breadcrumbs a{color:#8fb0ff}.software-hero{padding:35px;border:1px solid var(--line);border-radius:18px;background:linear-gradient(135deg,#111a2a,#0c121c);margin-bottom:20px}.software-hero .eyebrow{font:600 .72rem var(--mono);letter-spacing:.1em;color:#8fb0ff}.software-hero h1{font-size:clamp(2.3rem,6vw,4.2rem);letter-spacing:-.055em;line-height:1.08;margin:12px 0}.software-hero .lead{font-size:1.05rem;color:var(--muted);max-width:850px}.detail-badges{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0}.category-pill,.status-pill{display:inline-block;padding:7px 11px;border:1px solid #33435b;border-radius:999px;font-size:.8rem;background:#121d2e}.status-pill{color:#a8e8ce;border-color:#28513f}.source-links{display:flex;gap:10px;flex-wrap:wrap;margin-top:22px}.source-links a{padding:10px 13px;border:1px solid #354765;border-radius:9px;color:#a9c4ff;font-weight:600;font-size:.86rem}.detail-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.detail-card{border:1px solid var(--line);border-radius:14px;padding:24px;background:var(--panel);margin-bottom:14px;min-width:0}.detail-card h2{font-size:1.18rem;margin-bottom:9px;letter-spacing:-.02em}.detail-card p{color:var(--muted);font-size:.94rem;line-height:1.8;margin:7px 0}.detail-grid .detail-card{margin:0}.faq-item{border-top:1px solid var(--line);padding:12px 0}.faq-item summary{cursor:pointer;font-weight:650}.related-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.related-link{display:flex;flex-direction:column;gap:4px;padding:14px;border:1px solid var(--line);border-radius:10px;background:var(--panel-2)}.related-link:hover{border-color:#5475b4}.related-link span{font-size:.78rem;color:var(--muted)}.directory-count{color:var(--muted-2);font-size:.8rem;margin-top:18px}@media(max-width:700px){.software-main{padding:24px 15px 55px}.software-hero{padding:23px}.detail-grid{grid-template-columns:1fr}.related-grid{grid-template-columns:1fr}.detail-card{padding:19px}}"""
    theme_script = '<script>(function(){function applyTheme(t){document.body.dataset.theme=t;try{localStorage.setItem("isitfree-theme",t)}catch(e){}}window.toggleTheme=function(){applyTheme(document.body.dataset.theme==="light"?"dark":"light")};try{var t=localStorage.getItem("isitfree-theme");if(t==="light"||t==="dark")document.body.dataset.theme=t}catch(e){}})();</script>'
    return f"""<!doctype html><html lang="en"><head>{head}<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet"><style>{style}{extra_css}</style></head><body>{nav}{body}{footer}{theme_script}</body></html>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-context-batches", type=int, default=MAX_BATCHES)
    args = parser.parse_args()
    if not CSV_PATH.exists() or not INDEX_PATH.exists():
        print("[ERROR] software CSV or index.html is missing")
        return 1
    df = pd.read_csv(CSV_PATH, keep_default_na=False)
    required = {"Software Name", "Category", "License Status", "Official URL", "Last Updated", "Notes"}
    if not required.issubset(df.columns):
        print("[ERROR] CSV schema is missing required columns")
        return 1
    rows = df.to_dict("records")
    try:
        sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8")) if SOURCES_PATH.exists() else {}
        if not isinstance(sources, dict):
            sources = {}
    except (OSError, json.JSONDecodeError):
        sources = {}
    for row in rows:
        row["Evidence URL"] = sources.get(slugify(str(row["Software Name"])), "")
    # Newest records first, so fresh products receive AI context promptly without sending the directory to Gemini.
    rows.sort(key=lambda r: str(r.get("Last Updated", "")), reverse=True)
    contexts = generate_context_batches(rows, load_contexts(), max(0, args.max_context_batches))
    CONTEXT_PATH.write_text(json.dumps(contexts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    style, nav, footer = read_shell()
    by_category = {}
    for row in rows:
        by_category.setdefault(row["Category"], []).append(row)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    seen_slugs = set()
    generated_rows = []
    for row in rows:
        name = str(row["Software Name"]).strip()
        slug = slugify(name)
        if not name or slug in seen_slugs:
            print(f"[WARN] Skipping empty or colliding software slug: {name}")
            continue
        seen_slugs.add(slug)
        peers = [r for r in by_category.get(row["Category"], []) if r["Software Name"] != name][:6]
        context = contexts.get(slug, {})
        target = OUTPUT_DIR / slug / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_page(row, context, peers, style, nav, footer, len(rows)), encoding="utf-8")
        generated_rows.append(row)
    urls = [(SITE + "/", str(date.today()))]
    for row in generated_rows:
        slug = slugify(str(row["Software Name"]))
        modified = str(row.get("Last Updated", ""))
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", modified):
            modified = str(date.today())
        urls.append((SITE + "/software/" + slug + "/", modified))
    url_xml = "\n".join("  <url><loc>" + html.escape(url) + "</loc><lastmod>" + html.escape(modified) + "</lastmod></url>" for url, modified in urls)
    SITEMAP_PATH.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + url_xml + "\n</urlset>\n", encoding="utf-8")
    print(f"[SUCCESS] Generated {len(generated_rows)} software pages and sitemap with {len(urls)} URLs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
