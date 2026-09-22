"""Desktop service boundary: every operation opens its own database connection."""
from pathlib import Path
from datetime import datetime, timezone
from .db import connect, queue, claim_detail, transition, housekeeping, backup_database
from .engine import load_settings, run_once, normalize_mode, active_sources
from .formatters import traffic_message


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

    def detail(self, claim_id, mode='standard'):
        with connect(self.settings.database) as db:
            return claim_detail(db, claim_id, self.policy(mode))

    def mark(self, claim_id, target, mode='standard'):
        with connect(self.settings.database) as db:
            claim, _ = claim_detail(db, claim_id, self.policy(mode))
            if claim['expired']:
                raise ValueError('This report has expired and cannot advance through review.')
            transition(db, claim_id, target)

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

    def sources(self, mode='standard'):
        selected = {s.id for s in active_sources(self.settings, mode)}
        with connect(self.settings.database) as db:
            polls = {row['source_id']:dict(row) for row in db.execute('SELECT * FROM source_polls')}
        return [(source, source.id in selected, polls.get(source.id)) for source in self.settings.sources]
