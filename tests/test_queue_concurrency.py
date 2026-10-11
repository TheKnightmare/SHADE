"""Verifies queue()'s bulk-prefetch path stays correct while a second
connection is actively writing to the SAME on-disk database - the actual
condition on this station (a scheduled collector writes every 15 minutes
while an operator's queue()/inbox call can run at any time), not just a
before/after comparison against a frozen, single-writer snapshot.

Uses a real temp file (SQLite's per-connection locking/snapshot behavior does
not apply to ':memory:', which is private per connection) and two real
connections: a writer thread continuously ingesting new claims, and the main
thread repeatedly calling queue() throughout.
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from shade_node.db import connect, ingest, queue
from shade_node.model import Observation


def make_observation(i):
    # Distinct enough per-i wording that SHADE's own title-similarity
    # correlation (_similar_claim - a real, separate feature, not a bug)
    # doesn't fold these into fewer claims than were actually ingested; this
    # test is about read/write concurrency, not correlation behavior.
    return Observation('fixture', 'Fixture', 'official', 'agency', f'concurrent-{i}',
                        f'Incident report number {i:04d} reference code QX{i:04d}Z',
                        f'Independent unrelated incident body text for report {i:04d}.',
                        f'https://example.test/{i}', category='infrastructure',
                        location='Knoxville, TN', severity='severe',
                        published_at='2026-09-26T12:00:00Z', observed_at='2026-09-26T12:00:00Z')


class TestQueueConcurrency(unittest.TestCase):
    def test_queue_stays_correct_and_never_raises_while_writer_is_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / 'concurrent.db')
            # Create the schema first, on its own connection, before any
            # concurrent access begins.
            with connect(db_path):
                pass

            stop = threading.Event()
            errors = []
            written = {'count': 0}

            def writer():
                # Runs to completion unconditionally - the point of this test
                # is to have a real writer active throughout the reader's
                # whole run, not to race an arbitrary deadline.
                try:
                    with connect(db_path) as db:
                        for i in range(300):
                            ingest(db, make_observation(i))
                            written['count'] = i + 1
                            time.sleep(0.001)
                except Exception as e:  # pragma: no cover - surfaced via errors list
                    errors.append(('writer', e))
                finally:
                    stop.set()

            def reader():
                try:
                    # Keep reading for as long as the writer is active, plus a
                    # little past it to also exercise the final, fully-written
                    # state at least once.
                    while not stop.is_set():
                        with connect(db_path) as db:
                            rows = queue(db, minimum_score=0, limit=500, lane='inbox', include_suppressed=True)
                            for row in rows:
                                # A torn read would show up as a missing/malformed
                                # required field on a row that was otherwise
                                # returned - every returned item must be complete.
                                for key in ('id', 'title', 'category', 'status', 'score', 'confidence_label'):
                                    if row.get(key) in (None, ''):
                                        errors.append(('reader', f'incomplete row {row.get("id")}: missing {key}'))
                        time.sleep(0.002)
                    with connect(db_path) as db:
                        queue(db, minimum_score=0, limit=500, lane='inbox', include_suppressed=True)
                except Exception as e:  # pragma: no cover
                    errors.append(('reader', e))

            writer_thread = threading.Thread(target=writer)
            reader_thread = threading.Thread(target=reader)
            writer_thread.start()
            reader_thread.start()
            writer_thread.join(timeout=30)
            reader_thread.join(timeout=30)

            self.assertGreater(written['count'], 0, 'writer thread never made progress - test is not exercising anything')
            self.assertEqual(errors, [], f'queue() was not safe under a concurrent writer: {errors[:5]}')

            with connect(db_path) as db:
                final_count = db.execute('SELECT COUNT(*) FROM claims').fetchone()[0]
            self.assertEqual(final_count, written['count'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
