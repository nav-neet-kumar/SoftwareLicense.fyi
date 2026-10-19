# IsItFree?

**IsItFree?** is a public software licensing directory that helps people understand whether software is free, open source, freemium, trial-based, subscription-based, or paid.

- **Live website:** https://isitfree.webline.cloud/
- **Source code:** https://github.com/nav-neet-kumar/SoftwareLicense.fyi
- **Maintainer portfolio:** https://navneet.webline.cloud/

## Current project status

**Status checked: 19 October 2026**

| Area | Current status |
|---|---|
| Website | Live; latest GitHub Pages deployment completed successfully |
| Software database | 591 records in `data/software_list.csv` at the last repository check |
| Software detail pages | Static pages generated at `software/<slug>/index.html` |
| Discovery workflow | Scheduled daily at 08:00 UTC; can also be run manually |
| Discovery target | 15 verified new entries per successful run |
| Automated discovery right now | Latest run failed because the Gemini API free-tier request quota was exhausted |
| SEO and crawler files | Sitemap, robots.txt, llms.txt and readme.txt are part of the published site |
| Recent updates and evidence | Tracked in generated JSON files when data is added or materially updated |

The latest static-page generation and GitHub Pages deployment succeeded. The most recent discovery run did **not** add entries: Gemini returned HTTP 429 (quota exceeded), and the script left the CSV unchanged. Automated discovery will need available Gemini API quota before it can complete another run successfully.

- [Latest GitHub Actions runs](https://github.com/nav-neet-kumar/SoftwareLicense.fyi/actions)
- [Daily discovery workflow](https://github.com/nav-neet-kumar/SoftwareLicense.fyi/actions/workflows/discover_daily.yml)
- [Live website](https://isitfree.webline.cloud/)

## What the website does

- Search a directory of software and view its reported licensing status.
- Review official website links and concise licensing notes.
- Open individual software detail pages designed for direct linking and search-engine indexing.
- Browse category-filtered directory results.
- Report incorrect information or suggest software for review.

Licensing and pricing can change. Use the linked vendor or project website as the final authority before making purchasing, deployment, or compliance decisions. A free download does not necessarily mean free commercial use.

## How software data is maintained

The main directory is stored in:

```text
data/software_list.csv
```

The CSV is the source of truth for the software count and directory records. Do not hardcode a total in the homepage.

The automated discovery script is:

```text
scripts/discover_software.py
```

It researches candidates and checks for existing entries locally before accepting new records. The existing directory is not sent to Gemini as a duplicate-checking prompt. A successful discovery run targets **15 verified new software entries**. If the script cannot verify the target, it exits without changing the CSV.

The GitHub Actions workflow is:

```text
.github/workflows/discover_daily.yml
```

It runs daily at **08:00 UTC** and supports manual dispatch from the repository's Actions tab. It uses the `GEMINI_API_KEY` repository secret.

## Static pages and discoverability

Generated software pages live at:

```text
software/<slug>/index.html
```

The page generator reuses the site's design and produces individual pages with canonical metadata and structured data. Cached AI-generated page context is maintained incrementally to reduce repeated requests.

Key generated/public files:

| Path | Purpose |
|---|---|
| `sitemap.xml` | Lists the homepage and generated software pages |
| `robots.txt` | Crawler guidance and sitemap location |
| `llms.txt` | Summary for AI assistants and agents |
| `readme.txt` | Plain-text overview of the public website |
| `data/software_sources.json` | Official source links for licensing/pricing evidence, when available |
| `data/recent_updates.json` | Genuine new-entry and material-update events |
| `data/software_context.json` | Cached context used to enrich software detail pages |

Crawler directives are not access controls. Do not put secrets or private configuration in public files.

## Repository layout

```text
.
├── .github/workflows/
│   ├── discover_daily.yml
│   └── test_discovery_pages.yml
├── data/
│   ├── software_list.csv
│   ├── software_context.json
│   ├── software_sources.json
│   └── recent_updates.json
├── scripts/
│   ├── discover_software.py
│   └── generate_software_pages.py
├── software/
│   └── <slug>/index.html
├── index.html
├── sitemap.xml
├── robots.txt
├── llms.txt
├── readme.txt
└── README.md
```

## Development and validation

Before changing the project:

- Keep the CSV schema compatible with scripts that read it.
- Check for duplicate software names before adding records.
- Keep homepage totals data-driven.
- Regenerate software pages and the sitemap when relevant data changes.
- Avoid changing discovery logic for unrelated frontend-only work.
- Never commit API keys or other secrets.

The discovery and page-generation changes were tested before merging: 13 mocked tests passed, and an offline smoke test generated 590 detail pages and validated 591 sitemap URLs. Since then, the directory has grown to 591 CSV records and the Orkas detail page has been generated. Automated discovery currently needs Gemini quota restored; see the status section above.

## Contributing

If you find a wrong license, outdated price, broken official link, or missing product, open an issue in the [GitHub repository](https://github.com/nav-neet-kumar/SoftwareLicense.fyi/issues) with the product name and supporting official source. Suggestions are reviewed before being added.

---

**IsItFree? — Understand software licensing before you install, use, or buy.**
