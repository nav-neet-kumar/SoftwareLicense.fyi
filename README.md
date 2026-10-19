# IsItFree?

**IsItFree?** is a software licensing discovery and reference website that helps users quickly understand whether software is free, open source, freemium, subscription-based, or paid.

🌐 **Website:** https://isitfree.webline.cloud/
👤 **Portfolio:** https://navneet.webline.cloud/

---

## 🚀 Latest Update

### Increased Daily Software Discovery

The automated discovery workflow targets 15 verified new software entries per successful run.

The target is controlled by the `NEW_PER_RUN` environment variable. The script compares names against the CSV locally; the existing directory is not sent to Gemini.

```python
TARGET = int(os.getenv("NEW_PER_RUN", "15"))
```

in:

```text
scripts/discover_software.py
```

The daily GitHub Action automatically runs the discovery process and adds newly discovered software to the project database.

---

## 🤖 AI-Powered Software Updates

The project uses an automated AI-assisted workflow to discover and maintain software information.

The system can discover software and collect information such as:

* Software name
* Category
* License type
* Official website
* Description
* Source/repository information
* Pricing information when available
* Software availability

The website displays the latest AI update information based on the software database.

---

## 📊 Software Database

Software information is stored in:

```text
data/software_list.csv
```

The website dynamically reads the database so that software counts and update information remain synchronized with the CSV.

### Important

Do not hardcode the number of software entries in `index.html`.

The website should calculate the total directly from the CSV.

For example:

```text
150 software entries
```

will automatically become:

```text
151 software entries
```

when another valid entry is added.

---

## 🔄 Automated Discovery

The daily discovery workflow is located at:

```text
.github/workflows/discover_daily.yml
```

The workflow runs the discovery script:

```text
scripts/discover_software.py
```

The current target is 15 verified new software entries per successful run. If the script cannot verify the full target, it exits with an error and leaves the CSV unchanged.

---

## 🖥️ Website Features

### Software Search

Users can search the software directory directly from the homepage.

The `/` keyboard shortcut can be used to quickly focus/open the search interface.

### Categories

Software is organized into categories.

Selecting a category opens the directory with the appropriate category filter applied.

### License Information

Each software entry displays its available licensing information, making it easier to determine whether the software is:

* Free
* Open Source
* Freemium
* Commercial
* Subscription-based
* Trial
* Other licensing models

### Last AI Update

The website displays when the software information was most recently updated.

This information is derived from the project data rather than relying on a manually entered software count.

---

## ⚑ Report an Error

Users can report incorrect software information directly from the directory.

The **Flag Error** option allows users to submit information such as:

* Incorrect license
* Incorrect website
* Incorrect category
* Outdated information
* Incorrect description
* Other errors

This helps keep the software database accurate.

---

## 💡 Suggest Software

Users can also suggest software that is missing from the directory.

Suggestions can be submitted through the website and reviewed before being added to the database.

---

## 🚧 Upcoming Features

The following features are planned for future releases.

### 1. Compare Options

Allow users to compare multiple software products side-by-side.

Potential comparison fields:

* License
* Features
* Platform
* Pricing
* Open-source status
* Pros and cons

### 2. Approximate Pricing

Add estimated pricing information for commercial software.

Possible information:

* Free tier
* Starting price
* Monthly pricing
* Annual pricing
* Business pricing
* Enterprise pricing

Pricing will be presented as approximate information and users should verify the current price with the software vendor.

### 3. Free Alternatives

Show free and open-source alternatives to commercial software.

For example:

```text
Microsoft Office
        ↓
LibreOffice
        ↓
ONLYOFFICE
```

This will help users discover lower-cost or free alternatives.

---

## 📁 Project Structure

```text
SoftwareLicense.fyi/
│
├── data/
│   └── software_list.csv
│
├── scripts/
│   └── discover_software.py
│
├── .github/
│   └── workflows/
│       └── discover_daily.yml
│
├── index.html
└── README.md
```

---

## 🌐 SEO and Static Software Pages

Published software records are rendered as static pages under `software/<slug>/index.html`. The generator reuses the homepage CSS, header, and footer, creates canonical metadata and structured data, and caches AI-generated product context in `data/software_context.json`. Context generation is batched and incremental to control token usage; entries without cached AI context use a conservative CSV-derived summary until their batch is processed.

Public crawler resources:
- `robots.txt` — allows crawlers to access public pages and points to the sitemap.
- `llms.txt` — summarizes the public directory for AI assistants and agents.
- `readme.txt` — plain-text public site information.
- `sitemap.xml` — generated from the homepage and published software detail pages.
- `data/software_sources.json` — per-product official license/pricing evidence links when available.
- `data/recent_updates.json` — records genuine NEW and materially UPDATED events.

The public directory is intended to be crawlable. Crawler rules are not access controls; secrets and private configuration must be protected separately.

## ⚙️ Updating the Daily Limit

To change the target, update the `NEW_PER_RUN` environment value in `.github/workflows/discover_daily.yml`. For example, set it to `30` to target 30 verified entries. Larger targets may require more Gemini requests and can fail if verification cannot establish enough valid entries.

---

## ⚠️ Important Development Rule

The website frontend and automated backend are separate components.

When making frontend design changes:

* Do not modify Python discovery logic unnecessarily.
* Do not modify the CSV structure without updating dependent code.
* Do not hardcode software counts.
* Keep software counts dynamically calculated from `data/software_list.csv`.
* Preserve the existing automated GitHub workflow.

---

## 🌐 Project

**IsItFree?**

A simple way to discover software and understand its licensing.

Website: https://isitfree.webline.cloud/

Portfolio: https://navneet.webline.cloud/

---

## 📌 Current Release

### Version Update

**Daily discovery:** 20 new software entries per run
**Database:** CSV-based
**Discovery:** Automated
**Updates:** AI-assisted
**Search:** Enabled
**Categories:** Enabled
**Error reporting:** Enabled
**Software suggestions:** Enabled

### Planned

* Software comparison
* Approximate pricing
* Free alternatives
* More detailed software information
* Improved licensing intelligence
* Continued automated database expansion
