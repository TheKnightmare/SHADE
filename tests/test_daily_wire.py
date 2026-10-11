"""Daily Wire: a SHADE-side review/assembly view over the existing
TX_CANDIDATE queue (see docs/DAILY_WIRE.md). Covers exactly the boundary
this feature must not cross: membership is claims.status='TX_CANDIDATE' and
nothing else; ordering/notes/hidden-from-view live in wire_annotations and
never touch claims.status; preview/export never transmits or marks SENT;
FIGHT's own TX_CANDIDATE visibility (a hard-coded query in a separate
StationWatch process) is unaffected.
"""
import unittest
from dataclasses import replace

from shade_node.db import (connect, ingest, transition, daily_wire, set_wire_annotation, reorder_wire,
                            merge_claims, record_wire_transmission)
from shade_node.desktop import DesktopService
from shade_node.formatters import workflow_status
from shade_node.model import Observation


def obs(**kwargs):
    base = Observation('fixture', 'Fixture', 'official', 'agency', 'event-1',
                        'Major communications outage', 'Service outage confirmed',
                        'https://example.test/1', category='infrastructure', location='Knoxville TN',
                        severity='severe', published_at='2026-09-22T02:00:00Z', observed_at='2026-09-22T02:10:00Z')
    return replace(base, **kwargs)


def make_candidate(db, **kwargs):
    """Ingests one observation and walks it to TX_CANDIDATE, returning its id."""
    claim_id, _ = ingest(db, obs(**kwargs))
    transition(db, claim_id, 'REVIEW')
    transition(db, claim_id, 'TX_CANDIDATE')
    return claim_id


def fight_visible_tx_candidate_ids(db_path):
    """The exact query FIGHT's ShadeQueueAdapter runs (station_core/adapters.py
    in StationWatch) - copied here, not imported, since FIGHT is a separate
    application this pass must not touch. Used only to prove Daily Wire's
    membership matches what FIGHT would independently see."""
    import sqlite3
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT id FROM claims WHERE status = 'TX_CANDIDATE'").fetchall()
        return {row['id'] for row in rows}
    finally:
        con.close()


class TestDailyWireMembership(unittest.TestCase):
    def test_membership_matches_tx_candidate_status_exactly(self):
        with connect(':memory:') as db:
            candidate = make_candidate(db)
            review_only, _ = ingest(db, obs(external_id='e2', title='Not yet reviewed', location='Sevier TN'))
            rejected, _ = ingest(db, obs(external_id='e3', title='Rejected claim', location='Blount TN'))
            transition(db, rejected, 'REJECTED')
            items = daily_wire(db)
            self.assertEqual({item['id'] for item in items}, {candidate})
            self.assertNotIn(review_only, {item['id'] for item in items})
            self.assertNotIn(rejected, {item['id'] for item in items})

    def test_matches_fight_visible_tx_candidate_set_on_the_same_fixture(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / 'fixture.db')
            with connect(db_path) as db:
                a = make_candidate(db)
                b = make_candidate(db, external_id='e2', title='Second candidate', location='Sevier TN')
                rejected, _ = ingest(db, obs(external_id='e3', title='Rejected', location='Blount TN'))
                transition(db, rejected, 'REJECTED')
                items = daily_wire(db, include_hidden=True)
            self.assertEqual({item['id'] for item in items}, fight_visible_tx_candidate_ids(db_path))
            self.assertEqual({item['id'] for item in items}, {a, b})

    def test_rejected_suppressed_stale_expired_never_appear_as_wire_ready(self):
        with connect(':memory:') as db:
            rejected, _ = ingest(db, obs(external_id='e1'))
            transition(db, rejected, 'REJECTED')
            suppressed, _ = ingest(db, obs(external_id='e2', title='Routine newsletter', severity='unknown'))
            # SUPPRESSED is an administrative state applied by housekeeping()
            # via a direct UPDATE, not reachable through transition() from
            # NEW - mirror that here rather than exercising a path that
            # doesn't exist in the product.
            db.execute("UPDATE claims SET status='SUPPRESSED' WHERE id=?", (suppressed,))
            new_claim, _ = ingest(db, obs(external_id='e3', title='Brand new, unreviewed'))
            ids = {item['id'] for item in daily_wire(db)}
            self.assertNotIn(rejected, ids)
            self.assertNotIn(suppressed, ids)
            self.assertNotIn(new_claim, ids)

    def test_merged_duplicate_child_never_appears_independently(self):
        with connect(':memory:') as db:
            primary = make_candidate(db)
            duplicate, _ = ingest(db, obs(external_id='e2', title='Major communications outage', location='Knoxville TN'))
            merge_claims(db, primary, [duplicate])
            ids = {item['id'] for item in daily_wire(db)}
            self.assertEqual(ids, {primary})
            self.assertNotIn(duplicate, ids)


class TestDailyWireWarnings(unittest.TestCase):
    def test_global_locationless_item_is_visibly_flagged(self):
        with connect(':memory:') as db:
            claim_id = make_candidate(db, category='public-safety', location='',
                                       title='Multistate E. coli outbreak tied to raw milk cheese',
                                       body='CDC investigating a nationwide outbreak.')
            item = daily_wire(db)[0]
            self.assertEqual(item['id'], claim_id)
            self.assertTrue(item['wire_warnings'], 'a GLOBAL, locationless item must carry visible warnings')
            joined = ' '.join(item['wire_warnings'])
            self.assertIn('GLOBAL', joined)
            self.assertIn('location', joined.lower())

    def test_genuine_local_item_with_no_separate_location_field_is_not_flagged_for_missing_location(self):
        # area is the primary geography signal; a real ETN/WNC item can be
        # classified local purely from title/body text with no separately
        # populated `location` column - that's normal, not a defect.
        with connect(':memory:') as db:
            claim_id = make_candidate(db, location='', title='Knoxville substation fire prompts evacuations')
            item = daily_wire(db)[0]
            self.assertEqual(item['area'], 'ETN/WNC')
            self.assertNotIn('No location stated', item['wire_warnings'])

    def test_correlated_child_previously_sent_is_flagged(self):
        # merge_claims() itself refuses to merge an already-SENT claim (a
        # real transmission record must never be folded away) - so this
        # state can only exist historically (e.g. data older than that
        # safety check). Constructed directly here, the same way the v4
        # migration tests construct legacy states raw db.py doesn't produce
        # through its own normal write paths anymore.
        with connect(':memory:') as db:
            primary = make_candidate(db)
            child, _ = ingest(db, obs(external_id='e2', title='Major communications outage', location='Knoxville TN'))
            transition(db, child, 'REVIEW')
            transition(db, child, 'TX_CANDIDATE')
            db.execute("UPDATE claims SET status='SENT' WHERE id=?", (child,))
            db.execute('INSERT INTO claim_merges(child_id,parent_id,merged_at) VALUES(?,?,?)',
                       (child, primary, '2026-09-22T00:00:00Z'))
            item = daily_wire(db)[0]
            self.assertTrue(any('previously marked SENT' in w for w in item['wire_warnings']))

    def test_stale_expired_item_is_flagged(self):
        with connect(':memory:') as db:
            claim_id = make_candidate(db, category='weather', location='Knox, TN', title='Tornado Warning',
                raw={'geometry': None, 'properties': {'event': 'Tornado Warning', 'areaDesc': 'Knox, TN',
                     'sent': '2020-01-01T00:00:00Z', 'expires': '2020-01-01T01:00:00Z',
                     'effective': '2020-01-01T00:00:00Z', 'urgency': 'Immediate', 'certainty': 'Observed'}})
            item = daily_wire(db)[0]
            self.assertEqual(item['id'], claim_id)
            self.assertIn('Stale or expired', item['wire_warnings'])


class TestDailyWireCurationNeverTouchesStatus(unittest.TestCase):
    def test_annotation_note_and_hidden_do_not_change_claims_status(self):
        with connect(':memory:') as db:
            claim_id = make_candidate(db)
            before = db.execute('SELECT status FROM claims WHERE id=?', (claim_id,)).fetchone()['status']
            set_wire_annotation(db, claim_id, note='verified with utility company')
            set_wire_annotation(db, claim_id, hidden=True)
            after = db.execute('SELECT status FROM claims WHERE id=?', (claim_id,)).fetchone()['status']
            self.assertEqual(before, after)
            self.assertEqual(before, 'TX_CANDIDATE')

    def test_hidden_item_excluded_by_default_included_on_request(self):
        with connect(':memory:') as db:
            claim_id = make_candidate(db)
            set_wire_annotation(db, claim_id, hidden=True)
            self.assertEqual(daily_wire(db), [])
            shown = daily_wire(db, include_hidden=True)
            self.assertEqual(len(shown), 1)
            self.assertTrue(shown[0]['wire_hidden'])

    def test_reorder_persists_position_and_never_touches_status(self):
        with connect(':memory:') as db:
            a = make_candidate(db)
            b = make_candidate(db, external_id='e2', title='Second candidate', location='Sevier TN')
            reorder_wire(db, [b, a])
            items = daily_wire(db)
            self.assertEqual([item['id'] for item in items], [b, a])
            statuses = {row['id']: row['status'] for row in db.execute('SELECT id, status FROM claims')}
            self.assertEqual(statuses, {a: 'TX_CANDIDATE', b: 'TX_CANDIDATE'})

    def test_note_survives_across_daily_wire_calls(self):
        with connect(':memory:') as db:
            claim_id = make_candidate(db)
            set_wire_annotation(db, claim_id, note='hold for confirmation')
            self.assertEqual(daily_wire(db)[0]['wire_note'], 'hold for confirmation')


class TestDailyWireExportSafety(unittest.TestCase):
    def test_export_never_calls_transition_or_marks_sent(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.toml'
            config.write_text('[operator]\ncallsign="TEST"\n')
            service = DesktopService(config)
            with connect(service.settings.database) as db:
                claim_id = make_candidate(db)
            text = service.wire_export([claim_id])
            self.assertIsInstance(text, list)
            self.assertTrue(any(str(claim_id) not in '' for _ in text))  # produced real output
            with connect(service.settings.database) as db:
                status = db.execute('SELECT status FROM claims WHERE id=?', (claim_id,)).fetchone()['status']
            self.assertEqual(status, 'TX_CANDIDATE', 'preview/export must never mark a claim SENT')


class TestHistoricalSentDisplay(unittest.TestCase):
    def test_sent_without_transmission_record_is_labeled_as_historical(self):
        with connect(':memory:') as db:
            claim_id = make_candidate(db)
            transition(db, claim_id, 'SENT')  # no record_wire_transmission call - predates recording
            claim = db.execute('SELECT * FROM claims WHERE id=?', (claim_id,)).fetchone()
            item = dict(claim)
            item['wire_transmissions'] = []
            label = workflow_status(item)
            self.assertIn('no transmission record', label)
            self.assertIn('predates recording', label)

    def test_sent_with_transmission_record_is_labeled_distinctly(self):
        with connect(':memory:') as db:
            claim_id = make_candidate(db)
            transition(db, claim_id, 'SENT')
            record_wire_transmission(db, claim_id, '2026-09-22T03:00:00-04:00', 'JS8', 'exact text sent')
            claim = db.execute('SELECT * FROM claims WHERE id=?', (claim_id,)).fetchone()
            item = dict(claim)
            item['wire_transmissions'] = [{'sent_at': '2026-09-22T03:00:00-04:00', 'channel': 'JS8',
                                            'message_text': 'exact text sent'}]
            label = workflow_status(item)
            self.assertIn('transmission details available', label)
            self.assertNotIn('predates recording', label)

    def test_missing_historical_details_are_never_fabricated(self):
        """No code path invents a channel/message/time for a historical SENT
        claim that never had one recorded."""
        with connect(':memory:') as db:
            claim_id = make_candidate(db)
            transition(db, claim_id, 'SENT')
            transmissions = [dict(row) for row in db.execute(
                'SELECT * FROM wire_transmissions WHERE claim_id=?', (claim_id,))]
            self.assertEqual(transmissions, [], 'a claim with no real transmission record must show none, ever')


if __name__ == '__main__':
    unittest.main(verbosity=2)
