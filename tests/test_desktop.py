"""Desktop regression tests use disposable evidence, never a live station."""
import tempfile
import time
import unittest
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
        self.assertIn('TEST', self.service.format(self.claim_id))
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


if __name__ == '__main__':
    unittest.main()
