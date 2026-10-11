"""JS8 MCForms decoding and the read-only NODE log collector (synthetic forms
and synthetic log lines only; never reads the live station)."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from shade_node.db import connect, ingest, queue
from shade_node.mcforms import area_tier, decode, decode_datecode, load_forms, maidenhead_center, parse_form, summary
from shade_node.model import SourceConfig
from shade_node.node_js8 import collect_node_js8

CHECKIN = """Basic Check-in|F!104

? Current status
@1 *Normal - Green
@2 Degraded - Yellow
@3 Significant problems - Red

? Situation Report
@0 *Nothing to report
@E Situation Report active

? Awareness/Alert Report
@0 *Nothing to report
@E Alert Report active

[ST] State (2-letter code):
[GR] Maidenhead Grid Square (REQUIRED):
"""

ASSESS = """Area Assessment|F!505

[GR] Maidenhead Grid Square (REQUIRED):
[ST] State (2-letter code - optional):
[CT] City or County (optional):

? 1) Water - Distribution
@X *Unknown or N/A
@3 > 99%
@2 50-99%
@1 < 50%
@0 Destroyed

? 2) Power - System
@X *Unknown or N/A
@3 Works
@2 Brownouts
@1 Blackouts
@0 Destroyed

? 3) Roads
@X *Unknown or N/A
@3 Open
@2 Some closures
@1 Many closures
@0 Impassable
"""

NOW = datetime(2026, 10, 9, 22, 0, tzinfo=timezone.utc)


class DecoderTests(unittest.TestCase):
    def setUp(self):
        self.forms = {f.form_id: f for f in (parse_form(CHECKIN), parse_form(ASSESS))}

    def test_form_file_parsing(self):
        form = self.forms['F!104']
        self.assertEqual(len(form.questions), 3)
        self.assertEqual(form.questions[0].default, '1')
        self.assertIn('GR', form.fields)

    def test_checkin_green_red_and_alert(self):
        green = decode('@SITREP MSG F!104 100 ST[OK] GR[EM15DJ] #JGSE', self.forms)
        self.assertEqual((green.status, green.fields['State (2-letter code)'], green.complete), ('GREEN', 'OK', True))
        self.assertEqual(decode('@SITREP  F!104 300 GR[EM75] #JGSE', self.forms).status, 'RED')
        alert = decode('@SITREP  F!104 10E ST[NE] GR[EN21AC] #JCKG', self.forms)
        self.assertIn('Alert Report active', summary(alert, 'WH6AKZ'))

    def test_assessment_degraded_and_note(self):
        d = decode('@SITREP  F!505 32X GR[FM05OW] WATER RESTRICTIONS #JGRD', self.forms)
        self.assertEqual(d.status, 'YELLOW')
        self.assertEqual([a['number'] for a in d.degraded], [2])
        self.assertEqual(d.free_text_clean, 'WATER RESTRICTIONS')
        self.assertIn('WATER RESTRICTIONS', summary(d, 'W2KI'))

    def test_corrupted_partial_is_not_green(self):
        d = decode('KN4AM: @SITREP  F!505 33<NACK>R[EL99HC] #JFA1', self.forms)
        self.assertEqual(d.status, 'PARTIAL')
        self.assertTrue(d.corrupted)
        self.assertTrue(any('answers for' in p for p in d.problems))

    def test_invalid_code_and_unknown_form(self):
        self.assertEqual(decode('@SITREP  F!104 900 #JGSE', self.forms).status, 'PARTIAL')
        self.assertEqual(decode('@SITREP  F!999 1 #JGSE', self.forms).status, 'UNKNOWN')
        self.assertIsNone(decode('@SITREP SNR?', self.forms))

    def test_node_logged_end_marker(self):
        # NODE's groups-*.jsonl writes JS8's end marker as '?'
        d = decode('@SITREP F!505 333 GR[EL99HC] #JEX1 ?', self.forms)
        self.assertEqual((d.datecode, d.free_text_clean, d.status), ('#JEX1', '', 'GREEN'))
        self.assertEqual(decode('@SITREP F!104 100 #JIVA \u25cf ', self.forms).datecode, '#JIVA')

    def test_reception_debris_is_not_an_operator_note(self):
        d = decode('@SITREP  F!104 100 GR<NACK>[FM05OW] #JHHK', self.forms)
        self.assertEqual(d.free_text_clean, '')
        self.assertNotIn('FM05OW', summary(d, 'W2KI'))
        d = decode('@SITREP  F!104 100 G<NACK> #JHHK', self.forms)
        self.assertEqual(d.free_text_clean, '')

    def test_area_tier_by_distance_from_home(self):
        self.assertEqual(area_tier('EM75wx', 'EM75wx'), 'ETN/WNC')
        self.assertEqual(area_tier('EM85', 'EM75wx'), 'ETN/WNC')     # Asheville area
        self.assertEqual(area_tier('EM74', 'EM75wx'), 'REGIONAL')
        self.assertEqual(area_tier('EL99HC', 'EM75wx'), 'NATIONAL')
        self.assertEqual(area_tier('KP20', 'EM75wx'), 'GLOBAL')
        self.assertEqual(area_tier('EM75', ''), '')

    def test_datecode_and_grid(self):
        self.assertEqual(decode_datecode('#JGRD'), '10/07 17:06 (sender clock)')
        self.assertEqual(decode_datecode('#JGX1'), '10/07 23:54 (sender clock)')
        lat, lon = maidenhead_center('EM75wx')
        self.assertAlmostEqual(lat, 35.98, places=1); self.assertAlmostEqual(lon, -84.12, places=1)


class NodeCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'forms').mkdir(); (root / 'logs').mkdir()
        (root / 'forms' / 'MCF104.txt').write_text(CHECKIN)
        (root / 'forms' / 'MCF505.txt').write_text(ASSESS)
        def rec(utc, frm, msg):
            return json.dumps({'utc': utc, 'src': 'js8_directed', 'mode': 'JS8', 'dial_mhz': 7.078, 'snr': -5,
                               'frm': frm, 'to': '@SITREP', 'msg': msg, 'grid': None, 'groups': ['@SITREP']})
        lines = [rec('2026-10-09T21:00:00Z', 'KN4AM', 'F!104 100 GR[EL99HC] #JIVA'),
                 rec('2026-10-09T21:30:00Z', 'KN4AM', 'F!104 100 GR[EL99HC] #JIVA'),  # same report heard again
                 rec('2026-10-09T21:35:00Z', 'W2KI', 'F!505 32X GR[FM05OW] WATER RESTRICTIONS #JGRD'),
                 rec('2026-10-09T21:40:00Z', 'K4XYZ', 'F!104 300 ST[TN] GR[EM75WX] #JIVU'),
                 json.dumps({'utc': '2026-10-09T21:41:00Z', 'mode': 'JS8', 'frm': 'K1ABC', 'to': '@HB', 'msg': 'HEARTBEAT EM73'}),
                 'not json F!104']
        (root / 'logs' / 'groups-2026-10-09.jsonl').write_text('\n'.join(lines) + '\n')
        self.source = SourceConfig('node-js8', 'node_js8_forms', 'NODE JS8 MCForms', str(root / 'logs'),
                                   'community', 'js8-mcforms', category='chatter', forms_dir=str(root / 'forms'), home_grid='EM75wx')

    def test_collect_decodes_dedupes_and_classifies(self):
        obs = collect_node_js8(self.source, now=NOW)
        self.assertEqual(len(obs), 3)
        by_sender = {o.external_id.split('|')[0]: o for o in obs}
        self.assertEqual(by_sender['KN4AM'].category, 'chatter')          # routine green check-in
        self.assertEqual(by_sender['W2KI'].category, 'infrastructure')     # degraded assessment
        self.assertEqual(by_sender['K4XYZ'].severity, 'severe')            # RED
        self.assertIn('Re-broadcast of an older report', by_sender['W2KI'].body)
        self.assertIn('Not independently verified', by_sender['K4XYZ'].body)
        self.assertEqual(by_sender['K4XYZ'].raw['_area_tier'], 'ETN/WNC')
        self.assertEqual(by_sender['K4XYZ'].raw['geometry']['coordinates'][1], maidenhead_center('EM75WX')[0])

    def test_observations_ingest_and_list(self):
        with connect(':memory:') as db:
            ids = {ingest(db, o)[0] for o in collect_node_js8(self.source, now=NOW)}
            again = {ingest(db, o)[0] for o in collect_node_js8(self.source, now=NOW)}
            self.assertEqual(ids, again)  # re-collecting adds nothing new
            rows = queue(db, None, 100, lane='all', include_suppressed=True, now=NOW)
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(r['confidence_label'].startswith('UNVERIFIED') for r in rows))
            local = [r for r in rows if r['area'] == 'ETN/WNC']
            self.assertEqual(len(local), 1)  # the RED check-in from EM75

    def test_reads_remote_nodes_and_dedupes_across_them(self):
        root = Path(self.temp.name)
        remote = root / 'logs' / 'nodes' / 'pi-cabin'; remote.mkdir(parents=True)
        rec = json.dumps({'utc': '2026-10-09T21:50:00Z', 'mode': 'JS8', 'snr': -12, 'frm': 'W4ABC', 'to': '@SITREP',
                          'msg': 'F!104 200 ST[NC] GR[EM85] #JIVZ'})
        same = json.dumps({'utc': '2026-10-09T21:51:00Z', 'mode': 'JS8', 'snr': -3, 'frm': 'K4XYZ', 'to': '@SITREP',
                           'msg': 'F!104 300 ST[TN] GR[EM75WX] #JIVU'})   # also heard locally
        (remote / 'groups-2026-10-09.jsonl').write_text(rec + '\n' + same + '\n')
        obs = collect_node_js8(self.source, now=NOW)
        by_sender = {o.external_id.split('|')[0]: o for o in obs}
        self.assertIn('heard by remote NODE pi-cabin', by_sender['W4ABC'].body)
        self.assertEqual(by_sender['W4ABC'].raw['node']['node_id'], 'pi-cabin')
        self.assertEqual(len(obs), 4)   # K4XYZ heard by both NODEs counts once

    def test_missing_folders_are_clear_errors(self):
        from dataclasses import replace
        from shade_node.collectors import CollectionError
        with self.assertRaises(CollectionError):
            collect_node_js8(replace(self.source, url=str(Path(self.temp.name) / 'nope')), now=NOW)
        with self.assertRaises(CollectionError):
            collect_node_js8(replace(self.source, forms_dir=str(Path(self.temp.name) / 'nope')), now=NOW)


if __name__ == '__main__':
    unittest.main()
