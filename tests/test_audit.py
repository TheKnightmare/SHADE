"""Regression checks for the independent audit's workflow and collector findings."""
from contextlib import closing
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from shade_node.collectors import (
    CollectionError, PartialCollectionError, NotModified, collect, parse_swpc,
)
from shade_node.db import connect, ingest, queue, claim_detail, housekeeping, suppression_review
from shade_node.desktop import DesktopService
from shade_node.engine import Settings, run_once
from shade_node.model import Observation, SourceConfig
from shade_node.relevance import evaluate, severity_points

NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
PUBLISHED = "2026-09-23T11:00:00Z"


def observation(title, *, category="public-safety", location="United States", body=""):
    return Observation("fixture", "Fixture", "media", "fixture", title, title, body,
                       "https://example.test/item", category=category,
                       location=location, severity="unknown", published_at=PUBLISHED)


class AuditRegressionTests(unittest.TestCase):
    def test_severity_tiers_and_audit_examples(self):
        fixtures = [
            ("Navy sailor suicide attempts prompt crisis response", "self_harm", 25),
            ("Fatal TX shooting leaves one dead", "fatality", 30),
            ("Bodies of a teacher and child recovered", "fatality", 30),
            ("Bridge collapse disrupts interstate", "structural", 22),
            ("Bystander video shows ICE shooting", "injury", 20),
            ("Regional communications outage", "disruption", 15),
        ]
        for title, tier, points in fixtures:
            with self.subTest(title=title):
                self.assertEqual(severity_points(title), points)
                row = observation(title)
                assessed = evaluate({"title": row.title, "body": row.body, "severity": row.severity,
                                     "category": row.category, "location": row.location,
                                     "source_family": row.source_family,
                                     "published_at": row.published_at,
                                     "observed_at": row.observed_at, "raw_json": row.raw_json()},
                                    now=NOW)
                self.assertEqual(assessed["breakdown"]["impact"], points)

    def test_global_and_national_new_claims_are_visible(self):
        with connect(":memory:") as db:
            for location in ("", "United States"):
                item = observation("Navy sailor suicide attempts prompt crisis response",
                                   location=location)
                item.external_id = location or "global"
                claim_id, _ = ingest(db, item)
                self.assertIn(claim_id, [x["id"] for x in queue(db, now=NOW)])
            civil = observation("ICE-shooting bystander video", category="civil-unrest",
                                location="")
            civil.external_id = "civil"
            claim_id, _ = ingest(db, civil)
            self.assertIn(claim_id, [x["id"] for x in queue(db, now=NOW)])

    def test_suppressed_state_and_dry_run_review(self):
        with connect(":memory:") as db:
            item = observation("Fatal TX shooting")
            claim_id, _ = ingest(db, item)
            db.execute("UPDATE claims SET status='SUPPRESSED' WHERE id=?", (claim_id,))
            db.execute("INSERT INTO workflow_audit(claim_id,previous_status,new_status,reason,changed_at) VALUES(?,?,?,?,?)",
                       (claim_id, "NEW", "SUPPRESSED", "No established domestic/regional impact", PUBLISHED))
            self.assertNotIn(claim_id, [x["id"] for x in queue(db, now=NOW)])
            shown = queue(db, now=NOW, include_suppressed=True)
            self.assertEqual(shown[0]["status"], "SUPPRESSED")
            self.assertEqual(queue(db, now=NOW, status="SUPPRESSED")[0]["id"], claim_id)
            report = suppression_review(db, now=NOW)
            self.assertEqual(report[0]["id"], claim_id)
            self.assertTrue(report[0]["would_be_visible"])
            self.assertEqual(db.execute("SELECT status FROM claims WHERE id=?", (claim_id,)).fetchone()[0],
                             "SUPPRESSED")

    def test_housekeeping_only_suppresses_routine_title(self):
        with connect(":memory:") as db:
            severe, _ = ingest(db, observation("Fatal shooting abroad"))
            routine = observation("Weekly newsletter", body="Community events")
            routine.external_id = "routine"
            routine_id, _ = ingest(db, routine)
            changes = housekeeping(db, suppress=True, now=NOW)
            self.assertEqual([x["id"] for x in changes], [routine_id])
            self.assertNotEqual(severe, routine_id)
            housekeeping(db, suppress=True, apply=True, now=NOW)
            self.assertEqual(queue(db, now=NOW, status="SUPPRESSED")[0]["id"], routine_id)

    def test_desktop_relay_uses_operator_basis(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.toml"
            config.write_text("[operator]\ncallsign='TEST'\n[collection]\ndatabase='test.db'\n")
            service = DesktopService(config)
            with connect(service.settings.database) as db:
                item = observation("Witness reports shooting", category="civil-unrest")
                item.source_type = "community"
                claim_id, _ = ingest(db, item)
            service.mark(claim_id, "REVIEW")
            with self.assertRaises(ValueError):
                service.mark(claim_id, "TX_CANDIDATE")
            service.mark(claim_id, "TX_CANDIDATE", operator_override=True)
            with connect(service.settings.database) as db:
                row, _ = claim_detail(db, claim_id)
                self.assertEqual((row["status"], row["tx_candidate_basis"]),
                                 ("TX_CANDIDATE", "operator_relay"))

    def test_swpc_parser(self):
        source = SourceConfig("swpc", "swpc_alerts", "SWPC", "https://example.test/swpc",
                              "official", "swpc")
        payload = [{"product_id": "K07", "issue_datetime": PUBLISHED,
                    "message": "ALERT: K-index of 7\nGeomagnetic storm"}]
        parsed = parse_swpc(source, json.dumps(payload).encode())
        self.assertEqual((len(parsed), parsed[0].category, parsed[0].external_id),
                         (1, "space-weather", "K07|" + PUBLISHED))
        self.assertIn("K-index of 7", parsed[0].body)

    def test_wzdx_cap_partial_and_full_failure(self):
        source = SourceConfig("wz", "wzdx_registry", "WZDx", "https://example.test/registry",
                              "official", "wz", max_requests_per_poll=3)
        registry = json.dumps({"feeds": [{"feed_url": f"https://example.test/{n}"} for n in range(5)]}).encode()
        calls = []
        def fetch(url, **kwargs):
            calls.append(url)
            if url.endswith("registry"): return registry
            if url.endswith("/1"): raise CollectionError("HTTP 503")
            return json.dumps({"features": [{"id": "ok", "properties": {"roadName": "I-40"}}]}).encode()
        with patch("shade_node.collectors.fetch_bytes", side_effect=fetch):
            with self.assertRaises(PartialCollectionError) as caught:
                collect(source, user_agent="test", timeout=1, max_bytes=10000)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(caught.exception.observations), 1)
        with patch("shade_node.collectors.fetch_bytes",
                   side_effect=lambda url, **kw: registry if url.endswith("registry") else
                   (_ for _ in ()).throw(CollectionError("HTTP 503"))):
            with self.assertRaises(CollectionError):
                collect(source, user_agent="test", timeout=1, max_bytes=10000)

    def test_mastodon_link_pagination_cap(self):
        source = SourceConfig("mast", "mastodon_hashtag", "Mastodon",
                              "https://example.test/start", "community", "mast",
                              hashtag="news", max_requests_per_poll=2)
        calls = []
        def fetch(url, **kwargs):
            calls.append(url)
            return b"[]", {"Link": '<https://example.test/next>; rel="next"'}
        with patch("shade_node.collectors.fetch_bytes", side_effect=fetch):
            collect(source, user_agent="test", timeout=1, max_bytes=1000)
        self.assertEqual(calls, ["https://example.test/start", "https://example.test/next"])

    def test_google_news_cap(self):
        source = SourceConfig("news", "google_news_search", "News", "https://example.test/rss",
                              "media", "news", query_terms=["a", "b", "c"],
                              max_requests_per_poll=2)
        with patch("shade_node.collectors.fetch_bytes", return_value=b"<rss/>") as fetch:
            collect(source, user_agent="test", timeout=1, max_bytes=1000)
        self.assertEqual(fetch.call_count, 2)

    def test_nps_credential_header(self):
        source = SourceConfig("nps", "nps_alerts", "NPS", "https://example.test/alerts",
                              "official", "nps", api_key_env="NPS_TEST_KEY")
        with patch.dict("os.environ", {"NPS_TEST_KEY": "secret"}), patch(
                "shade_node.collectors.fetch_bytes", return_value=b'{"data":[]}') as fetch:
            collect(source, user_agent="test", timeout=1, max_bytes=1000)
        self.assertEqual(fetch.call_args.kwargs["headers"]["X-Api-Key"], "secret")

    def test_conditional_request_and_unchanged_poll(self):
        source = SourceConfig("rss", "rss", "RSS", "https://example.test/rss",
                              "media", "rss", min_poll_seconds=60)
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(str(Path(folder) / "test.db"), 7, 10, 1000, "test",
                                "TEST", "TEST", "TEST", 500, "standard", 35, 10,
                                "TEST", "TEST", 500, [source])
            def first(url, **kwargs):
                return b"<rss/>", {"ETag": '"abc"'}
            with patch("shade_node.collectors.fetch_bytes", side_effect=first):
                run_once(settings)
            with connect(settings.database) as db:
                db.execute("UPDATE source_polls SET next_allowed='2000-01-01T00:00:00Z'")
            def second(url, **kwargs):
                self.assertEqual(kwargs["headers"]["If-None-Match"], '"abc"')
                raise NotModified()
            with patch("shade_node.collectors.fetch_bytes", side_effect=second):
                result = run_once(settings)
            self.assertEqual(result["sources_ok"], 1)
            with connect(settings.database) as db:
                self.assertEqual(db.execute("SELECT etag FROM source_http_cache").fetchone()[0], '"abc"')

    def test_current_schema_fast_path_does_not_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "test.db")
            with connect(path): pass
            with patch("shade_node.db.backup_database", side_effect=AssertionError("backup")):
                with connect(path): pass
            self.assertEqual(list(Path(folder).glob("*.bak")), [])

    def test_partial_wzdx_poll_is_recorded_and_observations_kept(self):
        source = SourceConfig("wz", "wzdx_registry", "WZDx",
                              "https://example.test/registry", "official", "wz")
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(str(Path(folder) / "test.db"), 7, 10, 1000, "test",
                                "TEST", "TEST", "TEST", 500, "standard", 35, 10,
                                "TEST", "TEST", 500, [source])
            item = observation("I-40 bridge closure")
            item.published_at = datetime.now(timezone.utc).isoformat()
            with patch("shade_node.engine.collect",
                       side_effect=PartialCollectionError("HTTP 503 on child feed", [item])):
                result = run_once(settings)
            self.assertEqual((result["sources_partial"], result["inserted"]), (1, 1))
            with connect(settings.database) as db:
                poll = db.execute("SELECT success,error FROM source_polls WHERE source_id='wz'").fetchone()
                self.assertEqual((poll["success"], poll["error"]),
                                 (0, "HTTP 503 on child feed"))

    def test_incomplete_current_database_gets_verified_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "test.db")
            with connect(path): pass
            with closing(sqlite3.connect(path)) as db:
                db.execute("DROP TABLE source_http_cache")
            with connect(path) as db:
                self.assertIsNotNone(db.execute(
                    "SELECT name FROM sqlite_master WHERE name='source_http_cache'").fetchone())
            backups = list(Path(folder).glob("*.bak"))
            self.assertEqual(len(backups), 1)
            with closing(sqlite3.connect(backups[0])) as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_desktop_relay_requires_confirmation(self):
        import tkinter as tk
        from shade_node.gui import ShadeWindow
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.toml"
            config.write_text("""[operator]
callsign='TEST'
[collection]
database='test.db'
""")
            service = DesktopService(config)
            with connect(service.settings.database) as db:
                item = observation("Witness reports shooting", category="civil-unrest")
                item.source_type = "community"
                # The shared fixture date (PUBLISHED) has aged past the freshness
                # window, which hid this report from the Inbox. This test is about
                # the relay confirmation, so give the report a current date.
                from datetime import datetime, timedelta, timezone
                item.published_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
                item.observed_at = item.published_at
                claim_id, _ = ingest(db, item)
            service.mark(claim_id, "REVIEW")
            try:
                root = tk.Tk()
            except tk.TclError as exc:
                self.skipTest(str(exc))
            root.withdraw()
            app = ShadeWindow(root, service)
            self.addCleanup(lambda: (app.pool.shutdown(wait=True), root.destroy()))
            import time
            def settle():
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    root.update()
                    if not app.busy:
                        root.update()
                        if not app.busy: break
                    time.sleep(.01)
                self.assertFalse(app.busy)
            settle()
            app.tree.selection_set(str(claim_id))
            settle()
            with patch("shade_node.gui.messagebox.askyesno", return_value=False) as confirm:
                app.relay()
                confirm.assert_called_once()
            self.assertEqual(service.detail(claim_id)[0]["status"], "REVIEW")
            with patch("shade_node.gui.messagebox.askyesno", return_value=True):
                app.relay()
                settle()
            self.assertEqual(service.detail(claim_id)[0]["tx_candidate_basis"],
                             "operator_relay")


    def test_doctor_reports_disabled_source_last_failure(self):
        from contextlib import redirect_stdout
        from shade_node.cli import main
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.toml"
            config.write_text("""[operator]
callsign='TEST'
network='TEST'
region_label='TEST'
[collection]
database='test.db'
user_agent='SHADE test@example.test'
[[sources]]
id='disabled'
kind='rss'
name='Disabled'
url='https://example.test/rss'
source_type='media'
source_family='disabled'
enabled=false
""")
            with connect(str(Path(folder) / "test.db")) as db:
                db.execute("INSERT INTO source_polls VALUES(?,?,?,?,?)",
                           ("disabled", PUBLISHED, PUBLISHED, 0, "HTTP 404"))
            output = io.StringIO()
            with redirect_stdout(output):
                result = main(["--config", str(config), "doctor"])
            self.assertEqual(result, 1)
            self.assertIn("Disabled source, last failure: disabled: HTTP 404",
                          output.getvalue())


    def test_v5_migration_reconciles_persisted_confidence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "test.db")
            with connect(path) as db:
                item = observation("Major outage reported near Knoxville terminal",
                                   location="Knoxville TN")
                item.source_family = "media-a"
                claim_id, _ = ingest(db, item)
                other = observation(item.title, location=item.location)
                other.source_id = "community"
                other.source_family = "witness"
                other.source_type = "community"
                other.external_id = "second"
                ingest(db, other)
                db.execute("UPDATE claims SET confidence_label='CORROBORATED' WHERE id=?",
                           (claim_id,))
                db.execute("PRAGMA user_version=4")
            with connect(path, allow_migrate=True) as db:
                row = db.execute("SELECT confidence_label FROM claims WHERE id=?",
                                 (claim_id,)).fetchone()
                self.assertEqual(row[0], "REPORTED")
            self.assertEqual(len(list(Path(folder).glob("*.bak"))), 1)


    def test_radnet_request_uses_current_utc_year(self):
        current_year = datetime.now(timezone.utc).year
        csv_data = b"""date/time,exposure rate,unit,location
2026-09-23T12:00:00Z,0.1,uR,Knoxville
"""
        for city in ("knoxville", "nashville"):
            with self.subTest(city=city):
                source = SourceConfig("radnet-" + city, "epa_radnet", "RadNet",
                    "https://radnet.epa.gov/cdx-radnet-rest/api/rest/csv/2020/fixed/tn/" + city,
                    "official", "epa-radnet", area=city)
                with patch("shade_node.collectors.fetch_bytes", return_value=csv_data) as fetch:
                    rows = collect(source, user_agent="test", timeout=1, max_bytes=1000)
                requested_url = fetch.call_args.args[0]
                self.assertIn("/csv/" + str(current_year) + "/fixed/", requested_url)
                self.assertEqual(rows[0].url, requested_url)



if __name__ == "__main__":
    unittest.main()



