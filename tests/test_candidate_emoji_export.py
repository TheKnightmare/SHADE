"""SHADE-side radio-copy and FIGHT snapshot emoji regression tests.

Disposable SQLite only. No live database, station config, or FIGHT process.
"""
import base64
import hashlib
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

from candidate_snapshot import export_candidate as adapter_export
from shade_node.candidate_snapshot import (
    CandidateUnavailable, export_candidate, list_candidates, validate_candidate,
)
from shade_node.db import connect, ingest, transition
from shade_node.formatters import strip_emoji, traffic_message
from shade_node.model import Observation


NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


class EmojiExportTests(unittest.TestCase):
    def test_emoji_sequences_preserve_words_punctuation_and_numbers(self):
        original = 'Café 🚨, crew 👩🏽‍🚒 responds 🇺🇸; Route 1️⃣ closed ☀️! #️⃣'
        self.assertEqual(strip_emoji(original),
                         'Café, crew responds; Route 1 closed! #')
        self.assertEqual(strip_emoji('Flood🏳️‍🌈 warning 👍🏽: use I-40.'),
                         'Flood warning: use I-40.')

    def test_traffic_message_respects_limit_after_normalization(self):
        claim = {'title': 'Bridge collapse 🚨, Route 1️⃣ closed 👩🏽‍🚒!',
                 'location': 'Knoxville, TN', 'area': 'ETN/WNC',
                 'confidence_label': 'OFFICIAL-REPORT'}
        message = traffic_message(claim, [], callsign='KQ4ODY', network='GHOSTNET',
                                  max_chars=86)
        self.assertEqual(message,
                         '@GHOSTNET OFFICIAL-REPORT/KNOXVILLE, TN - BRIDGE COLLAPSE, ROUTE 1 CLOSED!')
        self.assertLessEqual(len(message), 86)

    def test_candidate_export_text_base64_hash_and_validation_agree(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / 'shade.db'
            observation = Observation(
                'test-official', 'Test Agency', 'official', 'test-agency', 'event-1',
                'Bridge collapse 🚨, rescue crews 👩🏽‍🚒 respond 🇺🇸 near Knoxville; Route 1️⃣ closed ☀️!',
                'Two people injured.', 'https://example.test/bridge-collapse',
                category='public-safety', location='Knoxville, TN',
                published_at='2026-10-04T11:50:00Z',
                observed_at='2026-10-04T11:55:00Z',
            )
            with connect(str(db_path)) as db:
                claim_id, _ = ingest(db, observation)
                transition(db, claim_id, 'TX_CANDIDATE')
            snapshot = export_candidate(db_path, claim_id, callsign='KQ4ODY',
                                        network='GHOSTNET', now=NOW)
            expected = ('@GHOSTNET OFFICIAL-REPORT/KNOXVILLE, TN - '
                        'BRIDGE COLLAPSE, RESCUE CREWS RESPOND NEAR KNOXVILLE; ROUTE 1 CLOSED!')
            self.assertEqual(snapshot['text'], expected)
            payload = expected.encode('utf-8')
            self.assertEqual(base64.b64decode(snapshot['text_utf8_b64']), payload)
            self.assertEqual(snapshot['text_sha256'], hashlib.sha256(payload).hexdigest())
            self.assertEqual(validate_candidate(db_path, snapshot, now=NOW)['text'], expected)
            inbox = list_candidates(db_path, callsign='KQ4ODY', network='GHOSTNET',
                                    now=NOW)['candidates']
            self.assertEqual(inbox[0]['text'], expected)
            self.assertEqual(inbox[0]['text_sha256'], snapshot['text_sha256'])
            # The older standalone SHADE adapter uses the same shared formatter.
            legacy = adapter_export(db_path, claim_id, callsign='KQ4ODY',
                                    network='GHOSTNET', now=NOW)
            self.assertEqual(legacy['text'], expected)
            self.assertEqual(legacy['text_sha256'], snapshot['text_sha256'])
            tampered = {**snapshot, 'text': expected + '🚨'}
            with self.assertRaises(CandidateUnavailable):
                validate_candidate(db_path, tampered, now=NOW)


if __name__ == '__main__':
    unittest.main()


class StoryTextSeedTests(unittest.TestCase):
    def test_prefers_attached_article_then_official_and_skips_rss_link_bodies(self):
        from shade_node.candidate_snapshot import _story_text
        rss = {"source_id": "gnews", "source_type": "media",
               "body": '<a href="u">Lost hunter rescued from ravine</a>&nbsp;<font>FOX 5</font>'}
        official = {"source_id": "dnr", "source_type": "official",
                    "body": "Georgia DNR and Camp Merrill searched the Cohutta WMA after a Saturday call &amp; found him."}
        article = {"source_id": "operator-article", "source_type": "media",
                   "body": "The hunter was hoisted from a steep ravine at night by the Camp Merrill team."}
        title = "Lost hunter rescued from ravine"
        self.assertEqual(_story_text(title, [rss]), "")
        self.assertIn("& found him", _story_text(title, [rss, official]))
        self.assertTrue(_story_text(title, [rss, official, article]).startswith("The hunter was hoisted"))
        self.assertTrue(_story_text(title, [{"source_id": "x", "source_type": "media",
                                             "body": "word " * 600}]).endswith(" ..."))


class SuppressedCandidateTests(unittest.TestCase):
    def test_operator_tx_candidate_survives_suppression_with_warning(self):
        from unittest import mock
        import shade_node.candidate_snapshot as cs
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / 'shade.db'
            observation = Observation(
                'test-official', 'Test Agency', 'official', 'test-agency', 'event-2',
                'Power outages across North Georgia after high winds', 'Crews restoring service.',
                'https://example.test/outage', category='infrastructure', location='',
                published_at='2026-10-04T11:50:00Z', observed_at='2026-10-04T11:55:00Z')
            with connect(str(db_path)) as db:
                claim_id, _ = ingest(db, observation)
                transition(db, claim_id, 'TX_CANDIDATE')
            real = cs.claim_detail

            def suppressed(*args, **kwargs):
                claim, rows = real(*args, **kwargs)
                return {**claim, 'reason': 'Weak or unclear regional impact'}, rows
            with mock.patch.object(cs, 'claim_detail', suppressed):
                snap = export_candidate(db_path, claim_id, callsign='KQ4ODY', network='GHOSTNET', now=NOW)
                inbox = list_candidates(db_path, callsign='KQ4ODY', network='GHOSTNET', now=NOW)
            self.assertIn('SHADE suppression overridden by operator TX_CANDIDATE: Weak or unclear regional impact',
                          snap['warnings'])
            self.assertEqual([c['claim_id'] for c in inbox['candidates']], [claim_id])
            self.assertIn('SHADE suppressed: Weak or unclear regional impact', inbox['candidates'][0]['wire_warnings'])
