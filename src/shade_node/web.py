"""SHADE browser console (OSINT STATION). Phase 1: Inbox.

Runs on this PC only (127.0.0.1), at a private per-session address like
FIGHT's. All work goes through the same DesktopService the Tk window uses,
so scoring, review rules and safety gates are identical. Collection runs in
the background; the page polls a cheap status endpoint and only reloads the
list when SHADE's data actually changed.

  python -m shade_node.web --config C:\\Station\\SHADE\\config.toml --open-browser

Never transmits. Never marks anything SENT from this phase.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import threading
import time
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .db import ALLOWED_TRANSITIONS, queue_accepts, queue_prefilter
from .desktop import DesktopService
from .formatters import evidence_summary, workflow_status
from .inbox_sort import sorted_inbox
from .relevance import age, remaining
from .web_theme import CSS, logo_uri

LANES = {'Inbox': 'inbox', 'Top news': 'context', 'NOW': 'now', 'All evidence': 'all'}
CATEGORIES = ['top-news', 'cyber', 'infrastructure', 'communications', 'grid', 'fuel', 'commodity', 'transportation',
              'public-safety', 'public-health', 'civil-liberties', 'politics', 'space-weather', 'earthquake', 'weather',
              'fire-weather', 'disaster', 'chatter']
AREAS = ['ETN/WNC', 'REGIONAL', 'NATIONAL', 'GLOBAL', 'DISTANT']
# Excel-style column filters: query key -> how to read the column value from a listing row.
COLUMN_FILTERS = {'fa': lambda r: r['area'] or '—', 'ft': lambda r: (r['category'] or '').upper(),
                  'fc': lambda r: r['confidence_label'] or '—', 'fs': lambda r: r['status']}


def column_filters(rows, query):
    """Return (facets, filtered rows). Each column's value counts are taken with
    every *other* column filter applied, so the menu shows what picking a value
    would actually leave on screen."""
    chosen = {}
    for key in COLUMN_FILTERS:
        raw = (query.get(key) or [''])[0]
        values = {v for v in raw.split('\x1f') if v} if raw else set()
        if values:
            chosen[key] = values
    facets = {}
    for key, read in COLUMN_FILTERS.items():
        counts = {}
        for r in rows:
            if all(read2(r) in vals for k2, vals in chosen.items() if k2 != key
                   for read2 in (COLUMN_FILTERS[k2],)):
                value = read(r)
                counts[value] = counts.get(value, 0) + 1
        facets[key] = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if chosen:
        rows = [r for r in rows if all(COLUMN_FILTERS[k](r) in vals for k, vals in chosen.items())]
    return facets, rows


SORT_KEYS = {'id', 'age', 'area', 'category', 'score', 'confidence', 'families', 'status', 'remaining', 'title'}
# Phase 1 review actions; SENT/relay-override/format come with the Daily Wire phase.
ACTIONS = {'REVIEW': 'Start review', 'CORRELATING': 'Correlate', 'TX_CANDIDATE': 'TX candidate', 'REJECTED': 'Reject'}
BULK_TARGETS = {'CORRELATING', 'REVIEW', 'REJECTED', 'TX_CANDIDATE'}
PAGE_SIZES = (50, 100, 250, 500)
LISTING_TTL = 600


def utc_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def state_dir():
    base = os.environ.get('LOCALAPPDATA') or str(Path.home() / '.local' / 'state')
    return Path(base) / 'OSINT STATION' / 'SHADE'


class Console:
    """Everything the HTTP layer needs, testable without sockets."""

    def __init__(self, service: DesktopService):
        self.service = service
        self.lock = threading.Lock()
        self.cache = {}          # options -> (signature, monotonic, rows)
        self.compute_locks = {}
        self.collect_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='shade-web-collect')
        self.collecting = False
        self.last_collect = None
        self.previews = {}
        self.pending = {}

    # -- listing --------------------------------------------------------------
    @staticmethod
    def options(query: dict) -> dict:
        def one(name, default=''):
            return (query.get(name) or [default])[0]
        lane = one('lane', 'Inbox')
        category = one('category')
        area = one('area')
        state = one('state')
        minimum = one('min').strip()
        max_age = one('max_age').strip()
        opts = dict(mode=one('mode', 'STANDARD').lower(), lane=LANES.get(lane, 'inbox'),
                    category=category if category in CATEGORIES else None,
                    area=area if area in AREAS else None,
                    status=state if state in ALLOWED_TRANSITIONS else None,
                    include_suppressed=one('broad') == '1', search=one('q')[:200],
                    minimum=int(minimum) if minimum.isdigit() and int(minimum) <= 100 else None,
                    max_age=float(max_age) if max_age.replace('.', '', 1).isdigit() else None)
        if opts['mode'] not in {'standard', 'exercise', 'actual'}:
            opts['mode'] = 'standard'
        return opts

    def invalidate(self):
        self.cache.clear()

    def rows(self, opts: dict):
        key = json.dumps(opts, sort_keys=True)
        signature = self.service.data_signature()
        with self.lock:
            hit = self.cache.get(key)
            if hit and hit[0] == signature and time.monotonic() - hit[1] < LISTING_TTL:
                return hit[2], signature, True
            gate = self.compute_locks.setdefault(key, threading.Lock())
        with gate:   # identical concurrent requests share one computation
            with self.lock:
                hit = self.cache.get(key)
                if hit and hit[0] == signature and time.monotonic() - hit[1] < LISTING_TTL:
                    return hit[2], signature, True
            rows = self.service.listing(**opts)
            with self.lock:
                if len(self.cache) > 24:
                    self.cache.clear()
                self.cache[key] = (signature, time.monotonic(), rows)
            return rows, signature, False

    def inbox(self, query: dict) -> dict:
        opts = self.options(query)
        rows, signature, cached = self.rows(opts)
        facets, rows = column_filters(rows, query)
        sort = (query.get('sort') or [''])[0]
        desc = (query.get('desc') or ['0'])[0] == '1'
        if sort in SORT_KEYS:
            rows = sorted_inbox(rows, sort, desc)
        size = int((query.get('size') or ['100'])[0]) if (query.get('size') or ['100'])[0].isdigit() else 100
        size = size if size in PAGE_SIZES else 100
        pages = max(1, -(-len(rows) // size))
        page = min(max(1, int((query.get('page') or ['1'])[0]) if (query.get('page') or ['1'])[0].isdigit() else 1), pages)
        chunk = rows[(page - 1) * size: page * size]
        return {'total': len(rows), 'page': page, 'pages': pages, 'size': size, 'signature': list(signature),
                'cached': cached, 'facets': facets, 'rows': [self.row(r) for r in chunk]}

    @staticmethod
    def row(r: dict) -> dict:
        return {'id': r['id'], 'age': age(r['published']), 'area': r['area'], 'category': r['category'].upper(),
                'score': r['score'], 'confidence': r['confidence_label'], 'families': r['independent_families'],
                'state': workflow_status(r), 'status': r['status'], 'expired': bool(r['expired']),
                'validity': remaining(r['expires']) if r['expires'] else '—', 'title': r['title'],
                'location': r.get('location') or ''}

    # -- detail and actions ---------------------------------------------------
    def detail(self, claim_id: int, mode: str) -> dict:
        claim, observations = self.service.detail(claim_id, mode)
        text = '\n'.join(line for line in evidence_summary(claim, observations).splitlines()
                         if not line.startswith(('NEXT:', 'FORMAT:')))
        allowed = sorted(ALLOWED_TRANSITIONS.get(claim['status'], set()) & set(ACTIONS)) if not claim['expired'] else []
        seen, links = set(), []
        for o in observations:
            if o.get('url') and o['url'] not in seen:
                seen.add(o['url'])
                links.append({'url': o['url'], 'label': f"{o['source_name']} [{o['source_type']}]"})
        live = not claim['expired']
        extras = {'relay': claim['status'] == 'REVIEW' and live,
                  'format': claim['status'] in {'REVIEW', 'TX_CANDIDATE'} and live,
                  'record_sent': claim['status'] != 'MERGED',
                  'article': live}
        field = ''
        if not any(o.get('source_id') == 'commstat' or 'commstat' in (o.get('source_family') or '') for o in observations):
            try:
                from datetime import timedelta
                from .statrep_rollup import describe, field_matches, latest_reports
                reports = latest_reports(self.service.settings.database, datetime.now(timezone.utc) - timedelta(hours=24))
                field = describe(field_matches(reports, title=claim['title'], location=claim.get('location') or '',
                                               area=claim.get('area') or '', category=claim.get('category') or ''))
            except Exception:          # field reports are a hint; never block the detail view
                field = ''
        return {'extras': extras, 'field': field, 'id': claim['id'], 'title': claim['title'], 'status': claim['status'], 'state': workflow_status(claim),
                'confidence': claim['confidence_label'], 'area': claim['area'], 'score': claim['score'],
                'expired': bool(claim['expired']), 'summary': text, 'links': links[:25],
                'actions': [{'target': t, 'label': ACTIONS[t]} for t in ('REVIEW', 'CORRELATING', 'TX_CANDIDATE', 'REJECTED') if t in allowed]}

    def mark(self, body: dict) -> dict:
        mode = str(body.get('mode', 'standard')).lower()
        target = str(body.get('target', ''))
        ids = [int(x) for x in body.get('ids', [])][:500]
        if target not in ACTIONS:
            raise ValueError('That action is not available here.')
        if not ids:
            raise ValueError('Select at least one report.')
        if len(ids) > 1 and target not in BULK_TARGETS:
            raise ValueError('That action cannot be applied in bulk.')
        before = self.service.data_signature()
        if len(ids) == 1:
            if self.service.detail(ids[0], mode)[0]['status'] == target:
                return {'changed': 0, 'skipped': 1, 'target': target}
            self.service.mark(ids[0], target, mode)
            changed = 1
        else:
            changed = self.service.mark_many(ids, target, mode)
        self._patch_cache(ids, target, before, self.service.data_signature())
        return {'changed': changed, 'skipped': len(ids) - changed, 'target': target}

    def _patch_cache(self, ids, target, before, after):
        """Update cached views for the reports just changed instead of
        re-scoring everything. Only safe when nothing else changed between the
        two signatures (index 2 is the workflow-audit id); otherwise, or when a
        changed report might now belong to a view it wasn't in, that view is
        recomputed on its next request."""
        only_ours = all(a == b for i, (a, b) in enumerate(zip(before, after)) if i != 2)
        now = datetime.now(timezone.utc)
        fresh = {}
        with self.lock:
            entries = list(self.cache.items())
        for key, (signature, stamp, rows) in entries:
            opts = json.loads(key)
            if not only_ours or signature != before or opts.get('minimum') is not None:
                with self.lock:
                    self.cache.pop(key, None)
                continue
            present = {r['id']: i for i, r in enumerate(rows)}
            if any(i not in present and queue_prefilter(target, status=opts['status'],
                                                           include_suppressed=opts['include_suppressed'])
                   for i in ids):
                with self.lock:
                    self.cache.pop(key, None)        # a report may have entered this view
                continue
            patched = list(rows)
            for claim_id in ids:
                if claim_id not in present:
                    continue
                if (opts['mode'], claim_id) not in fresh:
                    fresh[(opts['mode'], claim_id)] = self.service.detail(claim_id, opts['mode'])[0]
                item = fresh[(opts['mode'], claim_id)]
                keep = queue_prefilter(target, status=opts['status'], include_suppressed=opts['include_suppressed']) and \
                    queue_accepts(item, now=now, lane=opts['lane'], category=opts['category'], area=opts['area'],
                                  status=opts['status'], include_suppressed=opts['include_suppressed'],
                                  max_age=opts['max_age'])
                if opts['search'] and opts['search'].casefold() not in (item['title'] + ' ' + item['location']).casefold():
                    keep = False
                patched[present[claim_id]] = item if keep else None
            with self.lock:
                if key in self.cache:
                    self.cache[key] = (after, stamp, [r for r in patched if r is not None])

    # -- phase 2: message prep, relay, record sent, merge, article ---------------
    def _single_claim(self, body):
        claim_id = int(body.get('id', 0))
        if claim_id <= 0:
            raise ValueError('Choose one report.')
        return claim_id, str(body.get('mode', 'standard')).lower()

    def relay(self, body):
        claim_id, mode = self._single_claim(body)
        if body.get('confirm') is not True:
            raise ValueError('Relay needs explicit confirmation.')
        claim, _ = self.service.detail(claim_id, mode)
        if claim['status'] != 'REVIEW':
            raise ValueError('Relay anyway is only available for a report in REVIEW.')
        before = self.service.data_signature()
        self.service.mark(claim_id, 'TX_CANDIDATE', mode, operator_override=True)
        self._patch_cache([claim_id], 'TX_CANDIDATE', before, self.service.data_signature())
        return {'changed': 1, 'target': 'TX_CANDIDATE', 'basis': 'operator_relay'}

    def format_message(self, body):
        claim_id, mode = self._single_claim(body)
        text = self.service.format(claim_id, mode, body.get('confirm_actual') is True)
        return {'text': text, 'mode': mode.upper(), 'note': 'Prepared text only. Copying or saving does not transmit.'}

    def record_sent(self, body):
        claim_id, _ = self._single_claim(body)
        if body.get('confirm') is not True:
            raise ValueError('Recording a transmission needs explicit confirmation.')
        text = str(body.get('text', '')).strip()
        if not text:
            raise ValueError('Paste the exact text you transmitted.')
        result = self.service.record_wire(claim_id, str(body.get('sent_at', '')).strip(),
                                          str(body.get('channel', '')).strip()[:40], text[:16384])
        self.invalidate()
        return {'recorded': True, 'result': result if isinstance(result, (int, str, bool, type(None))) else True}

    def merge(self, body):
        keep = int(body.get('keep', 0))
        ids = [int(x) for x in body.get('ids', [])][:200]
        if body.get('confirm') is not True:
            raise ValueError('Grouping needs explicit confirmation.')
        others = [x for x in dict.fromkeys(ids) if x != keep]
        if keep <= 0 or keep not in ids or not others:
            raise ValueError('Choose at least two reports and the one to keep.')
        count = self.service.merge_reports(keep, others, allow_low_overlap=body.get('override') is True)
        self.invalidate()
        return {'kept': keep, 'grouped': len(others), 'result': count if isinstance(count, int) else len(others)}

    def article_preview(self, body):
        url = str(body.get('url', '')).strip()
        if not url.startswith(('http://', 'https://')):
            raise ValueError('Paste the full article link (https://...).')
        article = self.service.preview_article(url)
        token = secrets.token_urlsafe(12)
        with self.lock:
            if len(self.previews) > 50:
                self.previews.clear()
            self.previews[token] = article
        return {'token': token, 'publisher': article.get('publisher', ''), 'title': article.get('title', ''),
                'published_at': article.get('published_at') or '', 'url': article.get('url', url)}

    def article_attach(self, body):
        claim_id, mode = self._single_claim(body)
        if body.get('confirm') is not True:
            raise ValueError('Attaching evidence needs explicit confirmation.')
        with self.lock:
            article = self.previews.pop(str(body.get('token', '')), None)
        if not article:   # only articles this console fetched and showed can be attached
            raise ValueError('Preview the article again before attaching it.')
        self.service.attach_article(claim_id, article, mode)
        self.invalidate()
        return {'attached': True}

    # -- phase 3: sources & health, housekeeping, purge --------------------------
    def sources(self, mode):
        out = []
        for source, active, poll in self.service.sources(mode):
            out.append({'id': source.id, 'name': source.name, 'active': bool(active), 'enabled': bool(source.enabled), 'mine': source.pack == 'my-feeds',
                        'emcomm_only': bool(source.emcomm_only), 'family': source.source_family,
                        'pack': source.pack, 'kind': source.kind,
                        'last_attempt': (poll or {}).get('last_attempt') or '',
                        'next_allowed': (poll or {}).get('next_allowed') or '',
                        'result': 'Not polled' if not poll else ('OK' if poll['success'] else 'Failed'),
                        'error': (poll or {}).get('error') or ''})
        counts = {'total': len(out), 'active': sum(r['active'] for r in out),
                  'failed': sum(r['active'] and r['result'] == 'Failed' for r in out),
                  'never': sum(r['active'] and r['result'] == 'Not polled' for r in out)}
        packs, on = self.service.source_packs()
        return {'counts': counts, 'sources': out, 'packs': [{'name': k, 'sources': v, 'on': k in on}
                                                             for k, v in sorted(packs.items())],
                'note': 'Switches save to config.local.toml and take effect on the next collection run '
                        '(the collector runs every 15 minutes). Status reflects the last attempted poll; it does not prove current coverage.'}

    def node_settings(self):
        from .fight_mount import default_fight_dir
        from .node_settings import NodeSettings
        return NodeSettings(getattr(getattr(self, 'fight', None), 'dir', None) or default_fight_dir(str(self.service.config)))

    def node(self):
        return self.node_settings().listing()

    def node_toggle(self, body):
        return self.node_settings().set(str(body.get('key', '')), bool(body.get('on')))

    def node_restart(self, body):
        return self.node_settings().restart()

    def feed_check(self, body):
        count, title = self.service.check_feed(str(body.get('url', '')))
        return {'items': count, 'newest': title}

    def feed_add(self, body):
        feed = self.service.add_feed(name=body.get('name', ''), url=body.get('url', ''), area=body.get('area', ''),
                                     category=body.get('category', 'top-news'),
                                     source_type=body.get('source_type', 'media'))
        self.invalidate()
        return feed

    def feed_remove(self, body):
        self.service.remove_feed(str(body.get('id', '')))
        self.invalidate()
        return {'removed': body.get('id')}

    def source_toggle(self, body):
        sid = str(body.get('id', ''))
        self.service.set_sources(source_id=sid, on=bool(body.get('on')))
        self.invalidate()
        return {'id': sid, 'on': bool(body.get('on'))}

    def source_packs(self, body):
        packs = body.get('packs')
        if not isinstance(packs, list) or not all(isinstance(p, str) for p in packs):
            raise ValueError('packs must be a list of names')
        self.service.set_sources(packs=packs)
        self.invalidate()
        return {'packs': sorted(packs)}

    def housekeeping_preview(self, body):
        mode = str(body.get('mode', 'standard')).lower()
        hours = float(body.get('hours', 72))
        _, changes = self.service.cleanup(mode, suppress=True)
        plan = self.service.purge_preview(hours)
        token = secrets.token_urlsafe(12)
        with self.lock:
            self.pending.clear()          # only the newest preview can be applied
            self.pending[token] = {'mode': mode, 'changes': changes, 'plan': plan, 'used': set()}
        return {'token': token, 'changes': changes[:300], 'changes_total': len(changes),
                'purge': {'hours': plan['hours'], 'cutoff': plan['cutoff'], 'eligible': len(plan['claim_ids']),
                          'protected': plan['protected_old'], 'total': plan['total_claims'],
                          'by_status': plan.get('by_status', {})}}

    def _take(self, body, what, part):
        if body.get('confirm') is not True:
            raise ValueError(f'{what} needs explicit confirmation.')
        with self.lock:
            preview = self.pending.get(str(body.get('token', '')))
            if not preview:
                raise ValueError('That preview is out of date. Preview again before applying.')
            if part in preview['used']:
                raise ValueError(f'{what} from this preview was already applied. Preview again.')
            preview['used'].add(part)
        return preview

    def housekeeping_apply(self, body):
        preview = self._take(body, 'Housekeeping', 'expire')
        if not preview['changes']:
            return {'backup': None, 'changed': 0, 'skipped': 0}
        backup, applied, skipped = self.service.cleanup_selected(preview['mode'], preview['changes'])
        self.invalidate()
        return {'backup': backup, 'changed': len(applied), 'skipped': skipped}

    def purge_apply(self, body):
        preview = self._take(body, 'Purge', 'purge')
        if not preview['plan']['claim_ids']:
            raise ValueError('Nothing to purge.')
        backup, deleted, journal = self.service.purge_apply(preview['plan'])
        self.invalidate()
        return {'backup': backup, 'deleted': deleted, 'journal': journal}

    def invalidate(self):
        with self.lock:
            self.cache.clear()

    # -- collection -----------------------------------------------------------
    def start_collect(self, mode: str) -> dict:
        with self.lock:
            if self.collecting:
                return {'started': False, 'collecting': True}
            self.collecting = True
        def task():
            started = utc_now()
            try:
                result = self.service.collect(mode)
                self.last_collect = {'ok': True, 'started': started, 'finished': utc_now(),
                                     'summary': (f"{result['sources_ok']} feeds ok, {result['sources_partial']} partial, "
                                                 f"{result['sources_failed']} failed; {result['inserted']} new observations; "
                                                 f"{result['sources_skipped_cooldown']} waiting for their interval."),
                                     'errors': result.get('errors', [])[:20]}
            except Exception as exc:
                self.last_collect = {'ok': False, 'started': started, 'finished': utc_now(), 'summary': str(exc), 'errors': []}
            finally:
                self.collecting = False
        self.collect_pool.submit(task)
        return {'started': True, 'collecting': True}

    def status(self) -> dict:
        return {'collecting': self.collecting, 'last_collect': self.last_collect,
                'signature': list(self.service.data_signature()), 'version': __version__, 'time': utc_now()}


# --------------------------------------------------------------------------- page
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SHADE · OSINT STATION</title>
<style>__CSS__</style></head><body>
<header class="osint-bar">__LOGO__<div class="osint-name"><strong>SHADE</strong>
<span>Source Harvesting, Attribution, Deduplication &amp; Export</span></div>
<div class="osint-suite"><span class="who">OSINT STATION · __CALL__</span>
<label class="inline">Mode <select id="mode"><option>STANDARD</option><option>EXERCISE</option><option>ACTUAL</option></select></label>
<button id="quit" title="Stop the SHADE web console">Quit</button></div></header>
<nav class="tabs"><a class="on" href="#inbox" data-tab="inbox">Inbox</a><a href="#sources" data-tab="sources">Sources &amp; health</a>
<a href="#node" data-tab="node">NODE</a><a href="#housekeeping" data-tab="housekeeping">Housekeeping</a><a href="__BASE__fight">FIGHT</a></nav>
<main id="tab-inbox">
<div class="bar">
 <button id="collect" class="primary">Collect now</button><button id="refresh">Refresh</button>
 <select id="lane"><option>Inbox</option><option>Top news</option><option>NOW</option><option>All evidence</option></select>
 <select id="category"><option value="">All categories</option>__CATS__</select>
 <select id="area"><option value="">All areas</option>__AREAS__</select>
 <select id="state"><option value="">Active states</option>__STATES__</select>
 <label class="inline"><input type="checkbox" id="broad"> Suppressed &amp; routine</label>
 <input id="q" type="search" placeholder="Search title or location" size="24">
 <input id="min" placeholder="Min score" size="7"><input id="max_age" placeholder="Age ≤ h" size="7">
 <span class="grow"></span><button id="colclear" hidden title="Remove all column filters">Clear column filters</button><span id="count" class="muted"></span>
</div>
<div class="bar" id="bulkbar" hidden><span id="bulkcount"></span>
 <button data-bulk="REVIEW">Start review</button><button data-bulk="CORRELATING">Correlate</button>
 <button data-bulk="TX_CANDIDATE">TX candidate</button><button data-bulk="REJECTED" class="danger">Reject</button>
 <button id="mergebtn">Group as same event…</button>
 <button id="clearsel">Clear selection</button></div>
<div class="split">
 <section class="panel" id="listpanel"><table><thead><tr>
  <th class="nosort"><input type="checkbox" id="all" title="Select page"></th>
  <th data-k="id">ID</th><th data-k="title">Report</th><th data-k="age">Age</th><th data-k="area"><span class="lbl">Area</span><button class="colf" data-f="fa" title="Filter Area">⏷</button></th><th data-k="category"><span class="lbl">Type</span><button class="colf" data-f="ft" title="Filter Type">⏷</button></th>
  <th data-k="score">Sig</th><th data-k="confidence"><span class="lbl">Confidence</span><button class="colf" data-f="fc" title="Filter Confidence">⏷</button></th><th data-k="families">Src</th>
  <th data-k="status"><span class="lbl">State</span><button class="colf" data-f="fs" title="Filter State">⏷</button></th><th data-k="remaining">Validity</th></tr></thead>
  <tbody id="rows"><tr><td colspan="11" class="empty"><span class="spin"></span> Reading local evidence…</td></tr></tbody></table>
  <div class="pager"><button id="prev">‹ Prev</button><span id="pageinfo" class="muted"></span><button id="next">Next ›</button>
  <span class="grow"></span><label class="inline">Rows <select id="size"><option>50</option><option selected>100</option><option>250</option><option>500</option></select></label></div>
 </section>
 <section class="panel detail" id="detail"><p class="muted">Select a report to see its evidence and review actions.</p></section>
</div></main>
<main id="tab-sources" hidden>
 <div class="bar"><button id="src-refresh">Refresh</button><label class="inline"><input type="checkbox" id="src-problems"> Problems only</label>
 <span class="grow"></span><span id="src-counts" class="muted"></span></div>
 <p class="muted" id="src-note"></p>
 <section class="panel" style="padding:10px 14px;margin-bottom:12px"><b>Add your own RSS feed</b> <span class="muted">· paste the feed's https:// address; SHADE checks it before saving</span>
  <div class="bar" style="flex-wrap:wrap;gap:8px;margin-top:8px"><input id="feed-name" placeholder="Name (e.g. WBIR News)" size="22">
  <input id="feed-url" placeholder="https://example.com/feed.xml" size="42">
  <select id="feed-area" title="Area"><option value="">Area: auto</option><option>ETN/WNC</option><option>REGIONAL</option><option>NATIONAL</option></select>
  <select id="feed-cat" title="Category"><option>top-news</option><option>regional</option><option>public-safety</option><option>weather</option><option>cyber</option><option>chatter</option><option>general</option></select>
  <select id="feed-type" title="Who publishes it"><option value="media">News outlet</option><option value="official">Official / government</option><option value="community">Community</option></select>
  <button id="feed-test">Test</button><button id="feed-add" class="primary">Add feed</button><span id="feed-msg" class="muted"></span></div></section>
 <section class="panel" style="padding:10px 14px;margin-bottom:12px"><b>Source packs</b> <span class="muted">· groups of built-in sources; tick to include</span>
  <div id="src-packs" class="packs"></div><button id="src-packs-save" class="primary" hidden>Save packs</button></section>
 <section class="panel"><table><thead><tr><th class="nosort">On</th><th class="nosort">Source</th><th class="nosort">State</th><th class="nosort">Origin</th><th class="nosort">Pack</th>
 <th class="nosort">Last poll</th><th class="nosort">Result</th><th class="nosort">Next allowed</th></tr></thead><tbody id="src-rows"></tbody></table></section>
</main>
<main id="tab-node" hidden>
 <div class="bar"><button id="node-refresh">Refresh</button><button id="node-restart" class="primary">Restart NODE</button>
 <span class="grow"></span><span id="node-counts" class="muted"></span></div>
 <p class="muted" id="node-note"></p>
 <section class="panel"><table><thead><tr><th class="nosort">On</th><th class="nosort">Feed</th><th class="nosort">Type</th>
 <th class="nosort">Reads from</th></tr></thead><tbody id="node-rows"></tbody></table></section>
</main>
<main id="tab-housekeeping" hidden>
 <section class="panel detail"><h2>Housekeeping</h2>
 <p class="muted">Preview first; nothing changes until you apply. Both actions write a verified full backup of the database first.</p>
 <div class="bar"><label class="inline">Keep reports seen in the last <input id="hk-hours" value="72" size="4"> hours (1–72)</label>
 <button id="hk-preview" class="primary">Preview</button><span id="hk-busy" class="muted"></span></div>
 <div id="hk-out"></div></section>
</main>
<div class="status"><span id="stat">Ready</span><span id="fresh"></span><span class="grow"></span>
<span class="muted">Click: open · Ctrl-click: add/remove · Shift-click: range · Ctrl+A: page · Esc: clear</span><span class="muted">All transmission decisions remain human · SHADE __VER__</span></div>
<script>
const API="__API__";const $=s=>document.querySelector(s);
const S={f:{fa:new Set(),ft:new Set(),fc:new Set(),fs:new Set()},facets:{},sort:"",desc:false,page:1,sel:new Set(),current:null,anchor:null,sig:null,rows:[],busy:false};
function q(){const p=new URLSearchParams();for(const id of["mode","lane","category","area","state","q","min","max_age","size"])p.set(id,$("#"+id).value);
 p.set("broad",$("#broad").checked?"1":"0");for(const[k,v]of Object.entries(S.f))if(v.size)p.set(k,[...v].join("\x1f"));p.set("sort",S.sort);p.set("desc",S.desc?"1":"0");p.set("page",S.page);return p}
async function api(path,body){const r=await fetch(API+path,body?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{});
 const j=await r.json().catch(()=>({error:"Bad response"}));if(!r.ok)throw new Error(j.error||r.statusText);return j}
function toast(msg,err){const t=document.createElement("div");t.className="toast"+(err?" err":"");t.textContent=msg;document.body.append(t);setTimeout(()=>t.remove(),err?9000:4500)}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function tag(r){const c=r.status==="TX_CANDIDATE"?"tx":r.status==="NEW"?"new":r.expired?"warn":"";return `<span class="tag ${c}">${esc(r.state)}</span>`}
async function load(keepPage){if(!keepPage)S.page=1;$("#stat").innerHTML='<span class="spin"></span> Reading local evidence…';
 try{const d=await api("inbox?"+q());S.sig=JSON.stringify(d.signature);S.rows=d.rows;S.page=d.page;S.facets=d.facets||{};
  document.querySelectorAll(".colf").forEach(b=>{const n=S.f[b.dataset.f].size;b.classList.toggle("on",n>0);b.textContent=n?`⏷${n}`:"⏷"});
  $("#colclear").hidden=!Object.values(S.f).some(v=>v.size);
  $("#count").textContent=`${d.total} reports`;$("#pageinfo").textContent=`Page ${d.page} of ${d.pages}`;
  $("#prev").disabled=d.page<=1;$("#next").disabled=d.page>=d.pages;
  $("#rows").innerHTML=d.rows.length?d.rows.map(r=>`<tr class="row${S.sel.has(r.id)?" sel":""}" data-id="${r.id}">
   <td><input type="checkbox" class="pick" ${S.sel.has(r.id)?"checked":""}></td><td class="num">${r.id}</td>
   <td class="title">${esc(r.title)}${r.location?` <span class="muted">· ${esc(r.location)}</span>`:""}</td>
   <td class="nowrap">${esc(r.age)}</td><td class="nowrap">${esc(r.area)}</td><td class="nowrap">${esc(r.category)}</td>
   <td class="num">${r.score}</td><td class="nowrap">${esc(r.confidence)}</td>
   <td class="num">${r.families}</td><td class="nowrap">${tag(r)}</td><td class="nowrap">${esc(r.validity)}</td></tr>`).join("")
   :'<tr><td colspan="11" class="empty">No reports match this view. Try NOW, or All evidence + Suppressed &amp; routine.</td></tr>';
  $("#fresh").textContent="";$("#stat").textContent="View refreshed "+new Date().toLocaleTimeString();bulk()}
 catch(e){$("#stat").textContent="Could not read evidence";toast(e.message,true)}}
function bulk(){$("#bulkbar").hidden=S.sel.size<2;$("#all").checked=S.rows.length>0&&S.rows.every(r=>S.sel.has(r.id));$("#bulkcount").textContent=`${S.sel.size} selected`}
async function show(id){S.current=id;S.detail=null;document.querySelectorAll("tr.row").forEach(tr=>tr.classList.toggle("sel",+tr.dataset.id===id||S.sel.has(+tr.dataset.id)));
 $("#detail").innerHTML='<p class="muted"><span class="spin"></span> Loading report and attribution…</p>';
 try{const d=await api(`claim/${id}?mode=${$("#mode").value}`);if(S.current!==id)return;
  S.detail=d;$("#detail").innerHTML=`<h2>${esc(d.title)}</h2><div>${tag(d)} <span class="tag">${esc(d.confidence)}</span> <span class="tag">${esc(d.area)}</span> <span class="tag">Sig ${d.score}</span></div>
   ${d.field?`<div class="field">${esc(d.field)}</div>`:""}
   <div class="links">${d.links.map(l=>`<a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.label)}</a>`).join("")}</div>
   <div class="actions">${d.actions.map(a=>`<button data-act="${a.target}" class="${a.target==="REJECTED"?"danger":""}">${esc(a.label)}</button>`).join("")||'<span class="muted">No review actions for this state.</span>'}</div>
   <div class="actions secondary">${d.extras.relay?'<button data-x="relay">Relay anyway…</button>':""}${d.extras.format?'<button data-x="format">Prepare message</button>':""}${d.extras.record_sent?'<button data-x="sent">Record as sent…</button>':""}${d.extras.article?'<button data-x="article">Add article…</button>':""}</div>
   <pre>${esc(d.summary)}</pre>`}
 catch(e){$("#detail").innerHTML=`<p class="muted">${esc(e.message)}</p>`}}
async function mark(ids,target){if(target==="REJECTED"&&!confirm(`Reject ${ids.length} report(s)?`))return;
 try{const r=await api("mark",{ids,target,mode:$("#mode").value});toast(`${r.changed} report(s) → ${target}`+(r.skipped?` · ${r.skipped} already were`:""));S.sel.clear();await load(true);if(ids.length===1)show(ids[0])}
 catch(e){toast(e.message,true)}}
function paint(){document.querySelectorAll("tr.row").forEach(tr=>{const on=S.sel.has(+tr.dataset.id);
 tr.classList.toggle("sel",on||+tr.dataset.id===S.current);const box=tr.querySelector(".pick");if(box)box.checked=on});bulk()}
function rangeTo(id){const ids=S.rows.map(r=>r.id);const a=ids.indexOf(S.anchor),b=ids.indexOf(id);
 if(a<0||b<0){S.sel.add(id);return}const[lo,hi]=a<b?[a,b]:[b,a];for(let i=lo;i<=hi;i++)S.sel.add(ids[i])}
$("#rows").addEventListener("mousedown",e=>{if(e.shiftKey)e.preventDefault()});  // no text highlight on shift-click
$("#rows").addEventListener("click",e=>{const tr=e.target.closest("tr.row");if(!tr)return;const id=+tr.dataset.id;
 const box=e.target.classList.contains("pick");
 if(e.shiftKey&&S.anchor!=null){if(S.current!=null&&!S.sel.size)S.sel.add(S.current);rangeTo(id);paint();return}
 if(e.ctrlKey||e.metaKey||box){if(box&&!e.target.checked)S.sel.delete(id);else if(!box&&S.sel.has(id))S.sel.delete(id);else S.sel.add(id);
  S.anchor=id;paint();return}
 S.anchor=id;show(id)});
$("#all").addEventListener("change",e=>{for(const r of S.rows)e.target.checked?S.sel.add(r.id):S.sel.delete(r.id);paint()});
document.addEventListener("keydown",e=>{if(e.key==="Escape"&&S.sel.size){S.sel.clear();paint()}
 if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="a"&&!/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)){e.preventDefault();S.rows.forEach(r=>S.sel.add(r.id));paint()}});
$("#detail").addEventListener("click",e=>{const b=e.target.closest("button[data-act]");if(b&&S.current)mark([S.current],b.dataset.act)});
document.querySelectorAll("[data-bulk]").forEach(b=>b.addEventListener("click",()=>mark([...S.sel],b.dataset.bulk)));
$("#clearsel").onclick=()=>{S.sel.clear();paint()};
const lbl=h=>h.querySelector(".lbl")||h;
document.querySelectorAll("th[data-k]").forEach(th=>th.addEventListener("click",e=>{if(e.target.closest(".colf"))return;const k=th.dataset.k;
 S.desc=S.sort===k?!S.desc:["age","score","confidence","families","id"].includes(k);S.sort=k;
 document.querySelectorAll("th[data-k]").forEach(h=>lbl(h).textContent=lbl(h).textContent.replace(/ [▲▼]$/,""));lbl(th).textContent+=S.desc?" ▼":" ▲";load(true)}));
function closePop(){const p=$("#colpop");if(p)p.remove()}
document.querySelectorAll(".colf").forEach(b=>b.addEventListener("click",e=>{e.stopPropagation();const open=$("#colpop");closePop();if(open&&open.dataset.f===b.dataset.f)return;
 const k=b.dataset.f,picked=new Set(S.f[k]),items=S.facets[k]||[];const pop=document.createElement("div");pop.id="colpop";pop.dataset.f=k;
 pop.innerHTML=`<div class="muted" style="margin-bottom:6px">Show only…</div><div class="colvals">${items.map(([v,n])=>`<label><input type="checkbox" value="${esc(v)}" ${picked.has(v)?"checked":""}> ${esc(v)} <span class="muted">${n}</span></label>`).join("")||'<span class="muted">No values</span>'}</div>
  <div class="colbtns"><button class="primary" data-c="apply">Apply</button><button data-c="clear">Clear</button></div>`;
 document.body.append(pop);const r=b.getBoundingClientRect();pop.style.left=Math.min(r.left,innerWidth-260)+scrollX+"px";pop.style.top=r.bottom+scrollY+4+"px";
 pop.addEventListener("click",ev=>ev.stopPropagation());
 pop.querySelector('[data-c="apply"]').onclick=()=>{S.f[k]=new Set([...pop.querySelectorAll("input:checked")].map(i=>i.value));closePop();load()};
 pop.querySelector('[data-c="clear"]').onclick=()=>{S.f[k]=new Set();closePop();load()}}));
document.addEventListener("click",closePop);
$("#colclear").onclick=()=>{for(const k in S.f)S.f[k]=new Set();load()};
for(const id of["lane","category","area","state","broad","size","mode"])$("#"+id).addEventListener("change",()=>load());
for(const id of["q","min","max_age"])$("#"+id).addEventListener("keydown",e=>{if(e.key==="Enter")load()});
$("#refresh").onclick=()=>load(true);$("#prev").onclick=()=>{S.page--;load(true)};$("#next").onclick=()=>{S.page++;load(true)};

$("#collect").onclick=async()=>{try{await api("collect",{mode:$("#mode").value});poll()}catch(e){toast(e.message,true)}};
$("#quit").onclick=async()=>{if(!confirm("Stop the SHADE web console? The scheduled collector keeps running."))return;
 try{await api("quit",{})}catch(e){}document.body.innerHTML='<main><h1 style="color:var(--accent)">SHADE web console stopped.</h1><p class="muted">You can close this tab.</p></main>'};
let lastCollect=null;
async function poll(){try{const s=await api("status");const b=$("#collect");b.disabled=s.collecting;b.innerHTML=s.collecting?'<span class="spin"></span> Collecting…':"Collect now";
  if(s.last_collect&&JSON.stringify(s.last_collect)!==lastCollect){if(lastCollect!==null)toast((s.last_collect.ok?"Collection complete: ":"Collection failed: ")+s.last_collect.summary,!s.last_collect.ok);lastCollect=JSON.stringify(s.last_collect)}
  if(S.sig&&JSON.stringify(s.signature)!==S.sig){$("#fresh").innerHTML='<span class="pill" id="newdata">New data · refresh</span>';$("#newdata").onclick=()=>load(true)}}
 catch(e){$("#stat").textContent="Console not responding"}finally{setTimeout(poll,document.hidden?60000:(lastCollect&&$("#collect").disabled?3000:15000))}}

function modal(title,body,buttons){const bg=document.createElement("div");bg.className="modal-bg";
 bg.innerHTML=`<div class="modal" role="dialog" aria-modal="true"><h3>${esc(title)}</h3>${body}<div class="row"><span class="grow"></span></div></div>`;
 const row=bg.querySelector(".row");const close=()=>{bg.remove();document.removeEventListener("keydown",k)};
 const k=e=>{if(e.key==="Escape")close()};document.addEventListener("keydown",k);bg.addEventListener("click",e=>{if(e.target===bg)close()});
 for(const[label,fn,cls]of buttons){const b=document.createElement("button");b.textContent=label;if(cls)b.className=cls;b.onclick=()=>fn(close,bg);row.append(b)}
 document.body.append(bg);const f=bg.querySelector("input,textarea,select");if(f)f.focus();return bg}
const M=()=>$("#mode").value;
async function after(id){S.sel.clear();await load(true);if(id)show(id)}
$("#detail").addEventListener("click",async e=>{const b=e.target.closest("button[data-x]");if(!b||!S.detail)return;const d=S.detail,x=b.dataset.x;
 if(x==="relay"){if(!confirm("Relay anyway?\n\nThis deliberately bypasses the official/media evidence gate. The operator relay basis will be audited. Continue?"))return;
  try{await api("relay",{id:d.id,mode:M(),confirm:true});toast("Recorded operator relay → TX candidate");after(d.id)}catch(err){toast(err.message,true)}}
 if(x==="format"){let ca=false;if(M()==="ACTUAL"){ca=confirm("Confirm ACTUAL formatting\n\nI have verified the situation, operator authority and source evidence.\n\nPrepare ACTUAL copy for manual use?");if(!ca)return}
  try{const r=await api("format",{id:d.id,mode:M(),confirm_actual:ca});
   modal(`Report ${d.id} · ${r.mode} · message preview`,`<p class="note">${esc(r.note)}</p><textarea readonly>${esc(r.text)}</textarea>`,
    [["Copy message",async()=>{try{await navigator.clipboard.writeText(r.text);toast("Copied. Nothing transmitted.")}catch(_){toast("Copy failed; select the text and press Ctrl+C",true)}}],
     ["Save text…",()=>{const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([r.text+"\n"],{type:"text/plain"}));a.download=`shade-report-${d.id}.txt`;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),2000)}],
     ["Close",c=>c()]])}catch(err){toast(err.message,true)}}
 if(x==="sent"){const now=new Date();const off=-now.getTimezoneOffset(),sg=off>=0?"+":"-",pad=n=>String(Math.floor(Math.abs(n))).padStart(2,"0");
  const local=new Date(now-now.getTimezoneOffset()*60000).toISOString().slice(0,19)+sg+pad(off/60)+":"+pad(off%60);
  modal(`Record actual wire transmission · report ${d.id}`,`<p class="note">Enter only a message you actually transmitted. This will mark the report SENT.</p>
   <label>Actual time with timezone<input id="m-at" value="${local}"></label><label>Channel<input id="m-ch" value="JS8"></label>
   <label>Exact transmitted text<textarea id="m-tx"></textarea></label>`,
   [["Cancel",c=>c()],["Record transmitted message",async(c,bg)=>{const at=bg.querySelector("#m-at").value,ch=bg.querySelector("#m-ch").value,tx=bg.querySelector("#m-tx").value.trim();
    if(!tx){toast("Paste the exact text you transmitted.",true);return}
    if(!confirm(`Confirm you already sent this text on ${ch} at ${at}?`))return;
    try{await api("record_sent",{id:d.id,mode:M(),sent_at:at,channel:ch,text:tx,confirm:true});c();toast("Transmission recorded · report SENT");after(d.id)}catch(err){toast(err.message,true)}},"primary"]])}
 if(x==="article"){let preview=null;
  const bg=modal(`Add article to report ${d.id}`,`<p class="muted">Paste the original news article link you opened and checked.</p><label>Article link<input id="m-url" placeholder="https://"></label><pre id="m-prev">The article headline and publisher will appear here before you attach it.</pre>`,
   [["Preview article",async(c,bg)=>{preview=null;const pre=bg.querySelector("#m-prev");pre.textContent="Opening article preview…";
     try{preview=await api("article_preview",{url:bg.querySelector("#m-url").value});
      const words=t=>new Set(t.toLowerCase().match(/[a-z0-9]{3,}/g)||[]);const A=words(d.title),B=words(preview.title);const inter=[...A].filter(w=>B.has(w)).length;
      preview.overlap=inter/Math.max(1,new Set([...A,...B]).size);
      pre.textContent=`Publisher: ${preview.publisher}\nHeadline: ${preview.title}\nPublished: ${preview.published_at||"not supplied by page"}\nURL: ${preview.url}\n`+(preview.overlap<0.2?"Low headline overlap: verify event, place, and date carefully.":"Compare event, place, and date before attaching.")}
     catch(err){pre.textContent=err.message}}],
    ["Cancel",c=>c()],
    ["Attach reviewed article",async c=>{if(!preview){toast("Preview the article first.",true);return}
     if(preview.overlap<0.2&&!confirm("The article headline shares few words with the report. Did you verify the same event, place, and date?"))return;
     if(!confirm(`Confirm this article describes the same event and is reliable reporting.\n\n${preview.publisher}: ${preview.title}\n\nAttach it as media evidence?`))return;
     try{await api("article_attach",{id:d.id,mode:M(),token:preview.token,confirm:true});c();toast("Article attached as media evidence");after(d.id)}catch(err){toast(err.message,true)}},"primary"]])}});
$("#mergebtn").onclick=()=>{const ids=[...S.sel];if(ids.length<2)return;
 const byId=Object.fromEntries(S.rows.map(r=>[r.id,r.title]));
 modal(`Group ${ids.length} reports`,`<p>These headlines must describe the same event. Choose the report to keep.</p>
  <label>Keep report<select id="m-keep">${ids.map(i=>`<option value="${i}">${i} · ${esc((byId[i]||"").slice(0,80))}</option>`).join("")}</select></label>
  <p class="muted">All source evidence stays attached to the group. Other reports are hidden from the inbox.</p>`,
  [["Cancel",c=>c()],["Group selected reports",async(c,bg)=>{const keep=+bg.querySelector("#m-keep").value;
   if(!confirm(`Group ${ids.length} reports under report ${keep}? Verify they describe the same event.`))return;
   const go=async override=>{const r=await api("merge",{keep,ids,confirm:true,override});c();toast(`Grouped ${r.grouped} report(s) under ${keep}`+(override?" (operator override recorded)":""));after(keep)};
   try{await go(false)}catch(err){
    if(/too different/.test(err.message)&&confirm(`${err.message}\n\nThe headlines use different words. If you have checked they describe the same event, group them anyway? This is recorded as an operator override.`)){
     try{await go(true)}catch(e2){toast(e2.message,true)}}else toast(err.message,true)}},"primary"]])};
let lastMode="STANDARD";
$("#mode").addEventListener("change",e=>{if(e.target.value==="ACTUAL"&&!confirm("Enter ACTUAL mode?\n\nUse ACTUAL only for an intentional operator-directed workflow. This does not declare an emergency or transmit.")){e.target.value=lastMode;return}lastMode=e.target.value},true);

function tab(name){for(const t of["inbox","sources","node","housekeeping"])$("#tab-"+t).hidden=t!==name;
 document.querySelectorAll(".tabs a[data-tab]").forEach(a=>a.classList.toggle("on",a.dataset.tab===name));
 if(name==="sources")loadSources();if(name==="node")loadNode();}
async function loadNode(){$("#node-rows").innerHTML='<tr><td colspan="4" class="empty"><span class="spin"></span> Reading NODE settings…</td></tr>';
 let d;try{d=await api("node")}catch(e){$("#node-rows").innerHTML=`<tr><td colspan="4" class="empty">${esc(e.message)}</td></tr>`;return}
 $("#node-note").textContent=d.note+" File: "+d.path;
 $("#node-counts").textContent=`${d.rows.filter(r=>r.on).length} on of ${d.rows.length}`;
 $("#node-rows").innerHTML=d.rows.map(r=>`<tr><td><label class="switch" title="${r.on?"On - click to turn off":"Off - click to turn on"}"><input type="checkbox" class="nodesw" data-key="${esc(r.key)}" ${r.on?"checked":""}><span></span></label></td>
  <td>${esc(r.name)}</td><td>${esc(r.section)}</td><td class="muted">${esc(r.detail)}</td></tr>`).join("")||'<tr><td colspan="4" class="empty">No NODE feeds configured.</td></tr>';
 document.querySelectorAll(".nodesw").forEach(c=>c.onchange=async()=>{c.disabled=true;
  try{await api("node_toggle",{key:c.dataset.key,on:c.checked});toast(`${c.checked?"Turned on":"Turned off"}: ${c.dataset.key}. Restart NODE to apply.`);
   $("#node-counts").textContent=`${document.querySelectorAll(".nodesw:checked").length} on of ${d.rows.length}`}
  catch(e){c.checked=!c.checked;toast(e.message,true)}finally{c.disabled=false}})}
$("#node-refresh").onclick=loadNode;
$("#node-restart").onclick=async()=>{if(!confirm("Restart NODE now? Collection pauses for a few seconds; nothing is transmitted."))return;
 const b=$("#node-restart");b.disabled=true;b.textContent="Restarting…";
 try{await api("node_restart",{});toast("NODE restarted with the new settings.")}catch(e){toast(e.message,true)}finally{b.disabled=false;b.textContent="Restart NODE"}};
document.querySelectorAll(".tabs a[data-tab]").forEach(a=>a.addEventListener("click",e=>{e.preventDefault();history.replaceState(null,"","#"+a.dataset.tab);tab(a.dataset.tab)}));
let SRC=[];
async function loadSources(){$("#src-rows").innerHTML='<tr><td colspan="8" class="empty"><span class="spin"></span> Reading source health…</td></tr>';
 try{const d=await api("sources?mode="+M());SRC=d.sources;$("#src-note").textContent=d.note;
  $("#src-counts").textContent=`${d.counts.active} active of ${d.counts.total} · ${d.counts.failed} failed · ${d.counts.never} never polled`;
  $("#src-packs").innerHTML=d.packs.map(p=>`<label class="inline"><input type="checkbox" class="pack" value="${esc(p.name)}" ${p.on?"checked":""}> ${esc(p.name)} <span class="muted">${p.sources}</span></label>`).join("");
  $("#src-packs-save").hidden=true;document.querySelectorAll("#src-packs .pack").forEach(c=>c.onchange=()=>{$("#src-packs-save").hidden=false});drawSources()}
 catch(e){toast(e.message,true)}}
function drawSources(){const only=$("#src-problems").checked;const rows=SRC.filter(r=>!only||(r.active&&r.result!=="OK"));
 $("#src-rows").innerHTML=rows.map(r=>`<tr><td><label class="switch" title="${r.enabled?"On - click to turn off":"Off - click to turn on"}"><input type="checkbox" class="srcsw" data-id="${esc(r.id)}" ${r.enabled?"checked":""}><span></span></label></td><td>${esc(r.name)}${r.mine?` <button class="feed-rm" data-id="${esc(r.id)}" title="Remove this feed">Remove</button>`:""}<div class="muted">${esc(r.kind)}${r.mine?" · my feed":""}</div>${r.error?`<div style="color:var(--warning)">${esc(r.error)}</div>`:""}</td>
  <td>${r.active?'<span class="tag tx">Active</span>':r.enabled&&r.emcomm_only?'<span class="tag">EmComm only</span>':'<span class="tag">Off</span>'}</td><td>${esc(r.family)}</td><td>${esc(r.pack)}</td>
  <td class="nowrap">${esc(r.last_attempt||"Never")}</td><td>${r.result==="OK"?'<span class="tag tx">OK</span>':r.result==="Failed"?'<span class="tag warn">Failed</span>':'<span class="tag">Not polled</span>'}</td>
  <td class="nowrap">${esc(r.next_allowed)}</td></tr>`).join("")||'<tr><td colspan="8" class="empty">No sources match.</td></tr>';
 document.querySelectorAll(".srcsw").forEach(c=>c.onchange=async()=>{c.disabled=true;
  try{await api("source_toggle",{id:c.dataset.id,on:c.checked});const r=SRC.find(x=>x.id===c.dataset.id);if(r){r.enabled=c.checked}
   toast(`${c.checked?"Turned on":"Turned off"}: ${r?r.name:c.dataset.id}. Takes effect on the next collection.`)}
  catch(e){c.checked=!c.checked;toast(e.message,true)}finally{c.disabled=false}})}
function feedBody(){return{name:$("#feed-name").value,url:$("#feed-url").value,area:$("#feed-area").value,category:$("#feed-cat").value,source_type:$("#feed-type").value}}
$("#feed-test").onclick=async()=>{$("#feed-msg").textContent="Checking…";
 try{const d=await api("feed_check",{url:$("#feed-url").value});$("#feed-msg").textContent=d.items?`OK: ${d.items} items. Newest: ${d.newest}`:"Answered, but no feed items found."}
 catch(e){$("#feed-msg").textContent="";toast(e.message,true)}};
$("#feed-add").onclick=async()=>{$("#feed-msg").textContent="Checking and saving…";
 try{const f=await api("feed_add",feedBody());$("#feed-msg").textContent="";$("#feed-name").value=$("#feed-url").value="";
  toast(`Added ${f.name}. It is collected on the next run.`);loadSources()}catch(e){$("#feed-msg").textContent="";toast(e.message,true)}};
$("#src-rows").addEventListener("click",async e=>{const b=e.target.closest(".feed-rm");if(!b)return;
 if(!confirm("Remove this feed? Reports already collected from it stay in SHADE."))return;
 try{await api("feed_remove",{id:b.dataset.id});toast("Feed removed.");loadSources()}catch(err){toast(err.message,true)}});
$("#src-packs-save").onclick=async()=>{const packs=[...document.querySelectorAll("#src-packs .pack:checked")].map(c=>c.value);
 try{await api("source_packs",{packs});toast("Source packs saved. Takes effect on the next collection.");loadSources()}catch(e){toast(e.message,true)}};
$("#src-refresh").onclick=loadSources;$("#src-problems").onchange=drawSources;
$("#hk-preview").onclick=async()=>{const h=+$("#hk-hours").value;if(!(h>=1&&h<=72)){toast("Keep between 1 and 72 hours.",true);return}
 $("#hk-busy").innerHTML='<span class="spin"></span> Previewing… this reads every active report and can take a minute.';$("#hk-out").innerHTML="";$("#hk-preview").disabled=true;
 try{const d=await api("housekeeping_preview",{mode:M(),hours:h});const p=d.purge;
  $("#hk-out").innerHTML=`<p>Two separate clean-ups. <b>Expire &amp; suppress</b> marks warnings that have ended as EXPIRED and routine newsletters as SUPPRESSED; nothing is deleted. <b>Purge</b> permanently deletes every report older than the hours you keep (by its Age, or by when SHADE first collected it): NEW, suppressed, REVIEW, REJECTED, EXPIRED alike. Purged items never come back as new, even if a feed still carries them. Most of the time just use <b>Run both</b>.</p>
   ${(d.changes_total||p.eligible)?`<button id="hk-both" class="primary">Run both</button>`:'<p class="muted">Nothing to clean up.</p>'}
   <h3 style="margin-top:22px">Purge · keep the last ${p.hours} h</h3>
   <p>${p.eligible} of ${p.total} reports are older than ${esc(p.cutoff)} (Age or first collected) and can be permanently deleted with their evidence${Object.keys(p.by_status||{}).length?": "+Object.entries(p.by_status).map(([k,v])=>`${v} ${esc(k)}`).join(", "):""}.</p>
   <p class="muted">${p.protected} older reports are always kept: TX_CANDIDATE, SENT, wire-recorded, or merged with one of those.</p>
   ${p.eligible?`<button id="hk-purge" class="danger">Purge ${p.eligible} reports…</button>`:""}
   <h3 style="margin-top:22px">Expiration / routine suppression · ${d.changes_total} report(s)</h3>
   ${d.changes_total?`<button id="hk-apply">Apply housekeeping (${d.changes_total} reports)</button>
   <pre style="max-height:300px;overflow:auto">${esc(d.changes.map(c=>`Report ${c.id} → ${c.status}: ${c.reason}`).join("\n"))}${d.changes_total>d.changes.length?`\n… and ${d.changes_total-d.changes.length} more`:""}</pre>`:'<p class="muted">No expiration or suppression changes needed.</p>'}`;
  const done=r=>{toast(r);$("#hk-out").innerHTML="";load(true)};
  const both=async()=>{if(!confirm(`Run both clean-ups?\n\n• Expire/suppress ${d.changes_total} report(s) (nothing deleted)\n• Permanently delete ${p.eligible} report(s) older than ${p.hours} h (TX_CANDIDATE / SENT / wire-recorded kept)\n\nA verified backup is written first.`))return;
   $("#hk-busy").innerHTML='<span class="spin"></span> Backing up, cleaning and compacting…';const out=[];
   try{if(d.changes_total){const r=await api("housekeeping_apply",{token:d.token,confirm:true});out.push(`${r.changed} expired/suppressed`+(r.skipped?` (${r.skipped} had already changed)`:""))}
    if(p.eligible){const r=await api("purge_apply",{token:d.token,confirm:true});out.push(`${r.deleted.claims} purged`)}
    done("Clean-up complete: "+out.join(" · "))}catch(e){toast(e.message,true)}finally{$("#hk-busy").textContent=""}};
  if($("#hk-both"))$("#hk-both").onclick=both;
  if($("#hk-apply"))$("#hk-apply").onclick=async()=>{if(!confirm(`Apply housekeeping to ${d.changes_total} reports? A backup is written first; no evidence is deleted.`))return;
   try{const r=await api("housekeeping_apply",{token:d.token,confirm:true});toast(`${r.changed} reports updated`+(r.skipped?` · ${r.skipped} had already changed since the preview`:"")+`. Backup: ${r.backup}`);$("#hk-apply").disabled=true;load(true)}catch(e){toast(e.message,true)}};
  if($("#hk-purge"))$("#hk-purge").onclick=async()=>{if(!confirm(`Permanently delete ${p.eligible} reports older than ${p.hours} hours?\n\n${p.protected} older TX_CANDIDATE / SENT / wire-recorded reports are kept.\nA verified backup is written first. This cannot be undone from inside SHADE.`))return;
   $("#hk-busy").innerHTML='<span class="spin"></span> Backing up, purging and compacting…';
   try{const r=await api("purge_apply",{token:d.token,confirm:true});done(`${r.deleted.claims} reports and ${r.deleted.observations} observations deleted.\nBackup: ${r.backup}\nJournal: ${r.journal}`)}catch(e){toast(e.message,true)}finally{$("#hk-busy").textContent=""}}}
 catch(e){toast(e.message,true)}finally{$("#hk-preview").disabled=false;if(!$("#hk-busy").textContent.startsWith("Backing"))$("#hk-busy").textContent=""}};
tab((location.hash||"#inbox").slice(1)in{inbox:1,sources:1,node:1,housekeeping:1}?(location.hash||"#inbox").slice(1):"inbox");
load();poll();
</script></body></html>"""


def render_page(api_prefix: str, nonce: str, callsign: str) -> str:
    logo = logo_uri()
    opts = lambda values: ''.join(f'<option>{v}</option>' for v in values)
    return (PAGE.replace('__CSS__', CSS).replace('__API__', api_prefix)
            .replace('__LOGO__', f'<img src="{logo}" alt="">' if logo else '')
            .replace('__CATS__', opts(CATEGORIES)).replace('__AREAS__', opts(AREAS))
            .replace('__STATES__', opts(list(ALLOWED_TRANSITIONS)))
            .replace('__CALL__', callsign).replace('__VER__', __version__)
            .replace('__BASE__', api_prefix[:-len('api/')])
            .replace('<script>', f'<script nonce="{nonce}">'))


FIGHT_NAV = ('<style>.osint-tabs{display:flex;gap:2px;padding:0 24px;background:#182631;border-bottom:1px solid #2c4256}'
             '.osint-tabs a{padding:9px 16px;color:#9bafc2;text-decoration:none;border-radius:6px 6px 0 0;font:400 14px/1.45 '
             "'Segoe UI',system-ui,sans-serif}.osint-tabs a.on{background:#192735;color:#e7edf4}</style>"
             '<nav class="osint-tabs"><a href="__B__#inbox">Inbox</a><a href="__B__#sources">Sources &amp; health</a>'
             '<a href="__B__#node">NODE</a><a href="__B__#housekeeping">Housekeeping</a><a class="on" href="__B__fight">FIGHT</a>'
             '<button id="shade-quit" type="button" title="Stop the SHADE web console" style="margin-left:auto;'
             'align-self:center">Quit</button></nav>'
             '<script>document.getElementById("shade-quit").onclick=async()=>{'
             'if(!confirm("Stop the SHADE web console? The scheduled collector keeps running."))return;'
             'try{await fetch("__B__api/quit",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"})}catch(e){}'
             'document.body.innerHTML=\'<main style="padding:40px"><h1>SHADE web console stopped.</h1>'
             '<p>You can close this tab.</p></main>\'};</script>')


def fight_page(page: str, base: str, nonce: str, theme=None) -> str:
    """FIGHT's own HTML, routed under SHADE's private address with SHADE's tabs."""
    page = page.replace('__ACTION__', base + 'fight/action').replace('/s/__TOKEN__', base + 'fight').replace('__TOKEN__', '')
    if theme:
        page = theme(page)
    nav = FIGHT_NAV.replace('__B__', base)
    page = page.replace('</header>', '</header>' + nav, 1) if '</header>' in page else nav + page
    return page.replace('<script>', f'<script nonce="{nonce}">')


# --------------------------------------------------------------------------- http
def make_server(console: Console, *, port: int = 0, token: str | None = None):
    token = token or secrets.token_urlsafe(24)
    base = f'/s/{token}/'
    callsign = getattr(getattr(console.service.settings, 'operator', None), 'callsign', '') or \
        getattr(console.service.settings, 'callsign', '') or 'KQ4ODY'

    class Handler(BaseHTTPRequestHandler):
        server_version = 'SHADE-web'

        def log_message(self, *args):
            pass

        def _host_ok(self):
            host = (self.headers.get('Host') or '').split(':')[0]
            return host in {'127.0.0.1', 'localhost'}

        def _send(self, status, body, ctype='application/json', nonce=None):
            data = body if isinstance(body, bytes) else body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', f'{ctype}; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('Content-Security-Policy',
                             f"default-src 'none'; script-src 'nonce-{nonce or 'x'}'; style-src 'unsafe-inline'; "
                             "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status, payload):
            self._send(status, json.dumps(payload))

        def _fight(self, produce):
            mount = getattr(console, 'fight', None)
            nonce = secrets.token_urlsafe(16)
            session = mount.load() if mount else None
            theme = getattr(mount, 'theme', None)
            if session is None:
                reason = getattr(mount, 'error', None) or 'FIGHT is not configured for this console.'
                page = ('<!doctype html><html><head><meta charset="utf-8"><title>FIGHT</title></head><body><main>'
                        '<h1>FIGHT is unavailable</h1><p>' + html.escape(reason) +
                        '</p><p>SHADE keeps working. The standalone FIGHT-PREVIEW.bat still works.</p></main></body></html>')
                return self._send(200, fight_page(page, base, nonce, theme), 'text/html', nonce)
            try:
                page = produce(session)
                status = 200
            except getattr(mount, 'errors', ()) or (ValueError,) as exc:
                text = html.escape(str(exc))
                page = (f'<main><h1>FIGHT refused action</h1><p>{text}</p>'
                        f'<p>Nothing was sent. <a href="{base}fight">Start over</a>.</p></main>')
                status = 400
            self._send(status, fight_page(page, base, nonce, theme), 'text/html', nonce)

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {'error': 'loopback host check'})
            parts = urlsplit(self.path)
            if parts.path in {base, base.rstrip('/')}:
                nonce = secrets.token_urlsafe(16)
                return self._send(200, render_page(base + 'api/', nonce, callsign), 'text/html', nonce)
            if parts.path == base + 'fight':
                return self._fight(lambda session: session.home_page())
            if not parts.path.startswith(base + 'api/'):
                return self._json(404, {'error': 'not found'})
            route = parts.path[len(base + 'api/'):]
            query = parse_qs(parts.query)
            try:
                if route == 'inbox':
                    return self._json(200, console.inbox(query))
                if route.startswith('claim/') and route[6:].isdigit():
                    mode = (query.get('mode') or ['STANDARD'])[0].lower()
                    return self._json(200, console.detail(int(route[6:]), mode))
                if route == 'status':
                    return self._json(200, console.status())
                if route == 'sources':
                    return self._json(200, console.sources((query.get('mode') or ['standard'])[0].lower()))
                if route == 'node':
                    return self._json(200, console.node())
                if route == 'ping':
                    return self._json(200, {'ok': True})
            except (ValueError, KeyError) as exc:
                return self._json(400, {'error': str(exc)})
            except Exception as exc:
                return self._json(500, {'error': f'{type(exc).__name__}: {exc}'})
            return self._json(404, {'error': 'not found'})

        def do_POST(self):
            if not self._host_ok():
                return self._json(403, {'error': 'loopback host check'})
            origin = self.headers.get('Origin')
            expected = f"http://{self.headers.get('Host')}"
            if origin and origin != expected:
                return self._json(403, {'error': 'same-origin check'})
            path = urlsplit(self.path).path
            if path != base + 'fight/action' and (self.headers.get('Content-Type') or '').split(';')[0] != 'application/json':
                return self._json(415, {'error': 'JSON required'})
            if path == base + 'fight/action':
                if not origin:
                    referer = urlsplit(self.headers.get('Referer') or '')
                    if f'{referer.scheme}://{referer.netloc}' != expected or not referer.path.startswith(base):
                        return self._send(403, '<h1>Forbidden: same-origin check</h1>', 'text/html')
                if (self.headers.get('Content-Type') or '').split(';')[0] != 'application/x-www-form-urlencoded':
                    return self._send(415, '<h1>Form required</h1>', 'text/html')
                length = int(self.headers.get('Content-Length') or 0)
                from .fight_mount import FightMount
                try:
                    fields = FightMount.parse_form(self.rfile.read(min(length, 300_000)))
                except (ValueError, UnicodeError) as exc:
                    return self._fight(lambda session: (_ for _ in ()).throw(ValueError(str(exc))))
                return self._fight(lambda session: session.dispatch(fields))
            if not path.startswith(base + 'api/'):
                return self._json(404, {'error': 'not found'})
            length = int(self.headers.get('Content-Length') or 0)
            if length > 100_000:
                return self._json(413, {'error': 'too large'})
            try:
                body = json.loads(self.rfile.read(length) or b'{}')
                route = path[len(base + 'api/'):]
                if route == 'mark':
                    return self._json(200, console.mark(body))
                handlers = {'relay': console.relay, 'format': console.format_message, 'record_sent': console.record_sent,
                            'merge': console.merge, 'article_preview': console.article_preview,
                            'article_attach': console.article_attach, 'housekeeping_preview': console.housekeeping_preview,
                            'housekeeping_apply': console.housekeeping_apply, 'purge_apply': console.purge_apply,
                            'source_toggle': console.source_toggle, 'source_packs': console.source_packs,
                            'node_toggle': console.node_toggle, 'feed_check': console.feed_check,
                            'feed_add': console.feed_add, 'feed_remove': console.feed_remove, 'node_restart': console.node_restart}
                if route in handlers:
                    return self._json(200, handlers[route](body))
                if route == 'collect':
                    return self._json(200, console.start_collect(str(body.get('mode', 'standard')).lower()))
                if route == 'quit':
                    self._json(200, {'stopping': True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
            except (ValueError, KeyError, TypeError) as exc:
                return self._json(400, {'error': str(exc)})
            except Exception as exc:
                return self._json(500, {'error': f'{type(exc).__name__}: {exc}'})
            return self._json(404, {'error': 'not found'})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    url = f'http://127.0.0.1:{server.server_address[1]}{base}'
    return server, url


def _existing_url():
    path = state_dir() / 'web-url.txt'
    try:
        url = path.read_text(encoding='utf-8').strip()
        with urllib.request.urlopen(url + 'api/ping', timeout=2) as response:
            if response.status == 200:
                return url
    except Exception:
        pass
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description='SHADE browser console (local only)')
    parser.add_argument('--config', default=str(Path.cwd() / 'config.toml'))
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--open-browser', action='store_true')
    args = parser.parse_args(argv)
    running = _existing_url()
    if running:   # one console per PC: just reopen it
        if args.open_browser:
            webbrowser.open(running, new=1)
        print(running)
        return 0
    console = Console(DesktopService(args.config))
    from .fight_mount import FightMount
    console.fight = FightMount(console.service, args.config)
    server, url = make_server(console, port=args.port)
    # Warm the default Inbox while the browser opens, so the first view is fast.
    threading.Thread(target=lambda: console.rows(Console.options({})), daemon=True).start()
    folder = state_dir(); folder.mkdir(parents=True, exist_ok=True)
    (folder / 'web-url.txt').write_text(url, encoding='utf-8')
    print(f'SHADE web console: {url}', flush=True)
    if args.open_browser:
        webbrowser.open(url, new=1)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            (folder / 'web-url.txt').unlink()
        except OSError:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
