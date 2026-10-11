import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from shade_node.chokepoints import load_chokepoints, tag_chokepoints
from shade_node.collectors import parse_safecast, parse_tdot, parse_usgs_waterservices
from shade_node.db import connect
from shade_node.engine import Settings, load_settings, run_once
from shade_node.model import SourceConfig
from shade_node.relevance import evaluate

ROOT = Path(__file__).resolve().parents[1]


class ApprovedIntegrationTests(unittest.TestCase):
    def test_safecast_server_scope_and_inside_outside(self):
        settings = load_settings(str(ROOT / 'config.toml'))
        source = next(s for s in settings.sources if s.id == 'safecast-radiation')
        self.assertIn('latitude=35.96&longitude=-83.92&distance=300000', source.url)
        rows = parse_safecast(source, json.dumps([
            {'id': 1, 'value': 0.1, 'latitude': 35.96, 'longitude': -83.92},
            {'id': 2, 'value': 0.2, 'latitude': 36.5, 'longitude': -82.5},
            {'id': 3, 'value': 0.3, 'latitude': 40.7, 'longitude': -74},
            {'id': 4, 'value': 0.4},
        ]).encode())
        self.assertEqual([r.external_id for r in rows], ['1', '2'])
        self.assertTrue(all(r.raw['_area'] == 'ETN/WNC' for r in rows))
        self.assertEqual(rows[0].raw['_geographic_scope']['radius_m'], 300000)

    def test_packs_enabled_and_chokepoints_loaded(self):
        settings = load_settings(str(ROOT / 'config.toml'))
        for source_id in ('cisa-kev', 'cisa-advisories', 'eff-updates'):
            self.assertTrue(next(s for s in settings.sources if s.id == source_id).enabled)
        self.assertIn('i40-pigeon-river-gorge', {e['id'] for e in settings.chokepoints})

    def test_tdot_matches_alias_county_mile_marker_and_rejects_elsewhere(self):
        source = SourceConfig('tdot', 'tdot_events', 'TDOT', 'https://example.test', 'official', 'tdot-smartway')
        def incident(identity, description, county):
            return {'attributes': {'ID': identity, 'EVENT_TYPE': 'Incident', 'EVENT_SUBTYPE': 'Lane blocked',
                    'DESCRIPTION': description, 'COUNTY_NAME': county, 'START_DATE': 1790000000000}}
        rows = parse_tdot(source, json.dumps({'features': [
            incident(1, 'Pigeon River Gorge lane blocked', 'Cocke'),
            incident(2, 'Interstate 40 crash', 'Cocke'),
            incident(3, 'I-40 near mile marker 448', 'Jefferson'),
            incident(4, 'I-40 near mile marker 390', 'Knox'),
            incident(5, 'I-75 crash', 'Cocke'),
        ]}).encode())
        entries = load_chokepoints(ROOT / 'chokepoints.toml')
        for row in rows: tag_chokepoints(row, entries)
        for row in rows[:3]:
            self.assertEqual(row.raw['_chokepoints'][0]['id'], 'i40-pigeon-river-gorge')
            self.assertEqual(row.raw['_chokepoint_boost'], 10)
        self.assertNotIn('_chokepoints', rows[3].raw)
        self.assertNotIn('_chokepoints', rows[4].raw)
        match = rows[2]
        now = datetime.now(timezone.utc)
        match.published_at = now.isoformat()
        row = {'title': match.title, 'body': match.body, 'location': match.location,
               'raw_json': match.raw_json(), 'category': match.category, 'severity': match.severity,
               'published_at': match.published_at, 'observed_at': match.observed_at,
               'source_family': match.source_family, 'source_type': match.source_type}
        boosted = evaluate(row, now=now)['breakdown']['operator_priority']
        row['raw_json'] = json.dumps({k: v for k, v in match.raw.items() if not k.startswith('_chokepoint')})
        unboosted = evaluate(row, now=now)['breakdown']['operator_priority']
        self.assertEqual(boosted-unboosted, 10)

    def test_usgs_requires_configured_site_and_ingestion_tags_it(self):
        source = SourceConfig('water', 'usgs_waterservices', 'Water', 'https://example.test',
                              'official', 'usgs-waterservices', area='ETN/WNC')
        now = datetime.now(timezone.utc).isoformat()
        payload = {'value': {'timeSeries': [{'sourceInfo': {'siteCode': [{'value': 'TEST-SITE'}],
                   'siteName': 'Test River'}, 'variable': {'variableCode': [{'value': '00060'}],
                   'unit': {'unitCode': 'ft3/s'}}, 'values': [{'value': [{'value': '20', 'dateTime': now}]}]}]}}
        row = parse_usgs_waterservices(source, json.dumps(payload).encode())[0]
        empty = load_chokepoints(ROOT / 'chokepoints.toml')
        tag_chokepoints(row, empty)
        self.assertNotIn('_chokepoints', row.raw)
        other = [{'id': 'other', 'name': 'Other River', 'watch_usgs_sites': ['OTHER-SITE']}]
        tag_chokepoints(row, other)
        self.assertNotIn('_chokepoints', row.raw)
        configured = [{'id': 'test-river', 'name': 'Test River', 'watch_usgs_sites': ['TEST-SITE']}]
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(str(Path(folder)/'test.db'), 7, 10, 1000, 'test', 'TEST', 'TEST',
                                'TEST', 500, 'standard', 35, 10, 'TEST', 'TEST', 500, [source],
                                chokepoints=configured)
            with patch('shade_node.engine.collect', return_value=[row]):
                result = run_once(settings)
            self.assertEqual(result['inserted'], 1)
            with connect(settings.database) as db:
                stored = dict(db.execute('SELECT * FROM observations').fetchone())
                raw = json.loads(stored['raw_json'])
            self.assertEqual(raw['_chokepoints'][0]['matched_by'], 'USGS site TEST-SITE')
            boosted = evaluate(stored)['breakdown']['operator_priority']
            stored['raw_json'] = json.dumps({k: v for k, v in raw.items() if not k.startswith('_chokepoint')})
            self.assertEqual(boosted - evaluate(stored)['breakdown']['operator_priority'], 10)


if __name__ == '__main__':
    unittest.main()
