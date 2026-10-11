import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from shade_node.collectors import CollectionError, PartialCollectionError, collect, parse_rss
from shade_node.db import connect, ingest
from shade_node.model import Observation, SourceConfig
from shade_node.regional_discovery import search_queries
from shade_node.relevance import evaluate


class RegionalDiscoveryTests(unittest.TestCase):
    def source(self, **kwargs):
        return SourceConfig('regional-search-etn', 'google_news_search', 'ETN search',
                            'https://news.google.com/rss/search', 'media', 'google-news-unattributed',
                            **kwargs)

    def test_rotation_covers_each_location_and_concept_in_a_day(self):
        source = self.source(region_terms=['Knoxville', 'Asheville', 'Kentucky', 'Georgia'],
                             max_requests_per_poll=3)
        start = datetime(2026, 9, 24, 0, tzinfo=timezone.utc)
        from datetime import timedelta
        queries = [query for slot in range(8) for query in search_queries(source, start + timedelta(hours=3 * slot))]
        self.assertEqual(len(queries), 24)
        self.assertEqual(len(set(queries)), 24)
        self.assertTrue(all('when:7d' in query for query in queries))

    def test_search_attribution_preserves_origin(self):
        source = self.source(origin_families={'wvlt.tv': 'wvlt'})
        feed = b'<rss><channel><item><guid>x</guid><title>Flock update</title><source url="https://www.wvlt.tv">WVLT</source><pubDate>Thu, 24 Sep 2026 12:00:00 GMT</pubDate></item></channel></rss>'
        item = parse_rss(source, feed)[0]
        self.assertEqual(item.source_family, 'wvlt')
        self.assertEqual(item.source_name, 'WVLT')
        self.assertEqual(item.raw['_area'], '')

    def test_partial_search_keeps_success_and_query_errors(self):
        source = self.source(query_terms=['first', 'second'], max_requests_per_poll=2)
        feed = b'<rss><channel><item><guid>x</guid><title>Story</title></item></channel></rss>'
        meta = {}
        with patch('shade_node.collectors.fetch_bytes', side_effect=[feed, CollectionError('HTTP 429')]):
            with self.assertRaises(PartialCollectionError) as raised:
                collect(source, user_agent='test', timeout=1, max_bytes=10000, response_meta=meta)
        self.assertEqual(len(raised.exception.observations), 1)
        self.assertEqual([item['count'] for item in meta['query_attempts']], [1, 0])
        self.assertIn('HTTP 429', meta['query_attempts'][1]['error'])

    def test_search_filter_and_county_abbreviation(self):
        now = datetime(2026, 9, 24, 21, tzinfo=timezone.utc)
        def row(title, event_at=''):
            return dict(raw_json=json.dumps({'_discovery_source': 'regional-search-tn', '_event_at': event_at}),
                        category='regional', source_family='wvlt', source_type='media', title=title, body='',
                        location='', severity='unknown', published_at='2026-09-24T20:00:00Z', observed_at='2026-09-24T21:00:00Z')
        hit = evaluate(row('Anderson Co TN Flock camera contract dispute'), now=now)
        self.assertEqual(hit['area'], 'ETN/WNC')
        self.assertEqual(hit['reason'], '')
        self.assertGreater(hit['ghostnet_interest_score'], 0)
        self.assertIn('signal', evaluate(row('Blount County TN yard sale'), now=now)['reason'])
        self.assertIn('location', evaluate(row('Kansas City Flock camera dispute'), now=now)['reason'])
        self.assertNotEqual(evaluate(row('Georgia man killed in Anderson County crash'), now=now)['area'], 'ETN/WNC')
        self.assertEqual(evaluate(row('Anderson Co TN Flock camera contract dispute', '2026-09-10T12:00:00Z'), now=now)['reason'], 'Outside freshness window')

    def test_routine_tdot_congestion_does_not_outrank_incidents(self):
        now = datetime(2026, 9, 24, 21, tzinfo=timezone.utc)
        row = dict(raw_json=json.dumps({'EVENT_SUBTYPE': 'Congestion', 'VEHICLE_IMPACT': 'Eastbound no lanes blocked'}),
                   category='transportation', source_family='tdot-smartway', source_type='official',
                   title='Congestion - Eastbound no lanes blocked', body='', location='Knox, TN',
                   severity='moderate', published_at='2026-09-24T20:00:00Z', observed_at='2026-09-24T21:00:00Z')
        self.assertIn('Routine congestion', evaluate(row, now=now)['reason'])

    def test_small_locally_felt_quake_is_visible(self):
        now = datetime(2026, 9, 24, 21, tzinfo=timezone.utc)
        row = dict(raw_json=json.dumps({'properties': {'mag': 2.3, 'felt': 26}}),
                   category='earthquake', source_family='usgs', source_type='official',
                   title='M 2.3 - 7 km SE of Brevard, North Carolina', body='',
                   location='Brevard, North Carolina', severity='moderate',
                   published_at='2026-09-23T13:35:15Z', observed_at='2026-09-24T21:00:00Z')
        report = evaluate(row, now=now)
        self.assertEqual(report['area'], 'ETN/WNC')
        self.assertEqual(report['reason'], '')
        self.assertGreaterEqual(report['score'], 45)

    def test_interest_surfaces_existing_regional_health_reporting(self):
        now = datetime(2026, 9, 24, 21, tzinfo=timezone.utc)
        row = dict(raw_json='{}', category='public-health', source_family='nc-health-news',
                   source_type='media', title='NC leaders call Chemours deal historic',
                   body='North Carolina reached a PFAS contamination settlement.', location='',
                   severity='unknown', published_at='2026-09-21T08:30:00Z',
                   observed_at='2026-09-24T21:00:00Z')
        report = evaluate(row, now=now, confidence_score=35)
        self.assertGreaterEqual(report['score'], 45)
        self.assertGreater(report['ghostnet_interest_score'], 0)

    def test_search_and_rss_same_outlet_form_one_claim(self):
        source = self.source(origin_families={'wvlt.tv': 'wvlt'})
        feed = b'<rss><channel><item><guid>google-id</guid><title>Anderson Co Flock camera dispute - WVLT</title><source url="https://www.wvlt.tv">WVLT</source><pubDate>Thu, 24 Sep 2026 20:38:00 GMT</pubDate></item></channel></rss>'
        search = parse_rss(source, feed)[0]
        direct = Observation('wvlt-local', 'WVLT', 'media', 'wvlt', 'article-id',
                             'Anderson Co Flock camera dispute', '', 'https://www.wvlt.tv/story',
                             category='public-safety', published_at='2026-09-24T20:38:12Z')
        with connect(':memory:') as db:
            ingest(db, direct)
            ingest(db, search)
            self.assertEqual(db.execute('SELECT count(*) FROM claims').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT independent_families FROM claims').fetchone()[0], 1)

    def test_search_does_not_revive_suppressed_feed_claim(self):
        source = self.source(origin_families={'wvlt.tv': 'wvlt'})
        feed = b'<rss><channel><item><guid>google-id</guid><title>Anderson Co Flock camera dispute - WVLT</title><source url="https://www.wvlt.tv">WVLT</source><pubDate>Thu, 24 Sep 2026 20:38:00 GMT</pubDate></item></channel></rss>'
        search = parse_rss(source, feed)[0]
        direct = Observation('wvlt-local', 'WVLT', 'media', 'wvlt', 'article-id',
                             'Anderson Co Flock camera dispute', '', 'https://www.wvlt.tv/story',
                             category='public-safety', published_at='2026-09-24T20:38:12Z')
        with connect(':memory:') as db:
            claim_id, _ = ingest(db, direct)
            db.execute("UPDATE claims SET status='SUPPRESSED' WHERE id=?", (claim_id,))
            ingest(db, search)
            self.assertEqual(db.execute('SELECT count(*) FROM claims').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT status FROM claims').fetchone()[0], 'SUPPRESSED')
        with connect(':memory:') as db:
            ingest(db, search)
            ingest(db, direct)
            self.assertEqual(db.execute('SELECT count(*) FROM claims').fetchone()[0], 1)

    def test_duplicate_search_hits_same_publisher_form_one_claim(self):
        with connect(':memory:') as db:
            for external_id in ('a', 'b'):
                item = Observation('regional-search-etn', 'WVLT', 'media', 'wvlt', external_id,
                                   'Anderson Co Flock camera contract dispute', '', 'https://news.google.com/story/' + external_id,
                                   category='regional', published_at='2026-09-24T20:00:00Z',
                                   raw={'_discovery_source': 'regional-search-etn'})
                ingest(db, item)
            self.assertEqual(db.execute('SELECT count(*) FROM claims').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT independent_families FROM claims').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
