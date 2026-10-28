import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import discover_software as discovery
import generate_software_pages as pages
import update_licenses as updater
import monthly_data_audit as monthly_audit



class MonthlyAuditHelpersTests(unittest.TestCase):
    def test_edition_labels_normalize_for_duplicate_detection(self):
        self.assertEqual(
            monthly_audit.norm("IntelliJ IDEA Community Edition"),
            monthly_audit.norm("IntelliJ IDEA Ultimate Edition"),
        )

    def test_duplicate_candidates_include_same_product_editions(self):
        rows = [
            {"Software Name": "JetBrains IntelliJ IDEA", "Official URL": "https://www.jetbrains.com/idea", "Category": "License Required", "License Status": "Subscription", "Last Updated": "2025-01-01", "Notes": ""},
            {"Software Name": "IntelliJ IDEA Community Edition", "Official URL": "https://www.jetbrains.com/idea/", "Category": "Community Version", "License Status": "Free", "Last Updated": "2026-10-22", "Notes": ""},
        ]
        pairs = monthly_audit.duplicate_candidates(rows)
        self.assertTrue(any(p["left_index"] == 0 and p["right_index"] == 1 for p in pairs))

    def test_vendor_prefixed_aliases_like_chrome_are_duplicate_candidates(self):
        rows = [
            {"Software Name": "Chrome", "Official URL": "https://www.google.com/chrome/", "Category": "Free for Home", "License Status": "Free", "Last Updated": "2025-01-01", "Notes": ""},
            {"Software Name": "Google Chrome", "Official URL": "https://www.google.com/chrome/", "Category": "Free for Home", "License Status": "Free", "Last Updated": "2026-01-01", "Notes": ""},
        ]
        pairs = monthly_audit.duplicate_candidates(rows)
        self.assertTrue(any(p["left_index"] == 0 and p["right_index"] == 1 for p in pairs))

    def test_distinct_products_from_same_vendor_are_not_auto_marked_duplicate(self):
        rows = [
            {"Software Name": "Visual Studio", "Official URL": "https://visualstudio.microsoft.com", "Category": "License Required", "License Status": "Paid", "Last Updated": "2025-01-01", "Notes": ""},
            {"Software Name": "Visual Studio Code", "Official URL": "https://code.visualstudio.com", "Category": "Open Source", "License Status": "Free", "Last Updated": "2025-01-01", "Notes": ""},
        ]
        pairs = monthly_audit.duplicate_candidates(rows)
        self.assertEqual(pairs, [])


    def test_last_verified_date_within_29_days_is_fresh(self):
        from datetime import date, timedelta
        row = {"Last Verified Date": (date.today() - timedelta(days=29)).isoformat()}
        self.assertTrue(monthly_audit.is_freshly_verified(row))

    def test_last_verified_date_at_30_days_is_due(self):
        from datetime import date, timedelta
        row = {"Last Verified Date": (date.today() - timedelta(days=30)).isoformat()}
        self.assertFalse(monthly_audit.is_freshly_verified(row))

    def test_missing_or_invalid_last_verified_date_is_due(self):
        self.assertFalse(monthly_audit.is_freshly_verified({}))
        self.assertFalse(monthly_audit.is_freshly_verified({"Last Verified Date": "not-a-date"}))

    def test_checkpoint_writer_persists_rows_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "software_list.csv"
            rows = [{
                "Software Name": "Example",
                "Category": "Open Source",
                "License Status": "MIT",
                "Official URL": "https://example.com",
                "Last Updated": "2026-10-26",
                "Notes": "Verified",
                "Last Verified Date": "2026-10-26",
            }]
            columns = list(rows[0])
            monthly_audit.write_csv_atomic(rows, columns, path)
            with path.open("r", encoding="utf-8", newline="") as handle:
                saved = list(__import__("csv").DictReader(handle))
            self.assertEqual(saved, rows)
            self.assertFalse(path.with_suffix(".csv.tmp").exists())


class DiscoveryHelpersTests(unittest.TestCase):
    def test_normalization_ignores_punctuation_and_company_suffix(self):
        self.assertEqual(discovery.norm("Example, Inc."), discovery.norm("EXAMPLE"))

    def test_exact_match_is_duplicate(self):
        rows = [{"Software Name": "Example App", "Official URL": "https://example.com", "Category": "Open Source", "Notes": ""}]
        status, matches = discovery.local_match({"software_name": "Example-App", "official_url": "https://different.example"}, rows)
        self.assertEqual(status, "same")
        self.assertEqual(matches[0]["Software Name"], "Example App")

    def test_vscode_alias_is_duplicate_of_visual_studio_code(self):
        rows = [{"Software Name": "Visual Studio Code", "Official URL": "https://code.visualstudio.com", "Category": "Open Source", "Notes": ""}]
        status, matches = discovery.local_match({"software_name": "VS Code", "official_url": "https://different.example"}, rows)
        self.assertEqual(status, "same")
        self.assertEqual(matches[0]["Software Name"], "Visual Studio Code")

    def test_visual_studio_and_visual_studio_code_remain_distinct_but_ambiguous(self):
        rows = [{"Software Name": "Visual Studio", "Official URL": "https://visualstudio.microsoft.com", "Category": "License Required", "Notes": ""}]
        status, matches = discovery.local_match({"software_name": "Visual Studio Code", "official_url": "https://code.visualstudio.com"}, rows)
        self.assertEqual(status, "ambiguous")
        self.assertEqual(matches[0]["Software Name"], "Visual Studio")

    def test_canonical_url_ignores_www_and_trailing_slash(self):
        self.assertEqual(discovery.canonical_url("https://www.example.com/app/"), discovery.canonical_url("https://example.com/app"))

    def test_provider_failure_preserves_batch_without_retry_storm(self):
        candidates = [{"software_name": "Tool A"}, {"software_name": "Tool B"}, {"software_name": "Tool C"}]
        with mock.patch.object(discovery, "verify_batch", side_effect=RuntimeError("All configured Gemini models failed. quota")) as verify:
            verified, pending = discovery.verify_resilient(candidates)
        self.assertEqual(verified, [])
        self.assertEqual(pending, candidates)
        verify.assert_called_once()

    def test_github_source_returns_only_nonfork_repos_with_license_metadata(self):
        class FakeResponse:
            def raise_for_status(self):
                return None
            def json(self):
                return {"items": [
                    {
                        "full_name": "example/tool-a", "name": "tool-a",
                        "html_url": "https://github.com/example/tool-a",
                        "description": "A desktop utility application",
                        "fork": False, "archived": False, "disabled": False,
                        "stargazers_count": 100, "topics": ["desktop-app", "utility"],
                        "owner": {"login": "example"},
                        "license": {"spdx_id": "MIT", "name": "MIT License"}
                    },
                    {
                        "full_name": "example/no-license", "name": "no-license",
                        "html_url": "https://github.com/example/no-license",
                        "description": "A desktop utility application",
                        "fork": False, "archived": False, "disabled": False,
                        "stargazers_count": 100, "topics": ["desktop-app"],
                        "owner": {"login": "example"}, "license": None
                    }
                ]}
        with mock.patch.object(discovery.requests, "get", return_value=FakeResponse()):
            candidates = discovery.discover_github_candidates(limit=10)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0]["software_name"], "tool-a")
        self.assertIn("MIT", candidates[0]["notes"])

    def test_similar_but_distinct_products_require_clarification(self):
        rows = [{"Software Name": "Visual Studio", "Official URL": "https://visualstudio.microsoft.com", "Category": "Community Version", "Notes": ""}]
        status, matches = discovery.local_match({"software_name": "Visual Studio Code", "official_url": "https://code.visualstudio.com"}, rows)
        self.assertEqual(status, "ambiguous")
        self.assertEqual(matches[0]["Software Name"], "Visual Studio")

    def test_similar_name_is_flagged_not_automatically_rejected(self):
        rows = [{"Software Name": "Notion", "Official URL": "https://notion.so", "Category": "Community Version", "Notes": ""}]
        status, _ = discovery.local_match({"software_name": "Notion Calendar", "official_url": "https://calendar.notion.so"}, rows)
        self.assertEqual(status, "ambiguous")
        self.assertNotEqual(status, "same")

    def test_parse_json_handles_surrounding_text(self):
        self.assertEqual(discovery.parse_json('Result: [{"name":"A"}]'), [{"name": "A"}])

    def test_clarification_sends_only_shortlist_and_accepts_different_product(self):
        candidate = {"software_name": "Visual Studio Code", "official_url": "https://code.visualstudio.com", "notes": "Editor"}
        matches = [{"Software Name": "Visual Studio", "Official URL": "https://visualstudio.microsoft.com", "Category": "Community Version", "Notes": "IDE"}]
        with mock.patch.object(discovery, "ask", return_value='{"decision":"DIFFERENT_PRODUCT","matched_name":"","reason":"Separate products"}') as ask:
            decision = discovery.clarify(candidate, matches)
        self.assertEqual(decision, "DIFFERENT_PRODUCT")
        prompt = ask.call_args.args[0]
        self.assertIn("Visual Studio Code", prompt)
        self.assertIn("Visual Studio", prompt)
        self.assertNotIn("Entire 560", prompt)

    def test_retry_usage_logging(self):
        class Usage:
            prompt_token_count = 12
            candidates_token_count = 4
            total_token_count = 16
        class Response:
            text = '{"ok":true}'
            usage_metadata = Usage()
        class Client:
            class Models:
                def generate_content(self, **kwargs):
                    return Response()
            models = Models()
        old = discovery.CLIENT
        discovery.CLIENT = Client()
        before = dict(discovery.USAGE)
        try:
            with mock.patch.object(discovery.time, "sleep"):
                self.assertEqual(json.loads(discovery.ask("test")), {"ok": True})
            self.assertGreaterEqual(discovery.USAGE["requests"], before["requests"] + 1)
            self.assertGreaterEqual(discovery.USAGE["total_tokens"], before["total_tokens"] + 16)
        finally:
            discovery.CLIENT = old

    def test_zero_additions_is_reported_as_completed_run(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "software_list.csv"
            frame = pd.DataFrame([{
                "Software Name": "Existing", "Category": "Open Source", "License Status": "MIT",
                "Official URL": "https://example.com", "Last Updated": "2026-01-01", "Notes": "Existing note",
                "Evidence URL": "", "Last Verified Date": ""
            }])
            frame.to_csv(csv_path, index=False)
            before = csv_path.read_bytes()
            old = {
                "CSV_PATH": discovery.CSV_PATH, "RECENT_PATH": discovery.RECENT_PATH,
                "SOURCES_PATH": discovery.SOURCES_PATH, "STATE_PATH": discovery.STATE_PATH,
                "REPORT_PATH": discovery.REPORT_PATH, "CLIENT": discovery.CLIENT,
                "MAX_DISCOVERY_BATCHES": discovery.MAX_DISCOVERY_BATCHES,
                "MAX_EMPTY_BATCHES": discovery.MAX_EMPTY_BATCHES,
            }
            discovery.CSV_PATH = csv_path
            discovery.RECENT_PATH = Path(directory) / "recent_updates.json"
            discovery.SOURCES_PATH = Path(directory) / "software_sources.json"
            discovery.STATE_PATH = Path(directory) / "discovery_state.json"
            discovery.REPORT_PATH = Path(directory) / "discovery_report.json"
            discovery.CLIENT = object()
            discovery.MAX_DISCOVERY_BATCHES = 3
            discovery.MAX_EMPTY_BATCHES = 3
            try:
                with mock.patch.object(discovery, "discover", return_value=[]), mock.patch.object(discovery, "discover_github_candidates", return_value=[]):
                    self.assertEqual(discovery.run(), 0)
                self.assertEqual(csv_path.read_bytes(), before)
                self.assertFalse(discovery.RECENT_PATH.exists())
                report = json.loads(discovery.REPORT_PATH.read_text(encoding="utf-8"))
                self.assertEqual(report["status"], "no_additions")
                self.assertEqual(report["added"], 0)
            finally:
                for key, value in old.items():
                    setattr(discovery, key, value)





class LicenseUpdateTests(unittest.TestCase):
    def test_cosmetic_note_paraphrase_is_not_a_material_change(self):
        old = "Free for personal use; commercial use requires paid license."
        new = "Free for personal use, and commercial use requires a paid license."
        self.assertFalse(updater.notes_materially_changed(old, new))

    def test_negation_change_is_material(self):
        self.assertTrue(updater.notes_materially_changed("Commercial use allowed.", "Commercial use not allowed."))


class StaticPageTests(unittest.TestCase):
    def test_slug_is_stable(self):
        self.assertEqual(pages.slugify("Visual Studio Code"), "visual-studio-code")

    def test_fallback_context_does_not_claim_verified_commercial_use(self):
        row = {"Software Name": "Example", "Category": "Free for Home", "License Status": "Free for personal use", "Notes": ""}
        context = pages.fallback_context(row)
        self.assertIn("vendor", context["limitations"].lower())
        self.assertIn("official", context["licensing_notes"].lower())

    def test_detail_page_has_canonical_metadata_and_shared_chrome(self):
        row = {
            "Software Name": "Example App", "Category": "Open Source", "License Status": "MIT License",
            "Official URL": "https://example.com", "Last Updated": "2026-10-19", "Notes": "Example note"
        }
        shell_style = "body{color:white}"
        shell_nav = '<nav><a class="nav-logo" href="/">IsItFree?</a><a href="/">Directory</a></nav>'
        shell_footer = '<footer><p>Existing footer</p></footer>'
        page = pages.render_page(row, pages.fallback_context(row), [], shell_style, shell_nav, shell_footer, 1)
        self.assertIn('rel="canonical" href="https://isitfree.webline.cloud/software/example-app/"', page)
        self.assertIn("Example App", page)
        self.assertIn("Existing footer", page)
        self.assertIn("Official website", page)
        self.assertIn('application/ld+json', page)


if __name__ == "__main__":
    unittest.main()


class PublicSiteContractTests(unittest.TestCase):
    def test_error_reporting_never_requests_a_personal_access_token(self):
        page = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="flag-token"', page)
        self.assertNotIn('Authorization', page)
        self.assertIn("no personal access token is needed", page)
        self.assertIn("issues/new?title=", page)

    def test_public_status_supports_every_workflow_outcome_without_run_urls(self):
        status = json.loads((ROOT / "data" / "ai_status.json").read_text(encoding="utf-8"))
        self.assertIn(status.get("status"), {"pass", "no_additions", "partial", "fail"})
        self.assertNotIn("run_url", status)
        page = (ROOT / "index.html").read_text(encoding="utf-8")
        for outcome in ("pass", "no_additions", "partial", "fail"):
            self.assertIn(outcome, page)

    def test_catalog_ui_uses_existing_optional_schema_fields(self):
        import csv
        with (ROOT / "data" / "software_list.csv").open(encoding="utf-8", newline="") as handle:
            columns = set(next(csv.reader(handle)))
        self.assertIn("Evidence URL", columns)
        self.assertIn("Last Verified Date", columns)
        page = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('r["Evidence URL"]', page)
        self.assertIn('r["Last Verified Date"]', page)
        generator = (ROOT / "scripts" / "generate_software_pages.py").read_text(encoding="utf-8")
        self.assertIn('row.get("Last Verified Date", "")', generator)
        self.assertIn("Last licensing verification recorded", generator)

    def test_discovery_state_is_cached_not_added_to_public_git_changes(self):
        workflow = (ROOT / ".github" / "workflows" / "discover_daily.yml").read_text(encoding="utf-8")
        self.assertIn("actions/cache/restore@v4", workflow)
        self.assertIn("actions/cache/save@v4", workflow)
        self.assertIn("git add -A -- data/software_list.csv", workflow)
        self.assertNotIn("git add -A\\n", workflow)
        self.assertNotIn("RUN_URL:", workflow)

    def test_public_error_report_and_privacy_policy_match(self):
        privacy = (ROOT / "privacy.html").read_text(encoding="utf-8")
        self.assertIn("does not ask for, receive, or store a GitHub personal access token", privacy)
        self.assertIn("may be public on GitHub", privacy)
