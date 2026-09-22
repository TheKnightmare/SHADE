"""SHADE desktop console. Tk stays on the UI thread; work runs in one worker."""
from concurrent.futures import ThreadPoolExecutor
from importlib import resources
from pathlib import Path
import argparse
import queue as messages
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import webbrowser

from . import __version__
from .desktop import DesktopService
from .db import ALLOWED_TRANSITIONS
from .formatters import evidence_summary
from .relevance import age, remaining

BG = '#101923'
PANEL = '#192735'
TEXT = '#e7edf4'
MUTED = '#9bafc2'
ACCENT = '#59d8ce'


class ShadeWindow:
    def __init__(self, root, service):
        self.root, self.service = root, service
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='shade-desktop')
        self.results = messages.Queue()
        self.busy = False
        self.claim = None
        self.observations = []
        self.rows = []
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
        try:
            r.iconbitmap(str(resources.files('shade_node').joinpath('assets/shade.ico')))
        except tk.TclError:
            pass
        style = ttk.Style(r)
        style.theme_use('clam')
        style.configure('.', background=BG, foreground=TEXT, font=('Segoe UI', 10))
        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG)
        style.configure('Muted.TLabel', foreground=MUTED)
        style.configure('Title.TLabel', font=('Segoe UI Semibold', 27), foreground=ACCENT)
        style.configure('TButton', background=PANEL, foreground=TEXT, padding=(12, 8), borderwidth=0)
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
        main = ttk.Frame(r, padding=20)
        main.pack(fill='both', expand=True)
        header = ttk.Frame(main)
        header.pack(fill='x')
        ttk.Label(header, text='SHADE', style='Title.TLabel').pack(side='left')
        ttk.Label(header, text='  BE FREE // FIGHT IN THE SHADE', style='Muted.TLabel').pack(side='left', padx=10)
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
            (self.category, ['All categories','top-news','cyber','infrastructure','communications','grid','fuel','transportation','public-safety','public-health','space-weather','earthquake','weather','disaster','chatter'], 18),
            (self.area, ['All areas','ETN/WNC','REGIONAL','NATIONAL','GLOBAL','DISTANT'], 13),
            (self.state, ['Active states']+list(ALLOWED_TRANSITIONS), 15),
        ]:
            box = self._combo(filters, variable, values, width)
            box.pack(side='left', padx=(0, 8))
            box.bind('<<ComboboxSelected>>', lambda event: self.refresh())
        broad = ttk.Checkbutton(filters, text='Include suppressed', variable=self.broad, command=self.refresh)
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
        self.tree = ttk.Treeview(listing, columns=columns, show='headings', selectmode='browse', height=10)
        for key, label, width in zip(columns, ['ID','Age','Area','Type','Sig','Conf','Src','State','Validity','Report'], [48,48,90,125,42,48,38,98,90,420]):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=35, stretch=key=='title')
        ys = ttk.Scrollbar(listing, orient='vertical', command=self.tree.yview)
        xs = ttk.Scrollbar(listing, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
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
        for label, target in [('Start review','REVIEW'),('Correlate','CORRELATING'),('TX candidate','TX_CANDIDATE'),('Reject','REJECTED'),('Record as sent…','SENT')]:
            button = self._button(actions, label, lambda t=target: self.mark(t))
            button.pack(side='left', padx=(0, 6))
            self.action_buttons[target] = button
        self.format_button = self._button(actions, 'Prepare message…', self.format_message)
        self.format_button.pack(side='right')
        self.link_button = self._button(actions, 'Source links…', self.source_links)
        self.link_button.pack(side='right', padx=8)
        self.details = self._text(lower, height=11)
        self._set_text(self.details, 'Select a report to inspect its sources, relevance and review state.\nID is the claim ID. Collection runs only when you click Collect now.')
        footer = ttk.Frame(main)
        footer.pack(fill='x', pady=(10, 0), side='bottom', before=splitter)
        self.progress = ttk.Progressbar(footer, mode='indeterminate', length=100)
        self.progress.pack(side='right')
        ttk.Label(footer, textvariable=self.status, style='Muted.TLabel', wraplength=1050).pack(side='left')

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
        allowed = ALLOWED_TRANSITIONS.get(self.claim['status'], set()) if self.claim and not self.claim['expired'] else set()
        for target, button in self.action_buttons.items():
            button.configure(state='normal' if not self.busy and target in allowed else 'disabled')
        self.format_button.configure(state='normal' if not self.busy and self.claim and self.claim['status'] in {'REVIEW','TX_CANDIDATE'} and not self.claim['expired'] else 'disabled')
        self.link_button.configure(state='normal' if not self.busy and self.observations else 'disabled')

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
            done, result, error = self.results.get_nowait()
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
        self.work('Reading local evidence…', lambda: self.service.listing(**options), self.render)

    def render(self, rows):
        selected = self.tree.selection()
        previous = selected[0] if selected else None
        self.tree.delete(*self.tree.get_children())
        self.rows = rows
        self.claim = None
        self.observations = []
        for row in rows:
            self.tree.insert('', 'end', iid=str(row['id']), values=(row['id'],age(row['published']),row['area'],row['category'].upper(),row['score'],row['confidence_score'],row['independent_families'],row['status'],remaining(row['expires']) if row['expires'] else '—',row['title']))
        self.count.set(f'{len(rows)} reports')
        self._set_text(self.details, 'Select a report above.' if rows else 'No reports match this view.\nTry NOW for local life-safety weather, or All evidence + Include suppressed for retained reports.\nExpired reports require selecting EXPIRED in the state filter.')
        self._enabled()
        if previous and self.tree.exists(previous):
            self.tree.selection_set(previous)
            self.tree.see(previous)
        self.status.set('View refreshed • F5 refreshes local data; Collect now polls public sources.')

    def select(self, event=None):
        selected = self.tree.selection()
        if self.busy or not selected:
            return
        self.claim = None
        self.observations = []
        claim_id = int(selected[0])
        mode = self.mode.get().lower()
        self.work('Loading report and attribution…', lambda: self.service.detail(claim_id, mode), self.show_detail)

    def show_detail(self, result):
        self.claim, self.observations = result
        selected = self.tree.selection()
        if not selected or int(selected[0]) != self.claim['id']:
            self.claim = None
            self.observations = []
            self._enabled()
            self.select()
            return
        self._set_text(self.details, '\n'.join(line for line in evidence_summary(self.claim,self.observations).splitlines() if not line.startswith(('NEXT:', 'FORMAT:'))))
        self._enabled()
        self.status.set(f"Report {self.claim['id']} • {self.claim['confidence_label']} • {self.claim['status']}")

    def change_mode(self, event=None):
        chosen = self.mode.get()
        if chosen == 'ACTUAL' and not messagebox.askyesno('Enter ACTUAL mode?', 'Use ACTUAL only for an intentional operator-directed workflow.\n\nThis does not declare an emergency or transmit. Enter ACTUAL mode?', parent=self.root):
            self.mode.set(self.last_mode)
            return
        self.last_mode = chosen
        self.banner.configure(text=f'{chosen} • '+('Exercise copy is marked at both ends.' if chosen=='EXERCISE' else 'Every ACTUAL message requires confirmation.' if chosen=='ACTUAL' else 'Operator review console • All transmission decisions remain human.'))
        self.refresh()

    def collect(self):
        mode = self.mode.get().lower()
        self.work('Collecting public feeds… you can keep this window open.', lambda: self.service.collect(mode), self.collected)

    def collected(self, result):
        summary = (f"{result['sources_ok']} feeds succeeded; {result['sources_failed']} failed.\n"
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
                lines.append('')
            self._set_text(text,'\n'.join(lines))
        self.work('Reading source health…',lambda:self.service.sources(mode),display)

    def source_links(self):
        window = self.dialog('Original source links', '900x450')
        text = self._text(window)
        self._set_text(text,'Click a source URL to open it in your browser.\n\n')
        text.configure(state='normal')
        seen = set()
        for index,row in enumerate(self.observations):
            url = row['url']
            if (row['source_family'],url) in seen:
                continue
            seen.add((row['source_family'],url))
            text.insert('end',f"{row['source_name']} [{row['source_family']}]\n")
            tag = f'link{index}'
            text.insert('end',url+'\n\n',tag)
            text.tag_configure(tag,foreground=ACCENT,underline=True)
            if url.startswith('https://'):
                text.tag_bind(tag,'<Button-1>',lambda event,u=url:webbrowser.open(u))
        text.configure(state='disabled')

    def cleanup(self):
        mode = self.mode.get().lower()
        def preview(result):
            _, changes = result
            window = self.dialog('Housekeeping preview • no evidence will be deleted')
            text = self._text(window)
            self._set_text(text,'\n'.join(f"Report {c['id']} → {c['status']}: {c['reason']}" for c in changes) or 'Nothing needs housekeeping.')
            def apply():
                window.destroy()
                self.work('Backing up and applying housekeeping…',lambda:self.service.cleanup(mode,apply=True,suppress=True,expected=changes),applied)
            def applied(result):
                backup, changed = result
                messagebox.showinfo('Housekeeping complete',f'{len(changed)} reports updated.\nEvidence preserved.\nBackup: {backup}',parent=self.root)
                self.refresh()
            if changes:
                ttk.Button(window,text=f'Apply housekeeping ({len(changes)} reports)',command=apply).pack(anchor='e',pady=10)
        self.work('Previewing housekeeping…',lambda:self.service.cleanup(mode,suppress=True),preview)

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
            '3. Select a report to read its full evidence and score. Source links open the original sources.\n\n'
            '4. Start review → Prepare message → TX candidate. Copy/save are manual exports only. Record as sent is an after-the-fact annotation.\n\n'
            '5. All evidence + Include suppressed reveals retained low-priority reports. Select EXPIRED, REJECTED or SENT to inspect those states.\n\n'
            '6. Housekeeping previews expiration/suppression, then requires Apply. It backs up the database and never deletes evidence.\n\n'
            '7. The app opens in STANDARD. EXERCISE marks both ends. ACTUAL requires an explicit mode choice and confirmation for each formatted message.\n\n'
            'F5 refreshes local evidence. The view refreshes once a minute while idle; collection is never automatically scheduled by this window.\n\n'
            'Close and reopen safely at any time after the current operation finishes. Configuration and evidence stay local.')

    def _tick(self):
        if not self.busy and len(self.root.winfo_children()) <= 1:
            self.refresh()
        self.root.after(60000,self._tick)

    def close(self):
        if self.busy:
            messagebox.showinfo('Operation in progress','Please let the current operation finish before closing.',parent=self.root)
            return
        self.pool.shutdown(wait=False)
        self.root.destroy()


def main(argv=None):
    arguments = argparse.ArgumentParser(description='SHADE desktop operator console')
    arguments.add_argument('--config', default=str(Path.cwd()/'config.toml'))
    args = arguments.parse_args(argv)
    root = tk.Tk()
    root.withdraw()
    path = args.config
    if not Path(path).is_file():
        path = filedialog.askopenfilename(parent=root,title='Choose your existing SHADE config.toml',filetypes=[('TOML configuration','*.toml')])
        if not path:
            root.destroy()
            return 0
    try:
        service = DesktopService(path)
        ShadeWindow(root,service)
    except Exception as exc:
        messagebox.showerror('SHADE could not open this station',str(exc),parent=root)
        root.destroy()
        return 2
    root.deiconify()
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
