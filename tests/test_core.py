from __future__ import annotations

import sqlite3
import io
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr

from shade_node.cli import main
from shade_node.collectors import parse_nws, parse_rss, parse_usgs
from shade_node.db import claim_detail, connect, ingest, transition
from shade_node.engine import Settings, active_sources
from shade_node.formatters import traffic_message
from shade_node.model import Observation, SourceConfig


class ShadeTests(unittest.TestCase):
    def source(self, kind="rss", source_type="community", family="forum-a"):
        return SourceConfig("test", kind, "Test Source", "https://example.test/feed", source_type, family)

    def test_nws_parser_preserves_source_and_location(self):
        payload = b'{"features":[{"id":"urn:1","properties":{"event":"Tornado Warning","headline":"Tornado Warning issued","areaDesc":"Knox County, TN","severity":"Extreme","sent":"2026-09-21T01:00:00Z","description":"Take cover"}}]}'
        item = parse_nws(self.source("nws_alerts", "official", "nws"), payload)[0]
        self.assertEqual(item.location, "Knox County, TN")
        self.assertEqual(item.severity, "extreme")
        self.assertEqual(item.source_family, "nws")

    def test_usgs_parser(self):
        payload = b'{"features":[{"id":"us1","properties":{"mag":6.2,"title":"M 6.2 - Test Region","place":"Test Region","time":1789952400000,"url":"https://example.test/us1","tsunami":0}}]}'
        item = parse_usgs(self.source("usgs_geojson", "official", "usgs"), payload)[0]
        self.assertEqual(item.category, "earthquake")
        self.assertEqual(item.severity, "severe")

    def test_rss_parser(self):
        feed = b'<rss><channel><item><guid>x1</guid><title>Possible incident</title><description>Unconfirmed report</description><link>https://example.test/x1</link><pubDate>Sun, 21 Sep 2026 01:00:00 GMT</pubDate></item></channel></rss>'
        item = parse_rss(self.source(), feed)[0]
        self.assertEqual(item.external_id, "x1")
        self.assertEqual(item.source_type, "community")

    def test_repeats_do_not_become_independent_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            path = f"{folder}/test.db"
            with connect(path) as db:
                first = Observation("a", "A", "community", "telegram-origin", "1", "Possible fuel outage in Knox", "", "https://a")
                second = Observation("b", "B mirror", "community", "telegram-origin", "2", "Possible fuel outage in Knox", "", "https://b")
                claim_id, _ = ingest(db, first)
                ingest(db, second)
                claim, _ = claim_detail(db, claim_id)
                self.assertEqual(claim["independent_families"], 1)
                self.assertEqual(claim["confidence_label"], "UNVERIFIED")

    def test_independent_official_source_confirms_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            path = f"{folder}/test.db"
            with connect(path) as db:
                first = Observation("a", "Monitor", "community", "monitor-a", "1", "Major fire near Knoxville terminal", "", "https://a", category="fire", location="Knoxville TN", severity="severe")
                official = Observation("b", "Fire Agency", "official", "agency-b", "2", "Major fire near Knoxville terminal", "", "https://b", category="fire", location="Knoxville TN", severity="severe")
                claim_id, _ = ingest(db, first)
                ingest(db, official)
                claim, rows = claim_detail(db, claim_id)
                self.assertEqual(claim["confidence_label"], "CONFIRMED")
                message = traffic_message(claim, rows, callsign="TEST", network="GHOSTNET", max_chars=240)
                self.assertIn("@GHOSTNET CONFIRMED/", message)
                self.assertLessEqual(len(message), 240)

    def test_workflow_prevents_skipping_human_review(self):
        with tempfile.TemporaryDirectory() as folder:
            path = f"{folder}/test.db"
            with connect(path) as db:
                claim_id, _ = ingest(db, Observation("a", "A", "official", "a", "1", "Test alert", "", "https://a"))
                with self.assertRaises(ValueError):
                    transition(db, claim_id, "SENT")
                transition(db, claim_id, "REVIEW")
                transition(db, claim_id, "TX_CANDIDATE")
                self.assertEqual(db.execute('SELECT tx_candidate_basis FROM claims WHERE id=?',(claim_id,)).fetchone()[0],'corroborated')
                transition(db, claim_id, "SENT")

    def test_exercise_traffic_is_marked_at_both_ends(self):
        with tempfile.TemporaryDirectory() as folder:
            with connect(f"{folder}/test.db") as db:
                claim_id, _ = ingest(db, Observation("a", "A", "official", "a", "1", "Bridge closed", "", "https://a"))
                claim, rows = claim_detail(db, claim_id)
                message = traffic_message(
                    claim,
                    rows,
                    callsign="TEST",
                    network="TESTNET",
                    mode="exercise",
                    region_label="TEST REGION",
                )
                self.assertTrue(message.startswith("@TESTNET EXERCISE/EMCOMM"))
                self.assertTrue(message.endswith("EXERCISE"))

    def test_standard_mode_excludes_emcomm_only_sources(self):
        standard = SourceConfig("normal", "rss", "Normal", "https://example.test/normal", "official", "normal")
        emergency = SourceConfig(
            "eoc",
            "rss",
            "EOC",
            "https://example.test/eoc",
            "official",
            "eoc",
            emcomm_only=True,
        )
        settings = Settings(
            database=":memory:",
            max_age_days=7,
            timeout=20,
            max_bytes=1000,
            user_agent="SHADE-Test",
            callsign="TEST",
            network="TESTNET",
            region_label="TEST",
            max_message_chars=500,
            operating_mode="standard",
            standard_min_queue_score=35,
            emcomm_min_queue_score=10,
            emcomm_network="TESTNET",
            emcomm_region_label="TEST",
            emcomm_max_message_chars=700,
            sources=[standard, emergency],
        )
        self.assertEqual([source.id for source in active_sources(settings, "standard")], ["normal"])
        self.assertEqual([source.id for source in active_sources(settings, "exercise")], ["normal", "eoc"])

    def test_actual_format_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.toml"
            config.write_text(
                """
[operator]
callsign = "TEST"
network = "TESTNET"
region_label = "TEST"

[collection]
database = "test.db"
user_agent = "SHADE-Test"
""".strip(),
                encoding="utf-8",
            )
            with connect(str(root / "test.db")) as db:
                ingest(db, Observation("a", "A", "official", "a", "1", "Test alert", "", "https://a"))
            errors = io.StringIO()
            with redirect_stderr(errors):
                result = main(["--config", str(config), "--emcomm", "actual", "format", "1"])
            self.assertEqual(result, 2)
            self.assertIn("--confirm-actual", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
