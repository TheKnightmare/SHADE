import csv
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from shade_node.benchmark import audit_csv
from shade_node.db import connect, ingest, claim_detail, merge_claims, record_wire_transmission
from shade_node.gazetteer import resolve
from shade_node.relevance import geography, metadata, DEFAULT_POLICY
from shade_node.model import Observation

NOW = datetime.now(timezone.utc).isoformat()

def item(source, family, suffix):
    return Observation(source, family, 'media', family, suffix,
        'Knox County, Tennessee license plate cameras ordered removed',
        'County officials ordered cameras removed.',
        'https://' + family + '.example/story/' + suffix,
        category='privacy', published_at=NOW, observed_at=NOW)

class NextFiveTests(unittest.TestCase):
    def test_state_aware_gazetteer(self):
        self.assertEqual(resolve('Flock cameras in Knox Co. TN')['location'], 'Knox County, TN')
        self.assertEqual(resolve('Highway 258 near Richlands, North Carolina')['location'], 'Richlands, NC')
        self.assertEqual(resolve('Franklin County dispute')['confidence'], 'unknown')
        self.assertEqual(resolve('Anderson County crash')['confidence'], 'unknown')
        row = {'title':'Flock cameras in Knox Co. TN','body':'','location':'','source_type':'media'}
        self.assertEqual(geography(row, {'properties':{},'raw':{},'category':'regional'}, DEFAULT_POLICY)[0], 'ETN/WNC')
        self.assertEqual(resolve('Georgia Power and North Carolina reach deal')['confidence'], 'unknown')

    def test_group_and_wire_record_preserve_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            with connect(str(Path(directory)/'shade.db')) as db:
                a, _ = ingest(db, item('feed-a', 'a', '1'))
                alternate = item('feed-b', 'b', '2')
                alternate.title = 'Knox County, Tennessee license plate cameras will be removed'
                b, _ = ingest(db, alternate)
                self.assertNotEqual(a, b)
                self.assertEqual(merge_claims(db, a, [b]), 1)
                claim, rows = claim_detail(db, a)
                self.assertEqual(len(rows), 2)
                self.assertEqual(claim['independent_families'], 2)
                self.assertEqual(db.execute('SELECT status FROM claims WHERE id=?',(b,)).fetchone()[0], 'MERGED')
                self.assertTrue(record_wire_transmission(db, a, NOW, 'JS8', 'Actual message sent'))
                self.assertFalse(record_wire_transmission(db, a, NOW, 'JS8', 'Actual message sent'))
                self.assertEqual(db.execute('SELECT status FROM claims WHERE id=?',(a,)).fetchone()[0], 'SENT')

    def test_identical_syndicated_headline_is_one_claim_and_family(self):
        with tempfile.TemporaryDirectory() as directory:
            with connect(str(Path(directory)/'shade.db')) as db:
                a, _ = ingest(db, item('feed-a', 'a', '1'))
                syndicated = item('feed-b', 'b', '2')
                syndicated.published_at = (datetime.fromisoformat(NOW) + timedelta(hours=4)).isoformat()
                b, _ = ingest(db, syndicated)
                self.assertEqual(a, b)
                claim, rows = claim_detail(db, a)
                self.assertEqual(len(rows), 2)
                self.assertEqual(claim['independent_families'], 1)

    def test_benchmark_has_separate_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory)/'shade.db')
            csv_path = Path(directory)/'benchmark.csv'
            row = item('feed-a', 'a', '1')
            with csv_path.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.DictWriter(stream, fieldnames=['url','published_at','county','state'])
                writer.writeheader()
                writer.writerow({'url':row.url,'published_at':NOW,'county':'Knox','state':'TN'})
            with connect(database) as db:
                ingest(db, row)
                report = audit_csv(db, database, csv_path, {'minimum_score':0})
            story = report['stories'][0]
            self.assertEqual(story['discovery'], 'yes')
            self.assertTrue(story['area_correct'])
            self.assertIsNone(story['event_correct'])
            self.assertIn('inbox_rank', story)
