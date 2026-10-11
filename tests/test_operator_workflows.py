"""Operator console workflows use disposable databases and no live source requests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shade_node.db import connect, ingest, claim_detail
from shade_node.desktop import DesktopService
from shade_node.formatters import traffic_message
from shade_node.inbox_sort import sorted_inbox
from shade_node.model import Observation


class OperatorWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = Path(self.temp.name) / 'config.toml'
        config.write_text('[operator]\ncallsign="TEST"\nregion_label="ETN/WNC"\n')
        self.service = DesktopService(config)

    def add(self, external, source_type='community', title='Major fire at county courthouse'):
        with connect(self.service.settings.database) as db:
            claim_id, _ = ingest(db, Observation('fixture', 'Fixture', source_type, 'fixture', external,
                title, 'Fire response continues', 'https://example.test/'+external,
                category='public-safety', location='Franklin County NC'))
        return claim_id

    def test_age_sort_uses_time_not_display_text(self):
        rows = [
            {'id': 1, 'published': '2026-09-24T02:00:00Z'},
            {'id': 2, 'published': '2026-09-25T01:00:00Z'},
            {'id': 3, 'published': '2026-09-23T23:00:00Z'},
        ]
        self.assertEqual([r['id'] for r in sorted_inbox(rows, 'age', True)], [2, 1, 3])
        self.assertEqual([r['id'] for r in sorted_inbox(rows, 'age')], [3, 1, 2])

    def test_bulk_reject_is_atomic(self):
        first = self.add('one')
        second = self.add('two', title='Another courthouse fire')
        self.assertEqual(self.service.mark_many([first, second], 'REJECTED'), 2)
        with connect(self.service.settings.database) as db:
            self.assertEqual([row[0] for row in db.execute(
                'SELECT status FROM claims WHERE id IN (?,?) ORDER BY id', (first, second))],
                ['REJECTED', 'REJECTED'])
        with self.assertRaises(ValueError):
            self.service.mark_many([first, second], 'CORRELATING')
        with connect(self.service.settings.database) as db:
            self.assertEqual([row[0] for row in db.execute(
                'SELECT status FROM claims WHERE id IN (?,?) ORDER BY id', (first, second))],
                ['REJECTED', 'REJECTED'])

    @patch('shade_node.operator_sources._public_https', side_effect=lambda value: value)
    def test_reviewed_article_enables_normal_tx_candidate(self, _):
        claim_id = self.add('social')
        self.service.mark(claim_id, 'REVIEW')
        with self.assertRaises(ValueError):
            self.service.mark(claim_id, 'TX_CANDIDATE')
        article = dict(url='https://www.cnn.com/story', title='Major fire at county courthouse',
                       publisher='CNN', description='Fire response continues',
                       published_at='2026-09-25T01:00:00Z')
        self.assertTrue(self.service.attach_article(claim_id, article))
        self.assertFalse(self.service.attach_article(claim_id, article))
        self.service.mark(claim_id, 'TX_CANDIDATE')
        with connect(self.service.settings.database) as db:
            claim, rows = claim_detail(db, claim_id)
            self.assertEqual(claim['tx_candidate_basis'], 'corroborated')
            self.assertEqual(len(rows), 2)
            self.assertTrue(any(row['source_family'] == 'www.cnn.com' for row in rows))

    def test_wire_copy_excludes_provenance_and_wrong_home_region(self):
        claim = dict(confidence_label='REPORTED', location='', area='REGIONAL',
                     title='Georgia court issues new ruling')
        message = traffic_message(claim, [], callsign='TEST', network='GHOSTNET',
                                  region_label='ETN/WNC')
        self.assertIn('/REGIONAL - ', message)
        for marker in ('SRC-FAMILIES:', 'OFFICIAL:', 'LEAD:', 'SUPPORT:', 'REVIEWED-BY:'):
            self.assertNotIn(marker, message)
        exercise = traffic_message(claim, [], callsign='TEST', network='GHOSTNET',
                                   region_label='ETN/WNC', mode='exercise')
        self.assertTrue(exercise.endswith(' EXERCISE'))


if __name__ == '__main__':
    unittest.main()
