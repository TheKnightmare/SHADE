"""Operator-approved header logo (assets/shade-osint-arrows-concept.png,
replacing the plain 'SHADE' header text): loading, fallback, sizing, and
minimum-window layout."""
import unittest
from unittest.mock import patch

import tkinter as tk
from tkinter import ttk

from shade_node.gui import _header_logo, BG


class _HeaderTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()


class TestHeaderLogoLoading(_HeaderTestCase):
    def test_loads_as_an_image_label_not_the_old_text_title(self):
        widget = _header_logo(self.root)
        self.assertEqual(widget.winfo_class(), 'Label')
        self.assertNotEqual(str(widget.cget('image')), '')

    def test_scaled_down_from_the_original_1254px_source(self):
        widget = _header_logo(self.root)
        self.root.update_idletasks()
        # Must be legible at header size, not the raw multi-hundred-pixel
        # source (which would blow out the header row) and not degenerated
        # to nothing.
        self.assertGreater(widget.winfo_reqwidth(), 16)
        self.assertLess(widget.winfo_reqwidth(), 200)
        self.assertEqual(widget.winfo_reqwidth(), widget.winfo_reqheight())  # square source, proportional scale

    def test_falls_back_to_text_title_when_the_asset_cannot_be_loaded(self):
        with patch('shade_node.gui.tk.PhotoImage', side_effect=tk.TclError('missing asset')):
            widget = _header_logo(self.root)
        self.assertIsInstance(widget, ttk.Label)
        self.assertEqual(widget.cget('text'), 'SHADE')


class TestHeaderLayoutFit(_HeaderTestCase):
    def test_logo_tagline_and_mode_selector_fit_at_minimum_window_width(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('Muted.TLabel', background=BG)
        header = ttk.Frame(self.root)
        logo = _header_logo(header)
        logo.pack(side='left')
        tagline = ttk.Label(header, text='  SHADE — Source Harvesting, Attribution, Deduplication & Export',
                             style='Muted.TLabel')
        tagline.pack(side='left', padx=10)
        mode_box = ttk.Combobox(header, values=['STANDARD', 'EXERCISE', 'ACTUAL'], state='readonly', width=12)
        mode_box.pack(side='right')
        self.root.update_idletasks()
        needed = logo.winfo_reqwidth() + tagline.winfo_reqwidth() + mode_box.winfo_reqwidth()
        # The app's real minsize is 1000x700 with 20px padding on each side
        # (main = ttk.Frame(r, padding=20)) -> 960px of real header width.
        minimum_window_content_width = 1000 - 40
        self.assertLess(needed, minimum_window_content_width,
            f'header content ({needed}px) would clip or overlap the mode selector '
            f'at the minimum supported window width ({minimum_window_content_width}px)')


if __name__ == '__main__':
    unittest.main(verbosity=2)
