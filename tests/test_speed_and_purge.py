"""Speed pass (2026-10-09): cross-refresh scoring cache, change probe and the
72-hour purge. Disposable databases only."""
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shade_node import relevance
from shade_node.db import connect, ingest, transition, purge_plan, purge, compact, queue
from shade_node.desktop import DesktopService
from shade_node.model import Observation
from shade_node.relevance import DEFAULT_POLICY, evaluate, metadata

NOW = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)


def obs(external, title, observed, source_type='official', family='agency'):
    stamp = observed.isoformat().replace('+00:00', 'Z')
    return Observation('fixture-' + family, 'Fixture ' + family, source_type, family, external, title,
                       'Service outage confirmed near Knoxville', 'https://example.test/' + external,
                       category='infrastructure', location='Knoxville TN', severity='severe',
                       published_at=stamp, observed_at=stamp)


class StaticSignalCacheTests(unittest.TestCase):
    def row(self, **extra):
        row = {'title': 'Water main break', 'body': 'crews on scene', 'location': 'Sevier County',
               'category': 'infrastructure', 'source_type': 'official', 'source_family': 'x',
               'severity': 'unknown', 'published_at': '2026-10-09T20:00:00Z',
               'observed_at': '2026-10-09T20:00:00Z', 'raw_json': '{}'}
        row.update(extra)
        return row

    def test_cached_result_equals_uncached(self):
        relevance._STATIC_CACHE.clear()
        cold = evaluate(self.row(), DEFAULT_POLICY, NOW)
        warm = evaluate(self.row(), DEFAULT_POLICY, NOW)
        self.assertEqual(cold, warm)
        self.assertTrue(relevance._STATIC_CACHE)

    def test_policy_geography_change_is_a_cache_miss(self):
        relevance._STATIC_CACHE.clear()
        local = evaluate(self.row(location='Sevier County'), DEFAULT_POLICY, NOW)
        other = dict(DEFAULT_POLICY, local_terms=[], local_counties=[])
        moved = evaluate(self.row(location='Sevier County'), other, NOW)
        relevance._STATIC_CACHE.clear()
        self.assertEqual(moved, evaluate(self.row(location='Sevier County'), other, NOW))
        self.assertEqual(local['area'], evaluate(self.row(location='Sevier County'), DEFAULT_POLICY, NOW)['area'])

    def test_text_change_is_a_cache_miss(self):
        relevance._STATIC_CACHE.clear()
        quiet = evaluate(self.row(title='Library hours'), DEFAULT_POLICY, NOW)
        loud = evaluate(self.row(title='Tornado destroys homes, fatalities reported'), DEFAULT_POLICY, NOW)
        self.assertNotEqual(quiet['breakdown']['impact'], loud['breakdown']['impact'])

    def test_freshness_still_follows_the_clock(self):
        relevance._STATIC_CACHE.clear()
        fresh = evaluate(self.row(), DEFAULT_POLICY, NOW)
        later = evaluate(self.row(), DEFAULT_POLICY, NOW + timedelta(days=5))
        self.assertGreater(fresh['breakdown']['freshness'], later['breakdown']['freshness'])


class PurgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'shade.db')
        old, fresh = NOW - timedelta(hours=100), NOW - timedelta(hours=2)
        with connect(self.path) as db:
            self.old_new, _ = ingest(db, obs('a', 'Old outage one', old))
            self.old_tx, _ = ingest(db, obs('b', 'Old outage relayed', old, family='agency2'))
            self.old_sent, _ = ingest(db, obs('c', 'Old outage sent', old, family='agency3'))
            self.fresh, _ = ingest(db, obs('d', 'Fresh outage', fresh, family='agency4'))
            for claim, path in ((self.old_tx, ['REVIEW', 'TX_CANDIDATE']), (self.old_sent, ['REVIEW', 'TX_CANDIDATE', 'SENT'])):
                for status in path:
                    transition(db, claim, status)
            db.commit()

    def plan(self, hours=72):
        with connect(self.path) as db:
            return purge_plan(db, hours, now=NOW)

    def test_plan_selects_only_unprotected_old_claims(self):
        plan = self.plan()
        self.assertEqual(plan['claim_ids'], [self.old_new])
        self.assertEqual(plan['protected_old'], 2)

    def test_hours_are_capped_at_72(self):
        with self.assertRaises(ValueError):
            self.plan(73)
        with self.assertRaises(ValueError):
            self.plan(0)
        with self.assertRaises(ValueError):
            self.plan(0.5)
        self.assertEqual(self.plan(1)['claim_ids'], [self.old_new, self.fresh])  # 1 h window also takes the 2 h-old report
        self.assertEqual(self.plan(24)['claim_ids'], [self.old_new])

    def test_purge_removes_claim_and_dependents_without_fk_violations(self):
        plan = self.plan()
        with connect(self.path) as db:
            result = purge(db, plan)
            self.assertEqual(result, {'claims': 1, 'observations': 1})
            self.assertIsNone(db.execute('SELECT 1 FROM claims WHERE id=?', (self.old_new,)).fetchone())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observations WHERE claim_id=?', (self.old_new,)).fetchone()[0], 0)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            kept = {r[0] for r in db.execute('SELECT id FROM claims')}
        self.assertEqual(kept, {self.old_tx, self.old_sent, self.fresh})

    def test_reseen_old_report_is_purged_by_first_collection_and_stays_gone(self):
        with connect(self.path) as db:
            rejected, _ = ingest(db, obs('e', 'Old routine notice', NOW - timedelta(hours=90), family='agency5'))
            transition(db, rejected, 'REJECTED')
            # the feed keeps serving it: last_seen is fresh, first_seen is old
            db.execute('UPDATE claims SET last_seen=? WHERE id=?', ((NOW - timedelta(minutes=5)).isoformat(), rejected))
            db.commit()
        plan = self.plan()
        self.assertIn(rejected, plan['claim_ids'])
        self.assertEqual(plan['by_status'].get('REJECTED'), 1)
        with connect(self.path) as db:
            purge(db, plan)
            again = obs('e', 'Old routine notice', NOW - timedelta(hours=90), family='agency5')
            self.assertEqual(ingest(db, again), (0, False))           # tombstoned: does not come back as NEW
            self.assertIsNone(db.execute("SELECT 1 FROM observations WHERE external_id='e'").fetchone())
            self.assertIsNotNone(ingest(db, obs('f', 'Brand new outage report', NOW, family='agency6'))[0])

    def test_old_story_collected_today_is_purged_by_its_age(self):
        """Inbox Age is the publish time. A week-old story SHADE only first collected an hour ago
        (new feed, feed re-serving its back catalog) must still go."""
        with connect(self.path) as db:
            late, _ = ingest(db, obs('g', 'Week old flood story', NOW - timedelta(days=7), family='agency7'))
            undated, _ = ingest(db, obs('h', 'Undated bulletin', NOW - timedelta(days=7), family='agency8'))
            db.execute("UPDATE observations SET published_at='' WHERE claim_id=?", (undated,))
            db.execute('UPDATE claims SET first_seen=? WHERE id IN (?,?)',
                       ((NOW - timedelta(hours=1)).isoformat().replace('+00:00', 'Z'), late, undated))
            db.commit()
        ids = self.plan()['claim_ids']
        self.assertIn(late, ids)
        self.assertNotIn(undated, ids)          # no publish date: only first collection decides
        self.assertNotIn(self.fresh, ids)

    def test_merged_child_of_protected_claim_is_kept(self):
        with connect(self.path) as db:
            db.execute('INSERT INTO claim_merges(child_id,parent_id,merged_at) VALUES(?,?,?)',
                       (self.old_new, self.old_tx, NOW.isoformat()))
            db.commit()
        self.assertEqual(self.plan()['claim_ids'], [])

    def test_stale_plan_is_refused(self):
        plan = self.plan()
        with connect(self.path) as db:
            transition(db, self.old_new, 'REVIEW'); transition(db, self.old_new, 'TX_CANDIDATE'); db.commit()
            with self.assertRaises(ValueError):
                purge(db, plan)
            self.assertIsNotNone(db.execute('SELECT 1 FROM claims WHERE id=?', (self.old_new,)).fetchone())

    def test_compact_switches_to_wal(self):
        from contextlib import closing
        self.assertEqual(compact(self.path), 'wal')
        # closing(): sqlite3's own context manager only commits; an open
        # handle would keep Windows from deleting the temp folder.
        with closing(sqlite3.connect(self.path)) as raw:
            self.assertEqual(raw.execute('PRAGMA journal_mode').fetchone()[0], 'wal')


class ChangeProbeTests(unittest.TestCase):
    def test_signature_changes_on_new_evidence_and_workflow(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'config.toml'
            config.write_text('[operator]\ncallsign="TEST"\n')
            service = DesktopService(config)
            with connect(service.settings.database) as db:
                claim, _ = ingest(db, obs('a', 'Outage', NOW)); db.commit()
            first = service.data_signature()
            self.assertEqual(first, service.data_signature())
            service.mark(claim, 'REVIEW')
            second = service.data_signature()
            self.assertNotEqual(first, second)
            with connect(service.settings.database) as db:
                ingest(db, obs('b', 'Another outage', NOW, family='other')); db.commit()
            self.assertNotEqual(second, service.data_signature())


if __name__ == '__main__':
    unittest.main()
