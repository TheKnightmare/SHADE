"""CommStat STATREP roll-up for the FIGHT wire (synthetic data, disposable DB)."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shade_node import commstat
from shade_node.db import connect, ingest
from shade_node.model import SourceConfig
from shade_node.statrep_rollup import rollup, split_remarks

NOW = datetime(2026, 10, 10, 19, 0, tzinfo=timezone.utc)
SRC = SourceConfig('commstat', 'commstat_db', 'CommStat', '', 'community', 'commstat', home_grid='EM75wx')


def add(db, line, hours_ago):
    d = commstat.decode_statrep(line)
    ingest(db, commstat.observation(SRC, d, heard=NOW - timedelta(hours=hours_ago), origin='test'))


class RollupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'shade.db')
        with connect(self.path) as db:
            add(db, 'KQ4ODY: @AMRRON ,EM75WX,1,A1,+,TN||,{&%}', 2)
            add(db, 'K8DDD: @AMRRON ,EM63PJ,2,B1,121111111111,AL-Blew thru fast||,{&%3}', 3)      # power yellow
            add(db, 'K8DDD: @AMRRON ,EM63PJ,2,B0,111111111111,AL,{&%3}', 20)                      # older; superseded
            add(db, 'KO4QLA: @AMRRON ,EM93,1,C1,212111111111,GA||,{&%3}', 5)                      # water yellow
            add(db, 'W3DOF: @AMRRON ,FM28FH,1,D1,+,MD||,{&%3}', 1)                                # far away
            add(db, 'N0OLD: @AMRRON ,EM75VX,1,E1,333111111111,TN-old news,{&%}', 40)              # outside 24 h

    def test_region_rollup_latest_per_station(self):
        r = rollup(self.path, hours=24, scope='region', now=NOW, area_label='ETN/WNC')
        self.assertEqual(r['stations'], 3)                       # MD is out of region, N0OLD out of window
        self.assertEqual(r['counts'], {'GREEN': 1, 'YELLOW': 2, 'RED': 0, 'UNKNOWN': 0})
        self.assertIn('POWER YELLOW (AL)', r['concerns'])
        self.assertIn('WATER YELLOW (GA)', r['concerns'])
        self.assertIn('AL: Blew thru fast', r['notes'])
        self.assertTrue(r['burst'].startswith('STATREPS ETN/WNC LAST 24H: 3 STATIONS - 1 GREEN 2 YELLOW 0 RED.'))
        self.assertIn('NOTES: AL: BLEW THRU FAST.', r['burst'])
        self.assertIn('SELF-REPORTED, UNVERIFIED.', r['burst'])
        self.assertIn('K8DDD (AL): Power Yellow - Blew thru fast', r['writeup'])

    def test_all_areas_and_empty(self):
        self.assertEqual(rollup(self.path, hours=24, scope='all', now=NOW)['stations'], 4)
        empty = rollup(self.path, hours=1, scope='region', now=NOW + timedelta(days=5))
        self.assertEqual(empty['stations'], 0)
        self.assertIn('NONE RECEIVED', empty['burst'])

    def test_split_remarks(self):
        self.assertEqual(split_remarks('AL-Blew thru fast'), ('AL', 'Blew thru fast'))
        self.assertEqual(split_remarks('TN'), ('TN', ''))
        self.assertEqual(split_remarks('No power since noon'), ('', 'No power since noon'))


class FieldCorroborationTests(RollupTests):
    def test_matches_topic_and_place(self):
        from shade_node.statrep_rollup import describe, field_matches, latest_reports
        reports = latest_reports(self.path, NOW - timedelta(hours=24))
        power_al = field_matches(reports, title='Power outages across north Alabama after storms', area='REGIONAL')
        self.assertEqual([m['sender'] for m in power_al], ['K8DDD'])
        self.assertIn('Field reports agree: 1 STATREP(s) - K8DDD (AL) Power Yellow', describe(power_al))
        self.assertEqual(field_matches(reports, title='Power outages in Ohio', area='NATIONAL'), [])   # wrong place
        self.assertEqual(field_matches(reports, title='Alabama football score', area='REGIONAL'), [])  # no topic
        water_ga = field_matches(reports, title='Boil water notice issued', location='Dalton, GA', area='REGIONAL')
        self.assertEqual([m['sender'] for m in water_ga], ['KO4QLA'])

    def test_fight_inbox_carries_field_reports(self):
        from shade_node.candidate_snapshot import list_candidates
        from shade_node.db import transition
        from shade_node.model import Observation
        with connect(self.path) as db:
            stamp = (NOW - timedelta(hours=1)).isoformat().replace('+00:00', 'Z')
            cid, _ = ingest(db, Observation('tva', 'TVA', 'official', 'tva', 'o1', 'Power outages across north Alabama',
                                            'Crews restoring service.', 'https://example.test/o1', category='grid',
                                            location='', published_at=stamp, observed_at=stamp))
            transition(db, cid, 'TX_CANDIDATE')
        inbox = list_candidates(self.path, callsign='KQ4ODY', network='GHOSTNET', now=NOW)
        row = next(c for c in inbox['candidates'] if c['claim_id'] == cid)
        self.assertIn('K8DDD (AL) Power Yellow', row['field_reports'])


if __name__ == '__main__':
    unittest.main()
