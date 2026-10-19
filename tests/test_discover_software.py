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


class DiscoveryHelpersTests(unittest.TestCase):
    def test_normalization_ignores_punctuation_and_company_suffix(self):
        self.assertEqual(discovery.norm("Example, Inc."), discovery.norm("EXAMPLE"))

    def test_exact_match_is_duplicate(self):
        rows = [{"Software Name": "Example App", "Official URL": "https://example.com", "Category": "Open Source", "Notes": ""}]
        status, matches = discovery.local_match({"software_name": "Example-App", "official_url": "https://different.example"}, rows)
        self.assertEqual(status, "same")
        self.assertEqual(matches[0]["Software Name"], "Example App")

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

    def test_under_target_does_not_modify_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "software_list.csv"
            frame = pd.DataFrame([{
                "Software Name": "Existing", "Category": "Open Source", "License Status": "MIT",
                "Official URL": "https://example.com", "Last Updated": "2026-01-01", "Notes": "Existing note"
            }])
            frame.to_csv(csv_path, index=False)
            before = csv_path.read_bytes()
            old_csv, old_recent, old_client, old_target = discovery.CSV_PATH, discovery.RECENT_PATH, discovery.CLIENT, discovery.TARGET
            discovery.CSV_PATH = csv_path
            discovery.RECENT_PATH = Path(directory) / "recent_updates.json"
            discovery.CLIENT = object()
            discovery.TARGET = 1
            try:
                with mock.patch.object(discovery, "discover", return_value=[]), mock.patch.object(discovery.time, "sleep"):
                    self.assertEqual(discovery.run(), 1)
                self.assertEqual(csv_path.read_bytes(), before)
                self.assertFalse(discovery.RECENT_PATH.exists())
            finally:
                discovery.CSV_PATH, discovery.RECENT_PATH, discovery.CLIENT, discovery.TARGET = old_csv, old_recent, old_client, old_target


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
