"""Desktop regression tests use disposable evidence, never a live station."""
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from shade_node.desktop import DesktopService
from shade_node.db import connect, ingest
from shade_node.model import Observation


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = Path(self.temp.name) / 'config.toml'
        config.write_text('[operator]\ncallsign="TEST"\n[operation]\nmode="actual"\n')
        self.service = DesktopService(config)
        with connect(self.service.settings.database) as db:
            self.claim_id, _ = ingest(db, Observation('fixture','Fixture','official','agency','one',
                'Major communications outage','Service outage confirmed','https://example.test/report',
                category='infrastructure', location='Knoxville TN', severity='severe'))

    def test_filter_and_search(self):
        self.assertEqual([r['id'] for r in self.service.listing(search='KNOXVILLE')], [self.claim_id])
        self.assertEqual(self.service.listing(category='cyber'), [])
        self.assertEqual(self.service.listing(search='unmatched'), [])

    def test_review_and_actual_gates(self):
        with self.assertRaises(ValueError): self.service.format(self.claim_id)
        with self.assertRaises(ValueError): self.service.mark(self.claim_id, 'SENT')
        self.service.mark(self.claim_id, 'REVIEW')
        self.assertIn('MAJOR COMMUNICATIONS OUTAGE', self.service.format(self.claim_id))
        self.assertNotIn('REVIEWED-BY:', self.service.format(self.claim_id))
        with self.assertRaises(ValueError): self.service.format(self.claim_id, 'actual')
        self.assertIn('ACTUAL', self.service.format(self.claim_id, 'actual', True))
        self.assertGreaterEqual(self.service.format(self.claim_id, 'exercise').count('EXERCISE'), 2)

    def expire(self):
        with connect(self.service.settings.database) as db:
            self.old_id, _ = ingest(db, Observation('fixture','Fixture','official','agency','old',
                'Tornado Warning','Take shelter','https://example.test/old',category='weather',
                location='Knox TN',severity='extreme',published_at='2000-01-01T00:00:00Z',
                raw={'properties':{'event':'Tornado Warning','expires':'2000-01-01T01:00:00Z'}}))

    def test_expired_report_cannot_advance(self):
        self.expire()
        with self.assertRaises(ValueError): self.service.mark(self.old_id, 'REVIEW')

    def test_housekeeping_backup_and_preview(self):
        self.expire()
        def stored_state():
            with connect(self.service.settings.database) as db:
                return db.execute('SELECT status FROM claims WHERE id=?', (self.old_id,)).fetchone()[0]
        backup, preview = self.service.cleanup()
        self.assertIsNone(backup)
        self.assertEqual(stored_state(), 'NEW')
        with self.assertRaises(ValueError): self.service.cleanup(apply=True, expected=[])
        self.assertEqual(stored_state(), 'NEW')
        backup, changed = self.service.cleanup(apply=True, expected=preview)
        self.assertTrue(Path(backup).is_file())
        self.assertEqual(changed, preview)
        self.assertEqual(stored_state(), 'EXPIRED')
        self.assertEqual(len(self.service.detail(self.old_id)[1]), 1)

    def test_collection_uses_selected_mode(self):
        with patch('shade_node.desktop.run_once', return_value={'inserted':0}) as collect:
            self.assertEqual(self.service.collect('EXERCISE'), {'inserted':0})
            collect.assert_called_once_with(self.service.settings, 'exercise')

    def test_window_refresh_selection_and_preview(self):
        import tkinter as tk
        from shade_node.gui import ShadeWindow
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest('Tcl/Tk runtime unavailable: '+str(exc))
        root.withdraw()
        app = ShadeWindow(root, self.service)
        self.addCleanup(lambda: (app.pool.shutdown(wait=True), root.destroy()))
        errors = []
        root.report_callback_exception = lambda *args: errors.append(args)
        def settle():
            deadline = time.monotonic()+5
            while time.monotonic() < deadline:
                root.update()
                if not app.busy:
                    root.update()
                    if not app.busy: break
                time.sleep(.01)
            self.assertFalse(app.busy)
            self.assertEqual(errors, [])
        settle()
        self.assertEqual(app.mode.get(), 'STANDARD')
        self.assertIn(str(self.claim_id), app.tree.get_children())
        app.tree.selection_set(str(self.claim_id))
        settle()
        self.assertEqual(app.claim['id'], self.claim_id)
        self.assertEqual(str(app.format_button['state']), 'disabled')
        app.mark('REVIEW')
        settle()
        self.assertEqual(str(app.format_button['state']), 'normal')
        app.format_message()
        settle()
        dialogs = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
        self.assertEqual(len(dialogs), 1)
        dialogs[0].destroy()
        app.search.set('unmatched')
        app.refresh()
        settle()
        self.assertEqual(app.tree.get_children(), ())
        self.assertIsNone(app.claim)


    def test_window_age_sort_and_bulk_selection(self):
        import tkinter as tk
        from shade_node.gui import ShadeWindow
        with connect(self.service.settings.database) as db:
            # Relative to "now" rather than a fixed calendar date, so this
            # fixture can't drift stale (outside the freshness window,
            # dropped from the default Inbox listing) as real time passes.
            recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
            second_id, _ = ingest(db, Observation('second', 'Second', 'community', 'second', 'two',
                'County bridge closure', 'Bridge closed', 'https://example.test/bridge',
                category='transportation', location='Buncombe NC',
                published_at=recent))
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest('Tcl/Tk runtime unavailable: '+str(exc))
        root.withdraw()
        app = ShadeWindow(root, self.service)
        self.addCleanup(lambda: (app.pool.shutdown(wait=True), root.destroy()))
        deadline = time.monotonic()+5
        while app.busy and time.monotonic()<deadline:
            root.update()
            time.sleep(.01)
        root.update()
        app.sort_by('age')
        self.assertEqual(app.tree.get_children()[0], str(self.claim_id))
        app.tree.selection_set((str(self.claim_id), str(second_id)))
        root.update()
        self.assertEqual(str(app.bulk_button['state']), 'normal')
        self.assertEqual(str(app.format_button['state']), 'disabled')
        self.assertIsNone(app.claim)

if __name__ == '__main__':
    unittest.main()


class BackgroundCollectTests(unittest.TestCase):
    setUp = DesktopTests.setUp
    """Speed pass 2026-10-09: Collect now must not lock the operator out."""
    def test_collect_runs_in_background_and_review_continues(self):
        import threading
        import tkinter as tk
        from shade_node.gui import ShadeWindow
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest('Tcl/Tk runtime unavailable: '+str(exc))
        root.withdraw()
        release = threading.Event()
        def slow_collect(mode):
            release.wait(5)
            return {'sources_ok': 1, 'sources_partial': 0, 'sources_failed': 0, 'inserted': 0,
                    'sources_skipped_cooldown': 0, 'errors': []}
        self.service.collect = slow_collect
        app = ShadeWindow(root, self.service)
        self.addCleanup(lambda: (release.set(), app.pool.shutdown(wait=True), app.collect_pool.shutdown(wait=True), root.destroy()))
        def pump(until, seconds=5):
            deadline = time.monotonic()+seconds
            while time.monotonic() < deadline and not until():
                root.update(); time.sleep(.01)
            self.assertTrue(until())
        pump(lambda: not app.busy)
        with patch('shade_node.gui.messagebox.showinfo') as shown:
            app.collect()
            self.assertTrue(app.collecting)
            self.assertFalse(app.busy)
            self.assertEqual(str(app.collect_button['state']), 'disabled')
            app.refresh()
            pump(lambda: not app.busy)
            self.assertIn(str(self.claim_id), app.tree.get_children())
            app.tree.selection_set(str(self.claim_id))
            pump(lambda: app.claim is not None and not app.busy)
            release.set()
            pump(lambda: not app.collecting)
            pump(lambda: shown.called)
            pump(lambda: not app.busy)
        self.assertEqual(str(app.collect_button['state']), 'normal')
