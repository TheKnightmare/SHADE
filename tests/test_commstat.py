"""CommStat STATREP/alert decoding, the read-only CommStat database reader, and
on-air CommStat lines in NODE logs. Synthetic data only; never the live station."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from shade_node import commstat
from shade_node.db import connect, ingest
from shade_node.model import SourceConfig
from shade_node.node_js8 import collect_node_js8

NOW = datetime(2026, 10, 10, 18, 0, tzinfo=timezone.utc)


class DecodeTests(unittest.TestCase):
    def test_statrep_categories_overall_and_markers(self):
        d = commstat.decode_statrep('N0DDK: @MAGNET ,EM83CV,3,T31,321311111331,GA,{&%}')
        self.assertEqual(d['statuses']['Medical'], 'RED')
        self.assertEqual(d['statuses']['Power'], 'YELLOW')
        self.assertEqual(d['overall'], 'RED')
        self.assertEqual((d['scope'], d['via'], d['id']), ('county', 'radio', 'T31'))
        green = commstat.decode_statrep('K1Y: @SITREP ,EM75WX,2,Q1,+,,{&%3}')
        self.assertEqual(set(green['statuses'].values()), {'GREEN'})
        self.assertEqual(green['via'], 'internet')
        self.assertIn('ALL GREEN', commstat.headline(green))
        self.assertEqual(commstat.decode_statrep('KD0NBV: @SITREP  ,EM18VK,1,M10,111111111121,REMARK,{&%} ?')['concerns'],
                         ['Civil YELLOW'])

    def test_alert_new_and_legacy_layouts(self):
        new = commstat.decode_alert('W2X: @AMRRON ,A7,3,Bridge out,I-40 closed||detour,{%%3}')
        self.assertEqual((new['color'], new['id'], new['via']), ('RED', 'A7', 'internet'))
        self.assertEqual(new['message'], 'I-40 closed\ndetour')
        old = commstat.decode_alert('W1ABC: @ALL LRT ,1,Test Alert,This is a test,{%%}')
        self.assertEqual((old['color'], old['title'], old['id']), ('YELLOW', 'Test Alert', ''))
        self.assertIsNone(commstat.decode_alert('N0DDK: @MAGNET ,EM83CV,3,T31,321311111331,GA,{&%}'))
        self.assertIsNone(commstat.decode_statrep('plain chatter with no markers'))


class DatabaseTests(unittest.TestCase):
    def test_reads_statreps_and_alerts_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'traffic.db3'
            with sqlite3.connect(db) as c:
                c.execute('CREATE TABLE statrep (id INTEGER PRIMARY KEY, global_id INTEGER, pinned INTEGER, datetime TEXT, date TEXT, '
                          'freq DOUBLE, db INTEGER, source INTEGER, sr_id TEXT, from_callsign TEXT, target TEXT, grid TEXT, scope TEXT, '
                          'map TEXT, power TEXT, water TEXT, med TEXT, telecom TEXT, travel TEXT, internet TEXT, fuel TEXT, food TEXT, '
                          'crime TEXT, civil TEXT, political TEXT, comments TEXT, memo TEXT)')
                c.execute('CREATE TABLE alerts (id INTEGER PRIMARY KEY, datetime TEXT, date TEXT, freq DOUBLE, db INTEGER, source INTEGER, '
                          'alert_id TEXT, from_callsign TEXT, target TEXT, color INTEGER, title TEXT, message TEXT)')
                c.execute("INSERT INTO statrep(datetime,date,source,sr_id,from_callsign,target,grid,scope,map,power,water,med,telecom,"
                          "travel,internet,fuel,food,crime,civil,political,comments) VALUES('2026-10-10 17:00:00','2026-10-10',2,'Q9',"
                          "'W4AAA','@AMRRON','EM75VX','Community','2','2','1','1','1','1','1','1','1','1','1','1','Power out in Halls')")
                c.execute("INSERT INTO statrep(datetime,date,source,sr_id,from_callsign,target,grid,scope,map,power) "
                          "VALUES('2026-09-01 10:00:00','2026-09-01',1,'OLD','W4OLD','@X','EM75','1','1','1')")
                c.execute("INSERT INTO alerts(datetime,date,source,alert_id,from_callsign,target,color,title,message) "
                          "VALUES('2026-10-10 17:30:00','2026-10-10',1,'B2','W4BBB','@ALL',3,'Water boil notice','Knox Co')")
            before = db.read_bytes()
            src = SourceConfig('commstat', 'commstat_db', 'CommStat', str(d), 'community', 'commstat',
                               category='infrastructure', home_grid='EM75wx')
            obs = commstat.collect_commstat_db(src, now=NOW)
            self.assertEqual(db.read_bytes(), before)                 # never written
            titles = sorted(o.title for o in obs)
            self.assertEqual(len(obs), 2)                              # 40-day-old report skipped
            self.assertTrue(any('STATREP W4AAA EM75VX (community): YELLOW - Power YELLOW' in t for t in titles))
            self.assertTrue(any('RED ALERT from W4BBB: Water boil notice' in t for t in titles))
            statrep = next(o for o in obs if o.title.startswith('STATREP'))
            self.assertIn('via internet', statrep.body)
            self.assertIn('_area_tier', statrep.raw)
            with connect(str(Path(d) / 'shade.db')) as shade:
                first = ingest(shade, statrep)
                self.assertEqual(ingest(shade, statrep)[0], first[0])  # same report seen again, one claim

    def test_missing_database_is_a_clear_error(self):
        from shade_node.collectors import CollectionError
        with tempfile.TemporaryDirectory() as d:
            src = SourceConfig('commstat', 'commstat_db', 'CommStat', str(d), 'community', 'commstat')
            with self.assertRaises(CollectionError):
                commstat.collect_commstat_db(src, now=NOW)


class NodeLogTests(unittest.TestCase):
    def test_on_air_statrep_in_node_log_without_forms(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / 'logs'; logs.mkdir()
            line = {'utc': '2026-10-10T17:55:00Z', 'mode': 'JS8', 'frm': 'KD0NBV', 'to': '@SITREP', 'dial_mhz': 7.078,
                    'snr': -6, 'msg': ',EM18VK,1,M10,111111111121,MICHELIN CUTTING 100 JOBS,{&%} ?'}
            (logs / 'groups-2026-10-10.jsonl').write_text(json.dumps(line) + '\n' + json.dumps(line) + '\n')
            src = SourceConfig('node-js8', 'node_js8_forms', 'NODE JS8', str(logs), 'community', 'js8-mcforms',
                               forms_dir='', home_grid='EM75wx')
            obs = collect_node_js8(src, now=NOW)
            self.assertEqual(len(obs), 1)
            self.assertIn('STATREP KD0NBV EM18VK', obs[0].title)
            self.assertIn('heard on JS8 7.078 MHz', obs[0].body)


class BrevityTests(unittest.TestCase):
    LIST = {"emergency_type": {"A": {"name": "Unknown"}, "*** Weather ***": {"T": {"name": "Hurricane / typhoon"}}},
            "status_codes": {"*** While it is happening ***": {"items": {}, "description": ""}, "K": {"name": "Weakening"}},
            "severity": {"I": {"name": "Extreme"}}, "shared_impacts": {"D": {"name": "Inland flooding"}},
            "official_response": {"M": {"name": "Shelter-in-place"}}, "information_codes": {"S": {"name": "Independent media"}},
            "station_response": {"A": {"name": "Unknown"}}}

    def test_decodes_code_from_commstat_list_files(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '1-Natural Events_V2.0.json').write_text(json.dumps(self.LIST))
            lists = commstat.brevity_lists(Path(d))
            self.assertEqual(lists['1']['title'], 'Natural Events')
            got = commstat.decode_brevity_in('AL: 1TKIDMSA ISAIAS', lists)
            self.assertEqual(got[0]['text'], '1TKIDMSA = Natural Events: Hurricane / typhoon; Weakening; Extreme; '
                                             'Inland flooding; Shelter-in-place (source: Independent media)')
            self.assertEqual(commstat.decode_brevity_in('MICHELIN 2026 JOBS', lists), [])
            src = SourceConfig('commstat', 'commstat_db', 'CommStat', d, 'community', 'commstat', home_grid='EM75wx')
            obs = commstat.observation(src, commstat.decode_statrep('K4X: @AMRRON ,EM63PJ,2,B1,311111111131,AL 1TKIDMSA,{&%}'),
                                       heard=NOW, origin='test')
            self.assertIn('[Hurricane / typhoon, Weakening]', obs.title)
            self.assertIn('Brevity code 1TKIDMSA = Natural Events', obs.body)
            self.assertEqual(obs.raw['_commstat']['brevity'][0]['event'], 'Hurricane / typhoon')


if __name__ == '__main__':
    unittest.main()
