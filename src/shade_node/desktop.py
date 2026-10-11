"""Desktop service boundary: every operation opens its own database connection."""
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from .db import (connect, queue, claim_detail, transition, housekeeping, backup_database, ALLOWED_TRANSITIONS,
                  merge_claims, record_wire_transmission, daily_wire, set_wire_annotation, reorder_wire)
from .engine import load_settings, run_once, normalize_mode, active_sources
from .formatters import traffic_message, bulletin_messages
from .operator_sources import preview_article, attach_article


class DesktopService:
    def __init__(self, config):
        self.config = str(Path(config).resolve())
        self.settings = load_settings(self.config)

    def policy(self, mode):
        result = dict(self.settings.policy)
        if normalize_mode(mode) != 'standard':
            result['minimum_score'] = self.settings.emcomm_min_queue_score
        return result

    def listing(self, mode='standard', lane='inbox', category=None, area=None,
                status=None, include_suppressed=False, search='', max_age=None, minimum=None):
        policy = self.policy(mode)
        if minimum is not None:
            policy['minimum_score'] = minimum
        with connect(self.settings.database) as db:
            rows = queue(db, minimum, 10000, lane=lane, policy=policy, category=category,
                         area=area, status=status, max_age=max_age, include_suppressed=include_suppressed)
        return [row for row in rows if search.casefold() in (row['title']+' '+row['location']).casefold()]

    def data_signature(self):
        """Cheap fingerprint of everything that can change the Inbox view
        apart from the clock. Used by the idle probe to skip full rescoring
        when nothing changed."""
        with connect(self.settings.database) as db:
            return tuple(db.execute(
                'SELECT (SELECT MAX(id) FROM observations), (SELECT MAX(id) FROM observation_revisions), '
                '(SELECT MAX(id) FROM workflow_audit), (SELECT COUNT(*) FROM claims), '
                '(SELECT COUNT(*) FROM claim_merges), (SELECT COUNT(*) FROM wire_transmissions), '
                '(SELECT MAX(updated_at) FROM wire_annotations), (SELECT COUNT(*) FROM event_supersessions), '
                "(SELECT MAX(asof) FROM source_presence)").fetchone())

    def detail(self, claim_id, mode='standard'):
        with connect(self.settings.database) as db:
            return claim_detail(db, claim_id, self.policy(mode))

    def mark(self, claim_id, target, mode='standard', *, operator_override=False):
        with connect(self.settings.database) as db:
            claim, _ = claim_detail(db, claim_id, self.policy(mode))
            if claim['expired']:
                raise ValueError('This report has expired and cannot advance through review.')
            transition(db, claim_id, target, operator_override=operator_override)

    def mark_many(self, claim_ids, target, mode='standard', *, operator_override=False):
        # TX_CANDIDATE was previously excluded from bulk edit by design (see
        # git history / RELEASE_NOTES) - explicitly reversed per an operator
        # request to allow marking/unmarking TX_CANDIDATE from any screen,
        # including bulk edit. The actual safety gate is unchanged: transition()
        # still independently re-checks corroboration (or requires the explicit
        # operator_override) for EVERY claim, one at a time, inside this same
        # loop - bulk here means "fewer clicks," not "less verification." SENT
        # remains excluded from bulk on purpose: it is a one-at-a-time, audited
        # record of a real transmission having already happened, not a filter
        # a batch of claims should ever move through together.
        ids = list(dict.fromkeys(int(value) for value in claim_ids))
        if not ids:
            raise ValueError('Select at least one report.')
        if target not in {'CORRELATING', 'REVIEW', 'REJECTED', 'TX_CANDIDATE'}:
            raise ValueError('Bulk changes are limited to review, correlation, rejection, and TX_CANDIDATE.')
        with connect(self.settings.database) as db:
            # Reports already in the target state are left as they are rather
            # than failing the whole batch (operator request 2026-10-09).
            ids = [i for i in ids if (db.execute('SELECT status FROM claims WHERE id=?', (i,)).fetchone() or {'status': None})['status'] != target]
            for claim_id in ids:
                claim, _ = claim_detail(db, claim_id, self.policy(mode))
                if claim['expired']:
                    raise ValueError(f'Report {claim_id} expired. No reports were changed.')
                if target not in ALLOWED_TRANSITIONS.get(claim['status'], set()):
                    raise ValueError(f'Report {claim_id} cannot change from {claim["status"]} to {target}. No reports were changed.')
                if target == 'TX_CANDIDATE' and not operator_override:
                    evidence = db.execute('SELECT source_type FROM observations WHERE claim_id=?', (claim_id,)).fetchall()
                    if not any(row['source_type'] in {'official', 'media'} for row in evidence):
                        raise ValueError(f'Report {claim_id} has no official/media corroboration; use the explicit '
                                          f'relay override for this one claim instead of bulk. No reports were changed.')
            for claim_id in ids:
                transition(db, claim_id, target, operator_override=operator_override and target == 'TX_CANDIDATE')
        return len(ids)

    def daily_wire(self, mode='standard', include_hidden=False):
        """SHADE's own review/assembly view over the current TX_CANDIDATE
        queue - reuses queue() (status='TX_CANDIDATE') via db.daily_wire(),
        the same optimized bulk-prefetch path as listing(). Never changes
        claims.status; never touches FIGHT or StationWatch."""
        with connect(self.settings.database) as db:
            return daily_wire(db, self.policy(mode), include_hidden=include_hidden)

    def wire_annotate(self, claim_id, *, position=None, note=None, hidden=None):
        """Daily Wire display curation only (position/note/hidden) - stored
        separately from claim workflow state, never calls transition()."""
        with connect(self.settings.database) as db:
            set_wire_annotation(db, claim_id, position=position, note=note, hidden=hidden)

    def wire_reorder(self, ordered_claim_ids):
        with connect(self.settings.database) as db:
            reorder_wire(db, ordered_claim_ids)

    def wire_export(self, claim_ids, mode='standard'):
        """Renders the given Daily Wire claims, in the given order, as one
        bulletin - reusing the existing bulletin_messages()/claim_detail()
        formatter and evidence model exactly as the Inbox already does for a
        reviewed batch. This is a text preview/export only: it never calls
        transition(), never marks anything SENT, and never invokes FIGHT -
        FIGHT's own staging is a separate, unrelated code path in StationWatch."""
        policy = self.policy(mode)
        with connect(self.settings.database) as db:
            items = [claim_detail(db, claim_id, policy) for claim_id in claim_ids]
        return bulletin_messages(items, callsign=self.settings.callsign, network=self.settings.network,
                                  max_chars=self.settings.max_message_chars, mode=mode,
                                  region_label=self.settings.region_label)

    def merge_reports(self, primary_id, other_ids, *, allow_low_overlap=False):
        with connect(self.settings.database) as db:
            return merge_claims(db, int(primary_id), other_ids, allow_low_overlap=allow_low_overlap)

    def cleanup_selected(self, mode, changes):
        """Apply only the previewed expiration/suppression changes that still
        hold now. Time passes and the collector runs between preview and apply;
        anything that changed is skipped (and reported) instead of failing."""
        wanted = {(int(c['id']), c['status']) for c in changes}
        backup = backup_database(self.settings.database)
        with connect(self.settings.database) as db:
            if not db.in_transaction:
                db.execute('BEGIN IMMEDIATE')
            applied = housekeeping(db, self.policy(mode), apply=True, suppress=True,
                                   now=datetime.now(timezone.utc), only=wanted)
        return backup, applied, len(wanted) - len(applied)

    def record_wire(self, claim_id, sent_at, channel, message_text):
        with connect(self.settings.database) as db:
            return record_wire_transmission(db, claim_id, sent_at, channel, message_text)

    def preview_article(self, url):
        return preview_article(url)

    def attach_article(self, claim_id, article, mode='standard'):
        with connect(self.settings.database) as db:
            claim, _ = claim_detail(db, claim_id, self.policy(mode))
            if claim['expired']:
                raise ValueError('Report expired; article was not attached.')
            return attach_article(db, claim_id, article)

    def format(self, claim_id, mode='standard', confirm_actual=False):
        mode = normalize_mode(mode)
        if mode == 'actual' and not confirm_actual:
            raise ValueError('ACTUAL formatting requires explicit confirmation.')
        if self.settings.callsign.strip().upper() in {'', 'SET-ME', 'CHANGE-ME'}:
            raise ValueError('Set your operator callsign in the station configuration first.')
        claim, observations = self.detail(claim_id, mode)
        if claim['expired'] or claim['status'] not in {'REVIEW', 'TX_CANDIDATE'}:
            raise ValueError('Review a current report before preparing a message.')
        emergency = mode != 'standard'
        return traffic_message(claim, observations, callsign=self.settings.callsign,
                               network=self.settings.emcomm_network if emergency else self.settings.network,
                               region_label=self.settings.emcomm_region_label if emergency else self.settings.region_label,
                               max_chars=self.settings.emcomm_max_message_chars if emergency else self.settings.max_message_chars,
                               mode=mode)

    def collect(self, mode='standard'):
        return run_once(self.settings, normalize_mode(mode))

    def cleanup(self, mode='standard', apply=False, suppress=False, expected=None):
        backup = backup_database(self.settings.database) if apply else None
        with connect(self.settings.database) as db:
            now = datetime.now(timezone.utc)
            if apply:
                if not db.in_transaction:
                    db.execute('BEGIN IMMEDIATE')
                current = housekeeping(db, self.policy(mode), suppress=suppress, now=now)
                if expected is None or current != expected:
                    raise ValueError('Housekeeping preview changed. Please open a new preview before applying.')
            changes = housekeeping(db, self.policy(mode), apply=apply, suppress=suppress, now=now)
        return backup, changes

    def purge_preview(self, hours=72):
        from .db import purge_plan
        with connect(self.settings.database) as db:
            return purge_plan(db, hours)

    def purge_apply(self, plan):
        from .db import purge_plan, purge, compact
        backup = backup_database(self.settings.database)
        with connect(self.settings.database) as db:
            current = purge_plan(db, plan['hours'])
            # Only purge what the operator saw in the preview; claims that became
            # eligible since then wait for the next run.
            plan = dict(plan, claim_ids=[i for i in plan['claim_ids'] if i in set(current['claim_ids'])])
            result = purge(db, plan)
        try:
            journal = compact(self.settings.database)
        except sqlite3.OperationalError as exc:
            journal = f'not compacted now ({exc}); will retry next purge'
        return backup, result, journal

    def source_packs(self):
        """(all pack names with source counts, the packs currently enabled)."""
        import tomllib
        from importlib import resources
        catalog = tomllib.loads(resources.files('shade_node').joinpath('source_packs.toml').read_text(encoding='utf-8'))
        counts = {}
        for item in catalog['sources']:
            counts[item['pack']] = counts.get(item['pack'], 0) + 1
        enabled = sorted({s.pack for s in self.settings.sources if s.pack in counts})
        return counts, enabled

    def set_sources(self, *, source_id=None, on=None, packs=None):
        """Flip one source and/or set the enabled packs, then reload settings."""
        from . import local_config
        disabled, enabled = local_config.overrides(self.config)
        if source_id is not None:
            known = {s.id: s for s in self.settings.sources}
            if source_id not in known:
                raise ValueError('Unknown source.')
            disabled.discard(source_id); enabled.discard(source_id)
            (enabled if on else disabled).add(source_id)
        if packs is not None:
            counts, _ = self.source_packs()
            if set(packs) - set(counts):
                raise ValueError('Unknown source pack.')
        local_config.write(self.config, disabled=disabled, enabled=enabled,
                           packs=sorted(packs) if packs is not None else None)
        self.settings = load_settings(self.config)

    FEED_AREAS = ('', 'ETN/WNC', 'REGIONAL', 'NATIONAL')
    FEED_CATEGORIES = ('top-news', 'regional', 'public-safety', 'weather', 'cyber', 'chatter', 'general')

    def check_feed(self, url):
        """Fetch a feed once and confirm it parses; returns (item count, newest title). No data is stored."""
        from .collectors import collect
        from .model import SourceConfig
        probe = SourceConfig(id='probe', kind='rss', name='probe', url=url.strip(), source_type='media',
                             source_family='probe')
        try:
            items = collect(probe, user_agent=self.settings.user_agent, timeout=min(self.settings.timeout, 15),
                            max_bytes=self.settings.max_bytes)
        except ValueError:
            raise
        except Exception as exc:                       # network, HTTP or XML trouble -> plain message
            raise ValueError(f'Could not read that feed: {exc}') from exc
        return len(items), (items[0].title if items else '')

    def add_feed(self, *, name, url, area='', category='top-news', source_type='media'):
        """Add the operator's own RSS/Atom feed to config.local.toml (checked first)."""
        import re
        from urllib.parse import urlsplit
        from . import local_config
        from .model import SourceConfig
        name, url = ' '.join(str(name).split())[:80], str(url).strip()
        if not name:
            raise ValueError('Give the feed a name.')
        host = (urlsplit(url).hostname or '').lower()
        if not url.lower().startswith('https://') or not host:
            raise ValueError('Feed address must start with https://')
        if area not in self.FEED_AREAS or category not in self.FEED_CATEGORIES:
            raise ValueError('Unknown area or category.')
        if source_type not in ('media', 'official', 'community'):
            raise ValueError('Unknown source type.')
        if any(s.url.rstrip('/') == url.rstrip('/') for s in self.settings.sources):
            raise ValueError('That feed is already in your sources.')
        count, _ = self.check_feed(url)
        if not count:
            raise ValueError('That address answered but had no feed items. Is it the RSS/Atom link?')
        family = re.sub(r'^(www|feeds?|rss)\.', '', host)
        slug = 'my-' + (re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:40] or 'feed')
        taken = {s.id for s in self.settings.sources}
        feed_id, n = slug, 2
        while feed_id in taken:
            feed_id, n = f'{slug}-{n}', n + 1
        feed = {'id': feed_id, 'kind': 'rss', 'name': name, 'url': url, 'source_type': source_type,
                'source_family': family, 'category': category, 'area': area, 'pack': local_config.MY_PACK,
                'min_poll_seconds': 900}
        SourceConfig(**feed)                                  # same checks as config.toml
        local_config.write_feeds(self.config, local_config.my_feeds(self.config) + [feed])
        self.settings = load_settings(self.config)
        return feed

    def remove_feed(self, feed_id):
        from . import local_config
        feeds = local_config.my_feeds(self.config)
        if feed_id not in {f['id'] for f in feeds}:
            raise ValueError('Only feeds you added here can be removed here.')
        local_config.write_feeds(self.config, [f for f in feeds if f['id'] != feed_id])
        self.settings = load_settings(self.config)

    def sources(self, mode='standard'):
        selected = {s.id for s in active_sources(self.settings, mode)}
        with connect(self.settings.database) as db:
            polls = {row['source_id']:dict(row) for row in db.execute('SELECT * FROM source_polls')}
        return [(source, source.id in selected, polls.get(source.id)) for source in self.settings.sources]
