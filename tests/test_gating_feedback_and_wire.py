"""New coverage for the sports/global/regional gating tightening, NOTAM
triage, operator feedback, TX_CANDIDATE-from-anywhere (including bulk), the
Daily Wire CLI, and a large-claim-set performance regression check.

Disposable in-memory/temp databases only - no live network, no live radio,
no dependency on the station's real production shade.db.
"""
import io
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone

from shade_node.model import Observation
from shade_node.db import (connect, ingest, queue, claim_detail, transition, record_feedback,
                            feedback_report, bulk_transition, ALLOWED_TRANSITIONS)
from shade_node.relevance import evaluate, DEFAULT_POLICY
from shade_node.desktop import DesktopService
from shade_node.cli import main

NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)


def obs(**kwargs):
    base = Observation('fixture', 'Fixture', 'official', 'agency', 'event-1',
                        'Untitled report', 'Body text', 'https://example.test/1',
                        category='public-safety', location='Knoxville, TN', severity='unknown',
                        published_at='2026-09-26T11:50:00Z', observed_at='2026-09-26T11:55:00Z')
    return replace(base, **kwargs)


def visible(o, lane='inbox', **kwargs):
    with connect(':memory:') as db:
        ingest(db, o)
        return queue(db, lane=lane, now=NOW, **kwargs)


# --------------------------------------------------------------------------- 1/2. sports gating

class TestSportsGating(unittest.TestCase):
    def test_routine_sports_update_excluded(self):
        # public-safety already has its own content gate (no event signal at
        # all here) - the item is correctly excluded either way; what matters
        # functionally is that it never reaches the operator queue.
        o = obs(category='public-safety',
                title='Texas out to an early lead in matchup vs. Tennessee')
        rows = visible(o)
        self.assertEqual(rows, [])
        with connect(':memory:') as db:
            ingest(db, o)
            rows_all = queue(db, lane='inbox', now=NOW, include_suppressed=True)
            self.assertEqual(len(rows_all), 1)
            self.assertTrue(rows_all[0]['reason'])  # excluded WITH a visible reason, not silently dropped

    def test_sports_specific_reason_on_a_category_with_no_other_content_gate(self):
        # 'infrastructure' has no dedicated content gate of its own (falls to
        # the generic OPERATIONAL pass-through) - this isolates the new,
        # sports-specific universal check from public-safety's separate gate.
        o = obs(category='infrastructure',
                title='Texas out to an early lead in matchup vs. Tennessee')
        rows = visible(o)
        self.assertEqual(rows, [])
        with connect(':memory:') as db:
            ingest(db, o)
            rows_all = queue(db, lane='inbox', now=NOW, include_suppressed=True)
            self.assertIn('sports', rows_all[0]['reason'].lower())

    def test_sports_with_extraordinary_local_impact_is_flagged(self):
        o = obs(category='public-safety',
                title='Two killed after stadium bleacher collapse during matchup vs. rival')
        rows = visible(o)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['reason'], '')

    def test_sports_pattern_does_not_flag_ordinary_public_safety_headline(self):
        o = obs(category='public-safety', title='Two arrested after shooting near downtown Knoxville')
        rows = visible(o)
        self.assertEqual(len(rows), 1)


# --------------------------------------------------------------------------- 3. global-no-local

class TestGlobalNoLocalGating(unittest.TestCase):
    def test_global_public_safety_with_no_local_impact_excluded(self):
        o = obs(category='public-safety', location='',
                title="HUD announces national housing showcase at the National Mall")
        rows = visible(o)
        self.assertEqual(rows, [])

    def test_global_public_safety_with_documented_impact_not_excluded_by_area_rule(self):
        o = obs(category='public-safety', location='',
                title="Dozens killed in building collapse overseas")
        rows = visible(o)
        self.assertEqual(len(rows), 1)


# --------------------------------------------------------------------------- 4. regional-no-location

class TestRegionalLocationGating(unittest.TestCase):
    def test_regional_with_no_location_is_downgraded_with_reason(self):
        o = obs(category='regional', source_type='community', location='',
                title='Something happened somewhere, details unclear')
        with connect(':memory:') as db:
            ingest(db, o)
            rows = queue(db, lane='inbox', now=NOW, include_suppressed=True)
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]['reason'])
            self.assertEqual(queue(db, lane='inbox', now=NOW), [])

    def test_regional_with_stated_location_is_not_downgraded_for_location(self):
        o = obs(category='regional', source_type='community', location='Knoxville, TN',
                title='Power outage affects thousands after storm damage')
        rows = visible(o)
        self.assertEqual(len(rows), 1)


# --------------------------------------------------------------------------- NOTAM

class TestNotamTriage(unittest.TestCase):
    def test_routine_aviation_notam_stays_out_of_daily_wire(self):
        o = obs(category='notam', source_type='official', location='KTYS',
                title='NOTAM KTYS: runway edge light out of service',
                body='RWY 05L/23R EDGE LGT U/S')
        rows = visible(o)
        self.assertEqual(rows, [])

    def test_public_impact_notam_is_flagged_for_review(self):
        o = obs(category='notam', source_type='official', location='KTYS',
                title='NOTAM KTYS: TFR for wildfire firefighting operations',
                body='TEMPORARY FLIGHT RESTRICTION FOR FIREFIGHTING AIRCRAFT')
        rows = visible(o)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['category'], 'notam')

    def test_notam_captures_effective_and_expiration_data(self):
        from shade_node.collectors import parse_notams
        from shade_node.model import SourceConfig
        source = SourceConfig('faa-notam', 'notam_search', 'FAA NOTAM', 'https://external-api.faa.gov/notamapi/v1/notams',
                               'official', 'faa-notam')
        payload = ('{"items": [{"icaoId": "KTYS", "notamNumber": "A0001/26", '
                   '"text": "TFR FOR WILDFIRE OPERATIONS", "issued": "2026-09-26T10:00:00Z", '
                   '"startDate": "2026-09-26T10:00:00Z", "endDate": "2026-09-27T10:00:00Z", '
                   '"minimumAlt": "SFC", "maximumAlt": "FL180"}]}').encode()
        result = parse_notams(source, payload)
        self.assertEqual(len(result), 1)
        o = result[0]
        self.assertEqual(o.category, 'notam')
        self.assertEqual(o.raw['_notam_effective_start'], '2026-09-26T10:00:00Z')
        self.assertEqual(o.raw['_notam_effective_end'], '2026-09-27T10:00:00Z')
        self.assertEqual(o.raw['_notam_min_alt'], 'SFC')
        self.assertEqual(o.raw['_notam_max_alt'], 'FL180')
        self.assertEqual(o.raw['properties']['expires'], '2026-09-27T10:00:00Z')

    def test_expired_notam_is_not_active(self):
        o = obs(category='notam', source_type='official', location='KTYS',
                title='NOTAM KTYS: TFR for wildfire firefighting operations',
                body='TFR', raw={'properties': {'onset': '2026-09-20T00:00:00Z', 'expires': '2026-09-21T00:00:00Z'}})
        rows = visible(o)
        self.assertEqual(rows, [])


# --------------------------------------------------------------------------- TX_CANDIDATE from anywhere

class TestTxCandidateFromAnywhere(unittest.TestCase):
    def _corroborated_claim(self, db):
        claim_id, _ = ingest(db, obs(category='infrastructure', source_type='official',
                                      title='Power outage affects downtown Knoxville substation'))
        return claim_id

    def test_mark_tx_candidate_directly_from_new(self):
        with connect(':memory:') as db:
            claim_id = self._corroborated_claim(db)
            status = db.execute('SELECT status FROM claims WHERE id=?', (claim_id,)).fetchone()['status']
            self.assertEqual(status, 'NEW')
            transition(db, claim_id, 'TX_CANDIDATE')
            self.assertEqual(db.execute('SELECT status FROM claims WHERE id=?', (claim_id,)).fetchone()['status'],
                              'TX_CANDIDATE')

    def test_unmark_tx_candidate_back_to_review(self):
        with connect(':memory:') as db:
            claim_id = self._corroborated_claim(db)
            transition(db, claim_id, 'TX_CANDIDATE')
            transition(db, claim_id, 'REVIEW')
            self.assertEqual(db.execute('SELECT status FROM claims WHERE id=?', (claim_id,)).fetchone()['status'],
                              'REVIEW')

    def test_still_requires_corroboration_without_override(self):
        with connect(':memory:') as db:
            claim_id, _ = ingest(db, obs(category='infrastructure', source_type='community',
                                          title='Someone said the power might be out'))
            with self.assertRaises(ValueError):
                transition(db, claim_id, 'TX_CANDIDATE')
            transition(db, claim_id, 'TX_CANDIDATE', operator_override=True)  # still works with explicit override


class TestBulkMarking(unittest.TestCase):
    def _service(self, tmp_path):
        config = tmp_path / 'config.toml'
        config.write_text('[operator]\ncallsign="TEST"\nregion_label="ETN/WNC"\n')
        return DesktopService(config)

    def test_bulk_mark_tx_candidate_via_desktop_service(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            ids = []
            with connect(service.settings.database) as db:
                for i in range(3):
                    claim_id, _ = ingest(db, obs(category='infrastructure', source_type='official',
                                                  external_id=f'e{i}', title=f'Substation outage {i}'))
                    ids.append(claim_id)
            count = service.mark_many(ids, 'TX_CANDIDATE')
            self.assertEqual(count, 3)
            with connect(service.settings.database) as db:
                statuses = [db.execute('SELECT status FROM claims WHERE id=?', (i,)).fetchone()['status'] for i in ids]
            self.assertEqual(statuses, ['TX_CANDIDATE'] * 3)

    def test_bulk_mark_blocks_whole_batch_if_one_lacks_corroboration(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            with connect(service.settings.database) as db:
                good, _ = ingest(db, obs(category='infrastructure', source_type='official',
                                          external_id='good', title='Substation outage confirmed'))
                bad, _ = ingest(db, obs(category='infrastructure', source_type='community',
                                         external_id='bad', title='Someone thinks power might be out'))
            with self.assertRaises(ValueError):
                service.mark_many([good, bad], 'TX_CANDIDATE')
            with connect(service.settings.database) as db:
                self.assertEqual(db.execute('SELECT status FROM claims WHERE id=?', (good,)).fetchone()['status'], 'NEW')

    def test_bulk_unmark_via_cli_bulk_mark(self):
        import tempfile, json as _json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            ids = []
            with connect(service.settings.database) as db:
                for i in range(2):
                    claim_id, _ = ingest(db, obs(category='infrastructure', source_type='official',
                                                  external_id=f'u{i}', title=f'Outage {i}'))
                    transition(db, claim_id, 'TX_CANDIDATE')
                    ids.append(claim_id)
            id_arg = ','.join(str(i) for i in ids)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(['--config', service.config, 'bulk-mark', id_arg, 'review'])
            self.assertEqual(code, 0)
            with connect(service.settings.database) as db:
                statuses = [db.execute('SELECT status FROM claims WHERE id=?', (i,)).fetchone()['status'] for i in ids]
            self.assertEqual(statuses, ['REVIEW', 'REVIEW'])


# --------------------------------------------------------------------------- operator feedback

class TestOperatorFeedback(unittest.TestCase):
    def test_feedback_is_persisted_with_all_required_fields(self):
        with connect(':memory:') as db:
            claim_id, _ = ingest(db, obs(category='public-safety', source_type='official',
                                          title='Two arrested after downtown disturbance'))
            record_feedback(db, claim_id, 'reject', reason_code='duplicate', note='already covered elsewhere')
            row = db.execute('SELECT * FROM operator_feedback WHERE claim_id=?', (claim_id,)).fetchone()
            self.assertEqual(row['action'], 'reject')
            self.assertEqual(row['reason_code'], 'duplicate')
            self.assertEqual(row['note'], 'already covered elsewhere')
            self.assertEqual(row['source_family'], 'agency')
            self.assertEqual(row['category'], 'public-safety')
            self.assertEqual(row['previous_status'], 'NEW')
            self.assertEqual(row['resulting_status'], 'REJECTED')
            self.assertTrue(row['created_at'])

    def test_suppress_source_applies_to_only_that_claim(self):
        with connect(':memory:') as db:
            a, _ = ingest(db, obs(category='chatter', source_type='community', external_id='a',
                                   title='Routine community chatter one'))
            b, _ = ingest(db, obs(category='chatter', source_type='community', external_id='b',
                                   title='Routine community chatter two'))
            record_feedback(db, a, 'suppress_source', reason_code='noisy_source')
            statuses = {cid: db.execute('SELECT status FROM claims WHERE id=?', (cid,)).fetchone()['status']
                        for cid in (a, b)}
            self.assertEqual(statuses[a], 'SUPPRESSED')
            self.assertEqual(statuses[b], 'NEW')  # not retroactively touched

    def test_feedback_is_reportable_by_category(self):
        with connect(':memory:') as db:
            for i in range(3):
                claim_id, _ = ingest(db, obs(category='top-news', source_type='community',
                                              external_id=f'r{i}', title=f'Report {i}'))
                record_feedback(db, claim_id, 'reject', reason_code='no_local_impact')
            report = feedback_report(db, group_by='category')
            self.assertEqual(report[0]['group_key'], 'top-news')
            self.assertEqual(report[0]['action'], 'reject')
            self.assertEqual(report[0]['n'], 3)

    def test_unknown_feedback_action_rejected(self):
        with connect(':memory:') as db:
            claim_id, _ = ingest(db, obs())
            with self.assertRaises(ValueError):
                record_feedback(db, claim_id, 'not_a_real_action')


# --------------------------------------------------------------------------- Daily Wire

class TestDailyWire(unittest.TestCase):
    def test_daily_wire_includes_all_tx_candidate_items(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.toml'
            config.write_text('[operator]\ncallsign="TEST"\nregion_label="ETN/WNC"\n')
            service = DesktopService(config)
            ids = []
            with connect(service.settings.database) as db:
                for i, cat in enumerate(['infrastructure', 'public-safety', 'notam']):
                    src_type = 'official'
                    title = {'infrastructure': 'Substation fire knocks out power to downtown',
                              'public-safety': 'Shooting reported near downtown Knoxville',
                              'notam': 'NOTAM KTYS: TFR for wildfire firefighting operations'}[cat]
                    claim_id, _ = ingest(db, obs(category=cat, source_type=src_type, external_id=f'w{i}', title=title))
                    transition(db, claim_id, 'TX_CANDIDATE')
                    ids.append(claim_id)
                # a REVIEW-only item must NOT appear in the Daily Wire
                other, _ = ingest(db, obs(category='infrastructure', source_type='official',
                                           external_id='review-only', title='Still under review'))
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(['--config', service.config, 'daily-wire'])
            self.assertEqual(code, 0)
            out = buf.getvalue()
            for claim_id in ids:
                self.assertIn(str(claim_id), out)
            self.assertNotIn('Still under review', out)   # match the title; the bare ID can collide with the AGE column


# --------------------------------------------------------------------------- performance regression

class TestPerformanceRegression(unittest.TestCase):
    def test_queue_stays_fast_under_a_large_claim_set(self):
        with connect(':memory:') as db:
            for i in range(1500):
                ingest(db, obs(category='chatter', source_type='community', external_id=f'perf{i}',
                                title=f'Routine community post number {i} about local events',
                                body='Nothing operationally significant here.',
                                published_at='2026-09-26T11:00:00Z', observed_at='2026-09-26T11:00:00Z'))
            t0 = time.perf_counter()
            queue(db, lane='inbox', now=NOW, include_suppressed=True)
            elapsed = time.perf_counter() - t0
            # Generous ceiling for CI variance - the point is catching an
            # accidental return to O(claims) individual queries, not chasing
            # a specific millisecond number in an automated test.
            self.assertLess(elapsed, 3.0, f'queue() took {elapsed:.2f}s for 1500 claims - investigate a regression')


if __name__ == '__main__':
    unittest.main(verbosity=2)
