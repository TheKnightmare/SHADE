"""SHADE desktop console. Tk stays on the UI thread; work runs in one worker."""
from concurrent.futures import ThreadPoolExecutor
from importlib import resources
from pathlib import Path
from datetime import datetime
import argparse
import queue as messages
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import webbrowser

from . import __version__
from .desktop import DesktopService
from .db import ALLOWED_TRANSITIONS
from .formatters import evidence_summary, workflow_status
from .relevance import age, remaining
from .inbox_sort import sorted_inbox
from .model import token_set

BG = '#101923'
PANEL = '#192735'
TEXT = '#e7edf4'
MUTED = '#9bafc2'
ACCENT = '#59d8ce'
MIN_SPLASH_MS = 10000
FORCED_REFRESH_SECONDS = 600


def _set_window_icon(window):
    """Apply both Windows ICO and cross-platform PNG window icons."""
    try:
        window.iconbitmap(str(resources.files('shade_node').joinpath('assets/shade.ico')))
    except tk.TclError:
        pass
    try:
        icon = tk.PhotoImage(file=str(resources.files('shade_node').joinpath('assets/shade-osint-arrows-concept.png')))
        window.iconphoto(True, icon)
        window._shade_icon = icon
    except tk.TclError:
        pass


def _header_logo(parent):
    """Approved SHADE logo/icon art (assets/shade-osint-arrows-concept.png,
    operator-approved in place of the plain 'SHADE' header text), scaled down
    for the header via Tk's own PhotoImage.subsample - no new dependency, and
    the source PNG on disk is never read-modified, only loaded and scaled in
    memory. Falls back to the original text title if the asset can't be
    loaded (e.g. a stripped install missing the packaged asset)."""
    try:
        full = tk.PhotoImage(file=str(resources.files('shade_node').joinpath('assets/shade-osint-arrows-concept.png')))
        logo = full.subsample(20, 20)
        label = tk.Label(parent, image=logo, bg=BG, borderwidth=0, highlightthickness=0)
        label._shade_logo = logo
        return label
    except tk.TclError:
        return ttk.Label(parent, text='SHADE', style='Title.TLabel')


def _show_splash(root):
    splash = tk.Toplevel(root)
    splash.overrideredirect(True)
    splash.configure(bg='#080b0e', highlightbackground=ACCENT, highlightthickness=1)
    _set_window_icon(splash)
    body = tk.Frame(splash, bg='#080b0e', padx=28, pady=24)
    body.pack(fill='both', expand=True)
    try:
        artwork = tk.PhotoImage(file=str(resources.files('shade_node').joinpath('assets/shade-splash.png'))).subsample(2, 2)
        tk.Label(body, image=artwork, bg='#080b0e').pack()
        splash._shade_artwork = artwork
    except tk.TclError:
        pass
    tk.Label(body, text='SHADE', bg='#080b0e', fg=ACCENT,
             font=('Segoe UI Semibold', 28)).pack(pady=(12, 0))
    tk.Label(body, text='SHADE — Source Harvesting, Attribution,\nDeduplication & Export', bg='#080b0e', fg=MUTED,
             font=('Segoe UI', 10), justify='center').pack(pady=(2, 10))
    tk.Label(body, text='Opening operator console…', bg='#080b0e', fg=TEXT,
             font=('Segoe UI', 9)).pack()
    splash.update_idletasks()
    width, height = splash.winfo_reqwidth(), splash.winfo_reqheight()
    x = (splash.winfo_screenwidth() - width) // 2
    y = (splash.winfo_screenheight() - height) // 2
    splash.geometry(f'{width}x{height}+{x}+{y}')
    splash.lift()
    splash.update()
    return splash


class ShadeWindow:
    def __init__(self, root, service):
        self.root, self.service = root, service
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='shade-desktop')
        # Collection and the idle change-probe run on their own workers so a
        # long feed poll never locks the operator out of reading/reviewing.
        self.collect_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='shade-collect')
        self.probe_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='shade-probe')
        self.results = messages.Queue()
        self.busy = False
        self.collecting = False
        self.probing = False
        self.data_signature = None
        self.last_full_refresh = 0.0
        self.claim = None
        self.observations = []
        self.rows = []
        self.sort_key = None
        self.sort_descending = False
        self.controls = []
        self.mode = tk.StringVar(value='STANDARD')  # Never inherit ACTUAL on launch.
        self.last_mode = 'STANDARD'
        self.lane = tk.StringVar(value='Inbox')
        self.category = tk.StringVar(value='All categories')
        self.area = tk.StringVar(value='All areas')
        self.state = tk.StringVar(value='Active states')
        self.search = tk.StringVar()
        self.minimum = tk.StringVar()
        self.max_age = tk.StringVar()
        self.broad = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='Loading local evidence…')
        self.count = tk.StringVar(value='')
        # Daily Wire tab state - a SHADE-side review/assembly view over the
        # existing TX_CANDIDATE queue. wire_claim/wire_rows are independent
        # of the Inbox tab's claim/rows so switching tabs never disturbs
        # either view's selection.
        self.wire_rows = []
        self.wire_all_rows = []
        self.wire_claim = None
        self.wire_area = tk.StringVar(value='All areas')
        self.wire_category = tk.StringVar(value='All categories')
        self.wire_warnings_only = tk.BooleanVar(value=False)
        self.wire_show_hidden = tk.BooleanVar(value=False)
        self.wire_search = tk.StringVar()
        self.wire_status = tk.StringVar(value='')
        self.purge_hours = tk.StringVar(value='72')  # manual purge only; operator picks 1-72 h
        self._build()
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.bind('<F5>', lambda event: self.refresh())
        root.after(80, self._drain)
        self.refresh()
        root.after(60000, self._tick)

    def _button(self, parent, text, command, **kwargs):
        widget = ttk.Button(parent, text=text, command=command, **kwargs)
        self.controls.append(widget)
        return widget

    def _combo(self, parent, variable, values, width=18):
        widget = ttk.Combobox(parent, textvariable=variable, values=values, state='readonly', width=width)
        self.controls.append(widget)
        return widget

    def _build(self):
        r = self.root
        r.title(f'SHADE {__version__} • Operator console')
        r.geometry('1320x850')
        r.minsize(1000, 700)
        r.configure(bg=BG)
        _set_window_icon(r)
        style = ttk.Style(r)
        style.theme_use('clam')
        style.configure('.', background=BG, foreground=TEXT, font=('Segoe UI', 10))
        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG)
        style.configure('Muted.TLabel', foreground=MUTED)
        style.configure('Title.TLabel', font=('Segoe UI Semibold', 27), foreground=ACCENT)
        style.configure('TButton', background=PANEL, foreground=TEXT, padding=(12, 8), borderwidth=0)
        style.configure('Relay.TButton', background='#65452c', foreground='#fff0d8', padding=(12, 8), borderwidth=1)
        style.map('TButton', background=[('active', '#285268')], foreground=[('disabled', '#637789')])
        style.configure('TCombobox', fieldbackground=PANEL, background=PANEL, foreground=TEXT, padding=4)
        style.map('TCombobox', fieldbackground=[('readonly', PANEL)], foreground=[('readonly', TEXT)])
        style.configure('TEntry', fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT, padding=5)
        style.configure('Treeview', background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=32, borderwidth=0)
        style.configure('Treeview.Heading', background='#24384a', foreground=TEXT, font=('Segoe UI Semibold', 10), padding=8)
        style.map('Treeview', background=[('selected', '#205d6a')], foreground=[('selected', '#ffffff')])
        style.configure('TPanedwindow', background=BG)
        r.option_add('*TCombobox*Listbox.background', PANEL)
        r.option_add('*TCombobox*Listbox.foreground', TEXT)
        style.configure('TNotebook', background=BG, borderwidth=0)
        style.configure('TNotebook.Tab', background='#182631', foreground=MUTED, padding=(16, 8))
        style.map('TNotebook.Tab', background=[('selected', PANEL)], foreground=[('selected', TEXT)])
        notebook = ttk.Notebook(r)
        notebook.pack(fill='both', expand=True)
        main = ttk.Frame(notebook, padding=20)
        notebook.add(main, text='Inbox')
        header = ttk.Frame(main)
        header.pack(fill='x')
        _header_logo(header).pack(side='left')
        ttk.Label(header, text='  SHADE — Source Harvesting, Attribution, Deduplication & Export',
                  style='Muted.TLabel').pack(side='left', padx=10)
        self.mode_box = self._combo(header, self.mode, ['STANDARD', 'EXERCISE', 'ACTUAL'], 12)
        self.mode_box.pack(side='right')
        self.mode_box.bind('<<ComboboxSelected>>', self.change_mode)
        self.banner = ttk.Label(main, text='Operator review console • All transmission decisions remain human.', style='Muted.TLabel')
        self.banner.pack(anchor='w', pady=(4, 16))
        toolbar = ttk.Frame(main)
        toolbar.pack(fill='x', pady=(0, 12))
        self.collect_button = self._button(toolbar, 'Collect now', self.collect)
        self.collect_button.pack(side='left', padx=(0, 8))
        self._button(toolbar, 'Refresh view', self.refresh).pack(side='left', padx=(0, 8))
        self._button(toolbar, 'Sources & health', self.sources).pack(side='left', padx=(0, 8))
        self._button(toolbar, 'Housekeeping…', self.cleanup).pack(side='left', padx=(0, 8))
        self._button(toolbar, 'Open station…', self.open_station).pack(side='right')
        self._button(toolbar, 'Help', self.help).pack(side='right', padx=8)
        filters = ttk.Frame(main)
        filters.pack(fill='x', pady=(0, 8))
        for variable, values, width in [
            (self.lane, ['Inbox', 'Top news', 'NOW', 'All evidence'], 14),
            (self.category, ['All categories','top-news','cyber','infrastructure','communications','grid','fuel','commodity','transportation','public-safety','public-health','civil-liberties','politics','space-weather','earthquake','weather','fire-weather','disaster','chatter'], 18),
            (self.area, ['All areas','ETN/WNC','REGIONAL','NATIONAL','GLOBAL','DISTANT'], 13),
            (self.state, ['Active states']+list(ALLOWED_TRANSITIONS), 15),
        ]:
            box = self._combo(filters, variable, values, width)
            box.pack(side='left', padx=(0, 8))
            box.bind('<<ComboboxSelected>>', lambda event: self.refresh())
        broad = ttk.Checkbutton(filters, text='Show suppressed and routine', variable=self.broad, command=self.refresh)
        broad.pack(side='left')
        self.controls.append(broad)
        searchbar = ttk.Frame(main)
        searchbar.pack(fill='x', pady=(0, 10))
        ttk.Label(searchbar, text='Search').pack(side='left')
        entry = ttk.Entry(searchbar, textvariable=self.search, width=32)
        entry.pack(side='left', padx=(8, 16))
        entry.bind('<Return>', lambda event: self.refresh())
        self.controls.append(entry)
        for label, variable in [('Minimum score', self.minimum), ('Age ≤ hours', self.max_age)]:
            ttk.Label(searchbar, text=label).pack(side='left')
            entry = ttk.Entry(searchbar, textvariable=variable, width=6)
            entry.pack(side='left', padx=(8, 16))
            entry.bind('<Return>', lambda event: self.refresh())
            self.controls.append(entry)
        self._button(searchbar, 'Apply filters', self.refresh).pack(side='left')
        ttk.Label(searchbar, textvariable=self.count, style='Muted.TLabel').pack(side='right')
        splitter = ttk.Panedwindow(main, orient='vertical')
        splitter.pack(fill='both', expand=True)
        listing = ttk.Frame(splitter)
        splitter.add(listing, weight=3)
        columns = ('id','age','area','category','score','confidence','families','status','remaining','title')
        self.column_labels = dict(zip(columns, ['ID','Age','Area','Type','Sig','Confidence','Src','State','Validity','Report']))
        self.tree = ttk.Treeview(listing, columns=columns, show='headings', selectmode='extended', height=10)
        for key, label, width in zip(columns, ['ID','Age','Area','Type','Sig','Confidence','Src','State','Validity','Report'], [48,48,90,125,42,210,38,210,90,420]):
            self.tree.heading(key, text=label, command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, minwidth=35, stretch=key=='title')
        ys = ttk.Scrollbar(listing, orient='vertical', command=self.tree.yview)
        xs = ttk.Scrollbar(listing, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.tag_configure('suppressed', foreground='#b6a6a0')
        self.tree.tag_configure('new', foreground='#e7edf4')
        self.tree.grid(row=0, column=0, sticky='nsew')
        ys.grid(row=0, column=1, sticky='ns')
        xs.grid(row=1, column=0, sticky='ew')
        listing.rowconfigure(0, weight=1)
        listing.columnconfigure(0, weight=1)
        self.tree.bind('<<TreeviewSelect>>', self.select)
        lower = ttk.Frame(splitter, padding=(0, 12, 0, 0))
        splitter.add(lower, weight=2)
        actions = ttk.Frame(lower)
        actions.pack(fill='x', pady=(0, 8))
        self.action_buttons = {}
        for label, target in [('Start review','REVIEW'),('Correlate','CORRELATING'),('TX candidate','TX_CANDIDATE'),('Reject','REJECTED')]:
            button = self._button(actions, label, lambda t=target: self.mark(t))
            button.pack(side='left', padx=(0, 6))
            self.action_buttons[target] = button
        self.relay_button = self._button(actions, 'Relay anyway…', self.relay, style='Relay.TButton')
        self.relay_button.pack(side='left', padx=(3, 6))
        self.format_button = self._button(actions, 'Prepare message…', self.format_message)
        self.format_button.pack(side='right')
        self.link_button = self._button(actions, 'Source links…', self.source_links)
        self.link_button.pack(side='right', padx=8)
        self.add_article_button = self._button(actions, 'Add article…', self.add_article)
        self.add_article_button.pack(side='right', padx=8)
        self.bulk_button = self._button(actions, 'Bulk edit…', self.bulk_edit)
        self.bulk_button.pack(side='right', padx=8)
        self.merge_button = self._button(actions, 'Group duplicates…', self.merge_selected)
        self.merge_button.pack(side='right', padx=8)
        self.wire_button = self._button(actions, 'Log wire TX…', self.record_wire)
        self.wire_button.pack(side='right', padx=8)
        self.details = self._text(lower, height=11)
        self._set_text(self.details, 'Select a report to inspect its sources, relevance and review state.\nID is the claim ID. Collection runs only when you click Collect now.')
        footer = ttk.Frame(main)
        footer.pack(fill='x', pady=(10, 0), side='bottom', before=splitter)
        self.progress = ttk.Progressbar(footer, mode='indeterminate', length=100)
        self.progress.pack(side='right')
        ttk.Label(footer, textvariable=self.status, style='Muted.TLabel', wraplength=1050).pack(side='left')
        self._build_daily_wire(notebook)
        notebook.bind('<<NotebookTabChanged>>', self._wire_tab_shown)

    def _wire_tab_shown(self, event):
        # Lazy first load only - opening the Daily Wire tab shouldn't compete
        # with an Inbox refresh already in flight, and revisiting the tab
        # later doesn't re-fetch until the operator asks it to.
        if not self.busy and not self.wire_all_rows and event.widget.tab(event.widget.select(), 'text') == 'Daily Wire':
            self.wire_refresh()

    def _build_daily_wire(self, notebook):
        wire = ttk.Frame(notebook, padding=20)
        notebook.add(wire, text='Daily Wire')
        ttk.Label(wire, text='Daily Wire', style='Title.TLabel').pack(anchor='w')
        ttk.Label(wire, text='Review, order, annotate and export the current TX_CANDIDATE queue. '
                             'Nothing here changes a report’s workflow state or transmits anything.',
                  style='Muted.TLabel', wraplength=1100).pack(anchor='w', pady=(2, 12))
        toolbar = ttk.Frame(wire)
        toolbar.pack(fill='x', pady=(0, 8))
        self._button(toolbar, 'Refresh Daily Wire', self.wire_refresh).pack(side='left', padx=(0, 8))
        self.wire_preview_button = self._button(toolbar, 'Preview & export…', self.wire_preview_export)
        self.wire_preview_button.pack(side='left', padx=(0, 8))
        ttk.Label(toolbar, textvariable=self.wire_status, style='Muted.TLabel').pack(side='right')
        filters = ttk.Frame(wire)
        filters.pack(fill='x', pady=(0, 8))
        for variable, values, width in [
            (self.wire_area, ['All areas', 'ETN/WNC', 'REGIONAL', 'NATIONAL', 'GLOBAL', 'DISTANT'], 13),
            (self.wire_category, ['All categories', 'top-news', 'cyber', 'infrastructure', 'communications',
                                   'grid', 'fuel', 'commodity', 'transportation', 'public-safety',
                                   'public-health', 'civil-liberties', 'politics', 'space-weather',
                                   'earthquake', 'weather', 'fire-weather', 'disaster', 'chatter'], 18),
        ]:
            box = self._combo(filters, variable, values, width)
            box.pack(side='left', padx=(0, 8))
            box.bind('<<ComboboxSelected>>', lambda event: self.wire_render())
        warn_only = ttk.Checkbutton(filters, text='Warnings only', variable=self.wire_warnings_only,
                                     command=self.wire_render)
        warn_only.pack(side='left', padx=(0, 8))
        self.controls.append(warn_only)
        show_hidden = ttk.Checkbutton(filters, text='Show removed items', variable=self.wire_show_hidden,
                                       command=self.wire_refresh)
        show_hidden.pack(side='left')
        self.controls.append(show_hidden)
        searchbar = ttk.Frame(wire)
        searchbar.pack(fill='x', pady=(0, 10))
        ttk.Label(searchbar, text='Search').pack(side='left')
        entry = ttk.Entry(searchbar, textvariable=self.wire_search, width=32)
        entry.pack(side='left', padx=(8, 16))
        entry.bind('<Return>', lambda event: self.wire_render())
        self.controls.append(entry)
        self._button(searchbar, 'Apply filters', self.wire_render).pack(side='left')
        splitter = ttk.Panedwindow(wire, orient='vertical')
        splitter.pack(fill='both', expand=True)
        listing = ttk.Frame(splitter)
        splitter.add(listing, weight=3)
        columns = ('id', 'order', 'area', 'category', 'confidence', 'score', 'location', 'warnings', 'title')
        labels = ['ID', '#', 'Area', 'Type', 'Confidence', 'Sig', 'Location', 'Warnings', 'Report']
        widths = [48, 36, 90, 125, 170, 42, 150, 220, 300]
        self.wire_tree = ttk.Treeview(listing, columns=columns, show='headings', selectmode='browse', height=10)
        for key, label, width in zip(columns, labels, widths):
            self.wire_tree.heading(key, text=label)
            self.wire_tree.column(key, width=width, minwidth=35, stretch=key == 'title')
        ys = ttk.Scrollbar(listing, orient='vertical', command=self.wire_tree.yview)
        self.wire_tree.configure(yscrollcommand=ys.set)
        self.wire_tree.tag_configure('warning', foreground='#e0a45c')
        self.wire_tree.tag_configure('hidden', foreground='#6d7f8f')
        self.wire_tree.grid(row=0, column=0, sticky='nsew')
        ys.grid(row=0, column=1, sticky='ns')
        listing.rowconfigure(0, weight=1)
        listing.columnconfigure(0, weight=1)
        self.wire_tree.bind('<<TreeviewSelect>>', self.wire_select)
        lower = ttk.Frame(splitter, padding=(0, 12, 0, 0))
        splitter.add(lower, weight=2)
        actions = ttk.Frame(lower)
        actions.pack(fill='x', pady=(0, 8))
        self.wire_up_button = self._button(actions, 'Move up', lambda: self.wire_move(-1))
        self.wire_up_button.pack(side='left', padx=(0, 6))
        self.wire_down_button = self._button(actions, 'Move down', lambda: self.wire_move(1))
        self.wire_down_button.pack(side='left', padx=(0, 6))
        self.wire_hide_button = self._button(actions, 'Remove from Daily Wire', self.wire_toggle_hidden)
        self.wire_hide_button.pack(side='left', padx=(0, 6))
        self.wire_reject_button = self._button(actions, 'Reject…', lambda: self.wire_mark('REJECTED'))
        self.wire_reject_button.pack(side='right', padx=8)
        self.wire_review_button = self._button(actions, 'Back to review', lambda: self.wire_mark('REVIEW'))
        self.wire_review_button.pack(side='right', padx=8)
        note_row = ttk.Frame(lower)
        note_row.pack(fill='x', pady=(0, 8))
        ttk.Label(note_row, text='Operator note').pack(side='left')
        self.wire_note_var = tk.StringVar()
        note_entry = ttk.Entry(note_row, textvariable=self.wire_note_var, width=70)
        note_entry.pack(side='left', padx=(8, 8))
        self.controls.append(note_entry)
        self.wire_save_note_button = self._button(note_row, 'Save note', self.wire_save_note)
        self.wire_save_note_button.pack(side='left')
        self.wire_details = self._text(lower, height=9)
        self._set_text(self.wire_details, 'Select a Daily Wire item to inspect its evidence and warnings.')
        self.wire_action_buttons = (self.wire_up_button, self.wire_down_button, self.wire_hide_button,
                                     self.wire_reject_button, self.wire_review_button, self.wire_save_note_button)
        self._wire_enabled()

    def _text(self, parent, height=15):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True)
        text = tk.Text(frame, bg=PANEL, fg=TEXT, insertbackground=TEXT, selectbackground='#205d6a',
                       font=('Consolas', 10), relief='flat', padx=12, pady=10, wrap='word', height=height)
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)
        return text

    def _set_text(self, widget, content):
        widget.configure(state='normal')
        widget.delete('1.0','end')
        widget.insert('1.0',content)
        widget.configure(state='disabled')

    def _enabled(self):
        for widget in self.controls:
            widget.configure(state='disabled' if self.busy else 'readonly' if isinstance(widget, ttk.Combobox) else 'normal')
        selected_count = len(self.tree.selection())
        single = selected_count == 1
        allowed = ALLOWED_TRANSITIONS.get(self.claim['status'], set()) if single and self.claim and not self.claim['expired'] else set()
        for target, button in self.action_buttons.items():
            button.configure(state='normal' if not self.busy and target in allowed else 'disabled')
        self.relay_button.configure(state='normal' if not self.busy and 'TX_CANDIDATE' in allowed else 'disabled')
        self.format_button.configure(state='normal' if not self.busy and single and self.claim and self.claim['status'] in {'REVIEW','TX_CANDIDATE'} and not self.claim['expired'] else 'disabled')
        self.link_button.configure(state='normal' if not self.busy and single and self.observations else 'disabled')
        self.add_article_button.configure(state='normal' if not self.busy and single and self.claim and not self.claim['expired'] else 'disabled')
        self.bulk_button.configure(state='normal' if not self.busy and selected_count > 1 else 'disabled')
        self.merge_button.configure(state='normal' if not self.busy and selected_count > 1 else 'disabled')
        self.wire_button.configure(state='normal' if not self.busy and single and self.claim and self.claim['status'] != 'MERGED' else 'disabled')
        if self.collecting:
            self.collect_button.configure(state='disabled')
        self._wire_enabled()

    def _wire_enabled(self):
        selected = self.wire_tree.selection()
        single = bool(selected) and self.wire_claim is not None
        for button in self.wire_action_buttons:
            button.configure(state='normal' if not self.busy and single else 'disabled')
        self.wire_preview_button.configure(state='normal' if not self.busy and self.wire_rows else 'disabled')
        if single:
            allowed = ALLOWED_TRANSITIONS.get(self.wire_claim['status'], set())
            self.wire_reject_button.configure(state='normal' if not self.busy and 'REJECTED' in allowed else 'disabled')
            self.wire_review_button.configure(state='normal' if not self.busy and 'REVIEW' in allowed else 'disabled')
            index = next((i for i, item in enumerate(self.wire_rows) if item['id'] == self.wire_claim['id']), None)
            self.wire_up_button.configure(state='normal' if not self.busy and index else 'disabled')
            self.wire_down_button.configure(
                state='normal' if not self.busy and index is not None and index < len(self.wire_rows) - 1 else 'disabled')

    def wire_refresh(self):
        if self.busy:
            return
        mode = self.mode.get().lower()
        include_hidden = self.wire_show_hidden.get()
        self.work('Loading Daily Wire…', lambda: self.service.daily_wire(mode, include_hidden=include_hidden),
                   self.wire_loaded)

    def wire_loaded(self, items):
        self.wire_all_rows = items
        self.wire_render()

    def wire_render(self):
        rows = getattr(self, 'wire_all_rows', [])
        area = self.wire_area.get()
        category = self.wire_category.get()
        search = self.wire_search.get().casefold()
        warnings_only = self.wire_warnings_only.get()
        filtered = []
        for item in rows:
            if area != 'All areas' and item['area'] != area: continue
            if category != 'All categories' and item['category'] != category: continue
            if warnings_only and not item['wire_warnings']: continue
            if search and search not in (item['title'] + ' ' + item['location']).casefold(): continue
            filtered.append(item)
        self.wire_rows = filtered
        previous = tuple(self.wire_tree.selection())
        self.wire_tree.delete(*self.wire_tree.get_children())
        for index, item in enumerate(self.wire_rows, 1):
            warn_text = '; '.join(item['wire_warnings']) or '—'
            tags = []
            if item['wire_warnings']: tags.append('warning')
            if item['wire_hidden']: tags.append('hidden')
            self.wire_tree.insert('', 'end', iid=str(item['id']), values=(
                item['id'], index, item['area'], item['category'].upper(), item['confidence_label'],
                item['score'], item['location'] or '—', warn_text, item['title']), tags=tuple(tags))
        self.wire_status.set(f'{len(self.wire_rows)} of {len(rows)} Daily Wire item(s)')
        retained = [i for i in previous if self.wire_tree.exists(i)]
        if retained:
            self.wire_tree.selection_set(retained)
            self.wire_tree.see(retained[0])
        else:
            self.wire_claim = None
        self._wire_enabled()

    def wire_select(self, event=None):
        selected = self.wire_tree.selection()
        if self.busy or not selected:
            self.wire_claim = None
            self._wire_enabled()
            return
        claim_id = int(selected[0])
        self.wire_claim = next((item for item in self.wire_rows if item['id'] == claim_id), None)
        if self.wire_claim is None:
            self._wire_enabled()
            return
        self.wire_note_var.set(self.wire_claim['wire_note'])
        self._wire_enabled()
        # One detail() call for the selected item's full evidence/provenance -
        # same cost as the Inbox tab already pays per selection, not a
        # per-row cost of the (already bulk-loaded) Daily Wire list itself.
        mode = self.mode.get().lower()
        self.work('Loading Daily Wire evidence…', lambda: self.service.detail(claim_id, mode), self.wire_show_detail)

    def wire_show_detail(self, result):
        claim, observations = result
        if not self.wire_claim or self.wire_claim['id'] != claim['id']:
            return
        lines = [line for line in evidence_summary(claim, observations).splitlines()
                 if not line.startswith(('NEXT:', 'FORMAT:'))]
        lines.append('WARNINGS: ' + ('; '.join(self.wire_claim['wire_warnings']) or 'none'))
        self._set_text(self.wire_details, '\n'.join(lines))

    def wire_save_note(self):
        if not self.wire_claim or self.busy:
            return
        claim_id = self.wire_claim['id']
        note = self.wire_note_var.get()
        self.work('Saving Daily Wire note…', lambda: self.service.wire_annotate(claim_id, note=note),
                   lambda result: self.wire_refresh())

    def wire_toggle_hidden(self):
        if not self.wire_claim or self.busy:
            return
        claim_id = self.wire_claim['id']
        hide = not self.wire_claim['wire_hidden']
        verb = 'Removing from' if hide else 'Restoring to'
        self.work(f'{verb} Daily Wire…', lambda: self.service.wire_annotate(claim_id, hidden=hide),
                   lambda result: self.wire_refresh())

    def wire_move(self, direction):
        if not self.wire_claim or self.busy:
            return
        index = next((i for i, item in enumerate(self.wire_rows) if item['id'] == self.wire_claim['id']), None)
        if index is None:
            return
        target = index + direction
        if not 0 <= target < len(self.wire_rows):
            return
        ordered = list(self.wire_rows)
        ordered[index], ordered[target] = ordered[target], ordered[index]
        ordered_ids = [item['id'] for item in ordered]
        self.work('Reordering Daily Wire…', lambda: self.service.wire_reorder(ordered_ids),
                   lambda result: self.wire_refresh())

    def wire_mark(self, target):
        if not self.wire_claim or self.busy:
            return
        claim_id, mode = self.wire_claim['id'], self.mode.get().lower()
        if not messagebox.askyesno('Confirm', f'Move report {claim_id} to {target}?', parent=self.root):
            return
        self.work('Updating review state…', lambda: self.service.mark(claim_id, target, mode),
                   lambda result: (self.wire_refresh(), self.refresh()))

    def wire_preview_export(self):
        if self.busy or not self.wire_rows:
            return
        ids = [item['id'] for item in self.wire_rows]
        mode = self.mode.get().lower()
        self.work('Preparing Daily Wire preview…', lambda: self.service.wire_export(ids, mode),
                   lambda lines: self.wire_preview_dialog('\n'.join(lines)))

    def wire_preview_dialog(self, content):
        window = self.dialog('Daily Wire preview', '780x460')
        ttk.Label(window, text='Preview only • Copying or saving does not transmit or invoke FIGHT.',
                  style='Muted.TLabel').pack(anchor='w', pady=8)
        text = self._text(window, 14)
        self._set_text(text, content)
        buttons = ttk.Frame(window)
        buttons.pack(fill='x', pady=12)
        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self.status.set('Daily Wire preview copied to clipboard. Nothing transmitted.')
        def save():
            path = filedialog.asksaveasfilename(parent=window, defaultextension='.txt',
                                                 initialfile='shade-daily-wire.txt', filetypes=[('Text file', '*.txt')])
            if path:
                try:
                    Path(path).write_text(content + '\n', encoding='utf-8')
                except OSError as exc:
                    messagebox.showerror('Cannot save', str(exc), parent=window)
        ttk.Button(buttons, text='Copy preview', command=copy).pack(side='left', padx=(0, 8))
        ttk.Button(buttons, text='Save text…', command=save).pack(side='left')
        ttk.Button(buttons, text='Close', command=window.destroy).pack(side='right')

    def work(self, label, function, done):
        if self.busy:
            return
        self.busy = True
        self.status.set(label)
        self.progress.start(15)
        self._enabled()
        def task():
            try:
                self.results.put((done, function(), None))
            except Exception as exc:
                self.results.put((done, None, str(exc)))
        self.pool.submit(task)

    def _drain(self):
        try:
            item = self.results.get_nowait()
            if len(item) == 4 and item[3] == 'collect':
                done, result, error, _ = item
                self.collecting = False
                self.collect_button.configure(text='Collect now')
                self._enabled()
                if error:
                    messagebox.showerror('SHADE collection', error, parent=self.root)
                else:
                    done(result)
                self.root.after(80, self._drain)
                return
            if len(item) == 4 and item[3] == 'probe':
                _, signature, error, _ = item
                self.probing = False
                stale = time.monotonic() - self.last_full_refresh >= FORCED_REFRESH_SECONDS
                if not error and (signature != self.data_signature or stale) and not self.busy:
                    self.refresh()
                self.root.after(80, self._drain)
                return
            done, result, error = item[:3]
            self.busy = False
            self.progress.stop()
            self._enabled()
            if error:
                self.status.set('Operation failed. Your evidence has not been deleted.')
                messagebox.showerror('SHADE', error, parent=self.root)
            else:
                self.status.set('Ready')
                done(result)
        except messages.Empty:
            pass
        self.root.after(80, self._drain)

    def refresh(self):
        if self.busy:
            return
        try:
            minimum = int(self.minimum.get()) if self.minimum.get().strip() else None
            max_age = float(self.max_age.get()) if self.max_age.get().strip() else None
            if minimum is not None and not 0 <= minimum <= 100:
                raise ValueError('Minimum score must be between 0 and 100.')
            if max_age is not None and (max_age < 0 or not __import__('math').isfinite(max_age)):
                raise ValueError('Age must be a nonnegative number of hours.')
        except ValueError as exc:
            messagebox.showerror('Check filters',str(exc),parent=self.root)
            return
        options = dict(mode=self.mode.get().lower(), lane={'Inbox':'inbox','Top news':'context','NOW':'now','All evidence':'all'}[self.lane.get()],
                       category=None if self.category.get()=='All categories' else self.category.get(),
                       area=None if self.area.get()=='All areas' else self.area.get(),
                       status=None if self.state.get()=='Active states' else self.state.get(),
                       include_suppressed=self.broad.get(), search=self.search.get(), minimum=minimum, max_age=max_age)
        def load():
            signature = self.service.data_signature()
            return signature, self.service.listing(**options)
        self.work('Reading local evidence…', load, self._render_with_signature)

    def _render_with_signature(self, payload):
        signature, rows = payload
        self.data_signature = signature
        self.last_full_refresh = time.monotonic()
        self.render(rows)

    def render(self, rows):
        selected = self.tree.selection()
        previous = tuple(selected)
        self.tree.delete(*self.tree.get_children())
        self.rows = sorted_inbox(rows, self.sort_key, self.sort_descending) if self.sort_key else rows
        self.claim = None
        self.observations = []
        for row in self.rows:
            self.tree.insert('', 'end', iid=str(row['id']), values=(row['id'],age(row['published']),row['area'],row['category'].upper(),row['score'],row['confidence_label'],row['independent_families'],workflow_status(row),remaining(row['expires']) if row['expires'] else '—',row['title']), tags=('suppressed' if row['status']=='SUPPRESSED' else 'new' if row['status']=='NEW' else '',))
        self.count.set(f'{len(rows)} reports')
        self._set_text(self.details, 'Select a report above.' if rows else 'No reports match this view.\nTry NOW for local life-safety weather, or All evidence + Show suppressed and routine for retained reports.\nExpired reports require selecting EXPIRED in the state filter.')
        self._enabled()
        retained = [item for item in previous if self.tree.exists(item)]
        if retained:
            self.tree.selection_set(retained)
            self.tree.see(retained[0])
        self.status.set('View refreshed • F5 refreshes local data; Collect now polls public sources.')

    def select(self, event=None):
        selected = self.tree.selection()
        if self.busy:
            return
        if not selected:
            self.claim = None
            self.observations = []
            self._enabled()
            return
        if len(selected) > 1:
            self.claim = None
            self.observations = []
            self._set_text(self.details, f'{len(selected)} reports selected. Use Bulk edit to move them to review, correlation, rejection, or TX_CANDIDATE. Each report is still individually checked for corroboration.')
            self._enabled()
            return
        self.claim = None
        self.observations = []
        claim_id = int(selected[0])
        mode = self.mode.get().lower()
        self.work('Loading report and attribution…', lambda: self.service.detail(claim_id, mode), self.show_detail)

    def show_detail(self, result):
        self.claim, self.observations = result
        selected = self.tree.selection()
        if len(selected) != 1 or int(selected[0]) != self.claim['id']:
            self.claim = None
            self.observations = []
            self._enabled()
            self.select()
            return
        self._set_text(self.details, '\n'.join(line for line in evidence_summary(self.claim,self.observations).splitlines() if not line.startswith(('NEXT:', 'FORMAT:'))))
        self._enabled()
        self.status.set(f"Report {self.claim['id']} • {self.claim['confidence_label']} • {workflow_status(self.claim)}")

    def sort_by(self, key):
        if self.sort_key == key:
            self.sort_descending = not self.sort_descending
        else:
            self.sort_key = key
            self.sort_descending = key in {'age', 'score', 'confidence', 'families', 'id'}
        self.rows = sorted_inbox(self.rows, key, self.sort_descending)
        for column, label in self.column_labels.items():
            arrow = ((' ↑' if self.sort_descending else ' ↓') if key == 'age' else (' ↓' if self.sort_descending else ' ↑')) if column == key else ''
            self.tree.heading(column, text=label + arrow)
        for index, row in enumerate(self.rows):
            self.tree.move(str(row['id']), '', index)
        self.status.set(f"Sorted by {key}. Click the heading again to reverse.")

    def bulk_edit(self):
        ids = [int(item) for item in self.tree.selection()]
        if len(ids) < 2 or self.busy:
            return
        window = self.dialog(f'Bulk edit {len(ids)} reports', '520x240')
        ttk.Label(window, text=f'{len(ids)} selected reports. Choose one review state for all.').pack(anchor='w', pady=8)
        choice = tk.StringVar(value='REJECTED')
        box = ttk.Combobox(window, textvariable=choice, values=['REJECTED', 'REVIEW', 'CORRELATING', 'TX_CANDIDATE'], state='readonly')
        box.pack(anchor='w', pady=8)
        ttk.Label(window, text='Every report must allow the change. If one fails, none are changed. '
                               'TX_CANDIDATE still requires official/media corroboration on each report - '
                               'a report without it will block the whole batch rather than being silently skipped.',
                  style='Muted.TLabel', wraplength=480).pack(anchor='w')
        def apply():
            target = choice.get()
            warn = ' This will make them eligible for transmission by the operator.' if target == 'TX_CANDIDATE' else ''
            if not messagebox.askyesno('Confirm bulk edit', f'Move {len(ids)} reports to {target}?{warn}', parent=window):
                return
            window.destroy()
            mode = self.mode.get().lower()
            def done(count):
                messagebox.showinfo('Bulk edit complete', f'{count} reports moved to {target}.', parent=self.root)
                self.refresh()
            self.work('Updating selected reports…', lambda: self.service.mark_many(ids, target, mode), done)
        ttk.Button(window, text='Apply to selected', command=apply).pack(anchor='e', pady=16)

    def merge_selected(self):
        ids = [int(item) for item in self.tree.selection()]
        if len(ids) < 2 or self.busy:
            return
        window = self.dialog(f'Group {len(ids)} reports', '620x290')
        ttk.Label(window, text='These headlines must describe the same event. Choose the report to keep.').pack(anchor='w', pady=8)
        primary = tk.StringVar(value=str(ids[0]))
        ttk.Combobox(window, textvariable=primary, values=[str(x) for x in ids], state='readonly').pack(anchor='w')
        ttk.Label(window, text='All source evidence stays attached to the group. Other reports are hidden from the inbox.', style='Muted.TLabel').pack(anchor='w', pady=10)
        def save():
            keep = int(primary.get())
            if not messagebox.askyesno('Group duplicate coverage?',
                    f'Group {len(ids)} reports under report {keep}? Verify they describe the same event.', parent=window):
                return
            window.destroy()
            self.work('Grouping duplicate coverage…',
                      lambda: self.service.merge_reports(keep, [x for x in ids if x != keep]),
                      lambda count: self.refresh())
        ttk.Button(window, text='Group selected reports', command=save).pack(anchor='e', pady=12)

    def record_wire(self):
        if not self.claim or self.busy:
            return
        claim_id = self.claim['id']
        window = self.dialog(f'Record actual wire transmission • report {claim_id}', '720x400')
        ttk.Label(window, text='Enter only a message you actually transmitted. This will mark the report SENT.').pack(anchor='w', pady=6)
        sent_at = tk.StringVar(value=datetime.now().astimezone().isoformat(timespec='seconds'))
        channel = tk.StringVar(value='JS8')
        ttk.Label(window, text='Actual time with timezone').pack(anchor='w')
        ttk.Entry(window, textvariable=sent_at, width=38).pack(anchor='w')
        ttk.Label(window, text='Channel').pack(anchor='w', pady=(8,0))
        ttk.Entry(window, textvariable=channel, width=20).pack(anchor='w')
        ttk.Label(window, text='Exact transmitted text').pack(anchor='w', pady=(8,0))
        message = self._text(window, 5)
        def save():
            body = message.get('1.0', 'end').strip()
            if not messagebox.askyesno('Confirm actual transmission?',
                    f'Confirm you already sent this text on {channel.get()} at {sent_at.get()}?', parent=window):
                return
            window.destroy()
            self.work('Recording wire transmission…',
                      lambda: self.service.record_wire(claim_id, sent_at.get(), channel.get(), body),
                      lambda recorded: self.refresh())
        ttk.Button(window, text='Record transmitted message', command=save).pack(anchor='e', pady=10)

    def add_article(self):
        if not self.claim or self.busy or len(self.tree.selection()) != 1:
            return
        claim_id = self.claim['id']
        window = self.dialog(f'Add article to report {claim_id}', '720x320')
        ttk.Label(window, text='Paste the original news article link you opened and checked.').pack(anchor='w', pady=8)
        url = tk.StringVar()
        entry = ttk.Entry(window, textvariable=url, width=95)
        entry.pack(fill='x', pady=8)
        entry.focus_set()
        preview = self._text(window, 5)
        self._set_text(preview, 'The article headline and publisher will appear here before you attach it.')
        state = {'article': None}
        def fetched(article):
            state['article'] = article
            published = article['published_at'] or 'not supplied by page'
            original = token_set(self.claim['title'])
            linked = token_set(article['title'])
            overlap = len(original & linked) / max(1, len(original | linked))
            state['overlap'] = overlap
            match_note = 'Low headline overlap: verify event, place, and date carefully.' if overlap < 0.2 else 'Compare event, place, and date before attaching.'
            self._set_text(preview, f"Publisher: {article['publisher']}\nHeadline: {article['title']}\nPublished: {published}\nURL: {article['url']}\n{match_note}")
            attach.configure(state='normal')
        def fetch():
            state['article'] = None
            attach.configure(state='disabled')
            self.work('Opening article preview…', lambda: self.service.preview_article(url.get().strip()), fetched)
        def save():
            article = state['article']
            if not article:
                return
            if state.get('overlap', 0) < 0.2 and not messagebox.askyesno(
                    'Different headline', 'The article headline shares few words with the report. Did you verify the same event, place, and date?', parent=window):
                return
            if not messagebox.askyesno('Attach reviewed article?',
                f"Confirm this article describes the same event and is reliable reporting.\n\n{article['publisher']}: {article['title']}\n\nAttach it as media evidence?",
                parent=window):
                return
            window.destroy()
            self.work('Attaching article evidence…',
                      lambda: self.service.attach_article(claim_id, article, self.mode.get().lower()),
                      lambda added: self.refresh())
        buttons = ttk.Frame(window)
        buttons.pack(fill='x', pady=8)
        ttk.Button(buttons, text='Preview article', command=fetch).pack(side='left')
        attach = ttk.Button(buttons, text='Attach reviewed article', command=save, state='disabled')
        attach.pack(side='right')

    def change_mode(self, event=None):
        chosen = self.mode.get()
        if chosen == 'ACTUAL' and not messagebox.askyesno('Enter ACTUAL mode?', 'Use ACTUAL only for an intentional operator-directed workflow.\n\nThis does not declare an emergency or transmit. Enter ACTUAL mode?', parent=self.root):
            self.mode.set(self.last_mode)
            return
        self.last_mode = chosen
        self.banner.configure(text=f'{chosen} • '+('Exercise copy is marked at both ends.' if chosen=='EXERCISE' else 'Every ACTUAL message requires confirmation.' if chosen=='ACTUAL' else 'Operator review console • All transmission decisions remain human.'))
        self.refresh()

    def collect(self):
        if self.collecting:
            return
        mode = self.mode.get().lower()
        self.collecting = True
        self.collect_button.configure(text='Collecting…')
        self._enabled()
        self.status.set('Collecting public feeds in the background; review continues normally.')
        def task():
            try:
                self.results.put((self.collected, self.service.collect(mode), None, 'collect'))
            except Exception as exc:
                self.results.put((self.collected, None, str(exc), 'collect'))
        self.collect_pool.submit(task)

    def collected(self, result):
        summary = (f"{result['sources_ok']} feeds succeeded; {result['sources_partial']} partly succeeded; {result['sources_failed']} failed.\n"
                   f"{result['inserted']} new observations or revisions; {result['sources_skipped_cooldown']} feeds waiting for their polling interval.")
        if result['errors']:
            summary += '\n\n'+'\n'.join(result['errors'])
        messagebox.showinfo('Collection complete', summary, parent=self.root)
        self.refresh()

    def mark(self, target):
        if not self.claim or self.busy:
            return
        claim_id, mode = self.claim['id'], self.mode.get().lower()
        if target == 'SENT' and not messagebox.askyesno('Record an already-sent message?', 'This only records that YOU already transmitted this report manually.\n\nMark it SENT?', parent=self.root):
            return
        self.work('Updating review state…', lambda: self.service.mark(claim_id,target,mode), lambda result:self.refresh())

    def relay(self):
        if not self.claim or self.busy or self.claim['status'] != 'REVIEW':
            return
        if not messagebox.askyesno('Relay anyway?', 'This deliberately bypasses the official/media evidence gate. The operator relay basis will be audited. Continue?', parent=self.root):
            return
        claim_id, mode = self.claim['id'], self.mode.get().lower()
        self.work('Recording operator relay…', lambda: self.service.mark(claim_id, 'TX_CANDIDATE', mode, operator_override=True), lambda result: self.refresh())

    def format_message(self):
        if not self.claim or self.busy:
            return
        mode = self.mode.get().lower()
        confirmed = mode != 'actual' or messagebox.askyesno('Confirm ACTUAL formatting', 'I have verified the situation, operator authority and source evidence.\n\nPrepare ACTUAL copy for manual use?', parent=self.root)
        if not confirmed:
            return
        claim_id = self.claim['id']
        self.work('Preparing reviewed message…', lambda:self.service.format(claim_id,mode,confirmed), lambda text:self.message_dialog(text,claim_id,mode))

    def message_dialog(self, content, claim_id, mode):
        window = self.dialog(f'Report {claim_id} • {mode.upper()} • message preview', '760x380')
        ttk.Label(window,text='Prepared text only • Copying or saving does not transmit.').pack(anchor='w',pady=8)
        text = self._text(window, 8)
        self._set_text(text, content)
        buttons = ttk.Frame(window)
        buttons.pack(fill='x',pady=12)
        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self.status.set('Reviewed message copied to clipboard. Nothing transmitted.')
        def save():
            path = filedialog.asksaveasfilename(parent=window,defaultextension='.txt',initialfile=f'shade-report-{claim_id}.txt',filetypes=[('Text file','*.txt')])
            if path:
                try:
                    Path(path).write_text(content+'\n',encoding='utf-8')
                except OSError as exc:
                    messagebox.showerror('Cannot save',str(exc),parent=window)
        ttk.Button(buttons,text='Copy message',command=copy).pack(side='left',padx=(0,8))
        ttk.Button(buttons,text='Save text…',command=save).pack(side='left')
        ttk.Button(buttons,text='Close',command=window.destroy).pack(side='right')

    def dialog(self, title, geometry='850x500'):
        window = tk.Toplevel(self.root)
        window.title(title)
        width, height = map(int, geometry.split('x'))
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width()-width)//2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height()-height)//2)
        window.geometry(f'{geometry}+{x}+{y}')
        window.configure(bg=BG, padx=16,pady=12)
        window.transient(self.root)
        window.grab_set()
        window.bind('<Escape>', lambda event: window.destroy())
        return window

    def sources(self):
        mode = self.mode.get().lower()
        def display(rows):
            window = self.dialog('Sources & collection health')
            text = self._text(window)
            lines = ['Source status reflects the last attempted poll; it does not prove current coverage.\n']
            for source, active, poll in rows:
                state = 'Active' if active else 'Dormant / disabled'
                lines.append(f'{source.name} — {state}\n  Origin: {source.source_family} • Pack: {source.pack}')
                lines.append(f"  Last poll: {poll['last_attempt'] if poll else 'Never'} • Result: {'OK' if poll and poll['success'] else 'Failed' if poll else 'Not polled'}")
                if poll:
                    lines.append('  Next allowed: '+poll['next_allowed'])
                    if poll['error']: lines.append('  Error: '+poll['error'])
                lines.append('')
            self._set_text(text,'\n'.join(lines))
        self.work('Reading source health…',lambda:self.service.sources(mode),display)

    def source_links(self):
        window = self.dialog('Original source links', '900x450')
        text = self._text(window)
        self._set_text(text,'Click a source URL to open it in your browser. After checking an original article, use Add article to attach it to this report.\n\n')
        text.configure(state='normal')
        seen = set()
        ordered = sorted(self.observations, key=lambda row: row['url'].startswith('https://news.google.com/rss/articles/'))
        for index,row in enumerate(ordered):
            url = row['url']
            if (row['source_family'],url) in seen:
                continue
            seen.add((row['source_family'],url))
            wrapper = row['url'].startswith('https://news.google.com/rss/articles/')
            label = 'Google News result; open it to find the publisher article' if wrapper else 'Direct source link'
            text.insert('end',f"{row['source_name']} [{row['source_family']}] • {label}\n")
            tag = f'link{index}'
            text.insert('end',url+'\n\n',tag)
            text.tag_configure(tag,foreground=ACCENT,underline=True)
            if url.startswith('https://'):
                text.tag_bind(tag,'<Button-1>',lambda event,u=url:webbrowser.open(u))
        text.configure(state='disabled')
        ttk.Button(window, text='Add article…', command=lambda: (window.destroy(), self.add_article())).pack(anchor='e', pady=8)

    def cleanup(self):
        mode = self.mode.get().lower()
        def preview(result):
            (_, changes), plan = result
            window = self.dialog('Housekeeping preview')
            text = self._text(window)
            lines = [f"Report {c['id']} → {c['status']}: {c['reason']}" for c in changes] or ['No expiration or suppression changes needed.']
            lines += ['', f"PURGE (manual only): at {plan['hours']:g} h, {len(plan['claim_ids'])} of {plan['total_claims']} reports have not been "
                      f"collected or re-seen since {plan['cutoff']} and can be permanently deleted with their evidence.",
                      f"{plan['protected_old']} older reports are always kept: TX_CANDIDATE, SENT, wire-recorded, or merged with one of those.",
                      'Choose how many hours to keep below (1-72). A verified full backup is written first; the database is then compacted.']
            self._set_text(text, '\n'.join(lines))
            def apply():
                window.destroy()
                self.work('Backing up and applying housekeeping…',lambda:self.service.cleanup(mode,apply=True,suppress=True,expected=changes),applied)
            def applied(result):
                backup, changed = result
                messagebox.showinfo('Housekeeping complete',f'{len(changed)} reports updated.\nEvidence preserved.\nBackup: {backup}',parent=self.root)
                self.refresh()
            def purge():
                try:
                    hours = float(self.purge_hours.get())
                    if not 1 <= hours <= 72:
                        raise ValueError
                except (ValueError, tk.TclError):
                    messagebox.showerror('Check hours', 'Keep between 1 and 72 hours.', parent=window)
                    return
                window.destroy()
                def confirm(fresh):
                    if not fresh['claim_ids']:
                        messagebox.showinfo('Nothing to purge', f'No reports are older than {hours:g} hours.', parent=self.root)
                        return
                    if not messagebox.askyesno('Permanently delete old reports?',
                            f"Delete {len(fresh['claim_ids'])} reports first collected more than {hours:g} hours ago?\n\n"
                            f"{fresh['protected_old']} older TX_CANDIDATE / SENT / wire-recorded reports are kept.\n"
                            'A verified backup is written first. This cannot be undone from inside SHADE.', parent=self.root):
                        return
                    self.work('Backing up, purging and compacting… this can take a few minutes.',
                              lambda: self.service.purge_apply(fresh), purged)
                self.work(f'Counting reports older than {hours:g} hours…', lambda: self.service.purge_preview(hours), confirm)
            def purged(result):
                backup, deleted, journal = result
                messagebox.showinfo('Purge complete', f"{deleted['claims']} reports and {deleted['observations']} observations deleted.\n"
                                    f"Backup: {backup}\nDatabase journal: {journal}", parent=self.root)
                self.refresh()
            buttons = ttk.Frame(window); buttons.pack(fill='x', pady=10)
            ttk.Label(buttons, text='Keep reports seen in the last').pack(side='left')
            ttk.Spinbox(buttons, from_=1, to=72, increment=1, width=5, textvariable=self.purge_hours).pack(side='left', padx=6)
            ttk.Label(buttons, text='hours').pack(side='left')
            ttk.Button(buttons, text='Purge older reports…', command=purge).pack(side='left', padx=(12, 0))
            if changes:
                ttk.Button(buttons,text=f'Apply housekeeping ({len(changes)} reports)',command=apply).pack(side='right')
        try:
            preview_hours = min(72.0, max(1.0, float(self.purge_hours.get())))
        except (ValueError, tk.TclError):
            preview_hours = 72.0
        self.work('Previewing housekeeping…',lambda:(self.service.cleanup(mode,suppress=True), self.service.purge_preview(preview_hours)),preview)

    def open_station(self):
        path = filedialog.askopenfilename(parent=self.root,title='Open an existing station configuration',filetypes=[('TOML configuration','*.toml')])
        if not path:
            return
        def loaded(service):
            self.service = service
            self.mode.set('STANDARD')
            self.last_mode = 'STANDARD'
            self.claim = None
            self.change_mode()
        self.work('Opening station configuration…',lambda:DesktopService(path),loaded)

    def help(self):
        window = self.dialog('Using SHADE', '800x500')
        text = self._text(window)
        self._set_text(text, '1. Collect now checks permitted public feeds once; source cooldowns still apply.\n\n'
            '2. Inbox is durable operational intelligence. NOW is current local life-safety weather.\n\n'
            '3. Click column headings to sort, including Age. Ctrl/Shift select multiple reports, then Bulk edit their review state. Source links open original evidence; Add article attaches a checked original news article.\n\n'
            '4. Start review → Prepare message → TX candidate. Relay anyway asks for confirmation and records an operator override. Copy/save are manual exports only. Record as sent is an after-the-fact annotation.\n\n'
            '5. Show suppressed and routine includes explicit SUPPRESSED states and routine bulletins; NEW and SUPPRESSED appear as separate states. Select EXPIRED, REJECTED or SENT to inspect those states.\n\n'
            '6. Housekeeping previews expiration/suppression, then requires Apply. It also offers a manual purge: choose how many hours to keep (1-72) and it permanently deletes reports first collected before that window (any state; they do not come back as new). TX_CANDIDATE, SENT and wire-recorded reports are always kept. Both write a verified backup first.\n\n'
            '7. The app opens in STANDARD. EXERCISE marks both ends. ACTUAL requires an explicit mode choice and confirmation for each formatted message.\n\n'
            'F5 refreshes local evidence. While idle the view checks for new data every minute and fully refreshes when something changed (and at least every 10 minutes); Collect now runs in the background; collection is never automatically scheduled by this window.\n\n'
            'Close and reopen safely at any time after the current operation finishes. Configuration and evidence stay local.')

    def _tick(self):
        # Cheap change probe instead of a full rescore every minute: the full
        # listing only re-runs when evidence/workflow data changed, or every
        # FORCED_REFRESH_SECONDS so time-based freshness/expiry stays current.
        if not self.busy and not self.probing and len(self.root.winfo_children()) <= 1:
            self.probing = True
            def task():
                try:
                    self.results.put((None, self.service.data_signature(), None, 'probe'))
                except Exception as exc:
                    self.results.put((None, None, str(exc), 'probe'))
            self.probe_pool.submit(task)
        self.root.after(60000,self._tick)

    def close(self):
        if self.busy:
            messagebox.showinfo('Operation in progress','Please let the current operation finish before closing.',parent=self.root)
            return
        if self.collecting and not messagebox.askyesno('Collection in progress',
                'A feed collection is still running in the background. Close anyway?\n\n'
                'Evidence already saved is kept; the scheduled collector continues independently.', parent=self.root):
            return
        self.pool.shutdown(wait=False)
        self.collect_pool.shutdown(wait=False, cancel_futures=True)
        self.probe_pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main(argv=None):
    arguments = argparse.ArgumentParser(description='SHADE desktop operator console')
    arguments.add_argument('--config', default=str(Path.cwd()/'config.toml'))
    args = arguments.parse_args(argv)
    root = tk.Tk()
    root.withdraw()
    _set_window_icon(root)
    splash = _show_splash(root)
    splash_shown_at = time.monotonic()
    path = args.config
    if not Path(path).is_file():
        splash.destroy()
        path = filedialog.askopenfilename(parent=root,title='Choose your existing SHADE config.toml',filetypes=[('TOML configuration','*.toml')])
        if not path:
            root.destroy()
            return 0
    try:
        service = DesktopService(path)
        ShadeWindow(root,service)
    except Exception as exc:
        if splash.winfo_exists():
            splash.destroy()
        messagebox.showerror('SHADE could not open this station',str(exc),parent=root)
        root.destroy()
        return 2
    def reveal():
        if splash.winfo_exists():
            splash.destroy()
        root.deiconify()
        root.lift()
    # Measured from when the splash actually appeared, not from process start
    # or a fixed post-init offset - a fast DesktopService init (e.g. cached
    # state) must not shorten the splash below MIN_SPLASH_MS, and a slow one
    # must not add MIN_SPLASH_MS on top of it.
    elapsed_ms = (time.monotonic() - splash_shown_at) * 1000
    remaining_ms = max(0, int(MIN_SPLASH_MS - elapsed_ms))
    root.after(remaining_ms, reveal)
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
