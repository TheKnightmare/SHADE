from __future__ import annotations

from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import json
from dataclasses import asdict
from hashlib import sha256
from .relevance import assess, confidence, timestamp, iso

from .model import Observation, token_set, normalize_text, utc_now


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'NEW',
    confidence_label TEXT NOT NULL DEFAULT 'UNVERIFIED',
    confidence_score INTEGER NOT NULL DEFAULT 0,
    structured_severity_score INTEGER NOT NULL DEFAULT 0,
    observation_count INTEGER NOT NULL DEFAULT 0,
    independent_families INTEGER NOT NULL DEFAULT 0,
    official_families INTEGER NOT NULL DEFAULT 0,
    tx_candidate_basis TEXT CHECK(tx_candidate_basis IN ('corroborated','operator_relay') OR tx_candidate_basis IS NULL),
    UNIQUE(fingerprint)
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_family TEXT NOT NULL,
    external_id TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    url TEXT NOT NULL,
    category TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'unknown',
    published_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE(source_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_claims_queue ON claims(status, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_observations_claim ON observations(claim_id);
CREATE INDEX IF NOT EXISTS idx_observations_family_external ON observations(source_family, external_id);
CREATE INDEX IF NOT EXISTS idx_observations_match ON observations(category, location, published_at);
"""

SCHEMA_VERSION = 6
MIGRATION_AUDIT_PREFIX = 'migration v4: backfilled TX_CANDIDATE basis as '
LEGACY_POLL_ERROR = 'Collection failed; see run output'
UNKNOWN_LEGACY_POLL_ERROR = 'Legacy failure cause unavailable; the next poll will record the exact error'


def backup_database(path):
    from contextlib import closing
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = Path(path).with_name(Path(path).name + '.pre-v0.6-' + stamp + '.bak')
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup validation failed')
    return str(target)


def _has_column(connection, table, column):
    return any(row[1] == column for row in connection.execute(f'PRAGMA table_info({table})'))


def _migrate_v4(connection):
    if not _has_column(connection, 'claims', 'tx_candidate_basis'):
        connection.execute("ALTER TABLE claims ADD COLUMN tx_candidate_basis TEXT CHECK(tx_candidate_basis IN ('corroborated','operator_relay') OR tx_candidate_basis IS NULL)")
    if not _has_column(connection, 'workflow_audit', 'tx_candidate_basis'):
        connection.execute("ALTER TABLE workflow_audit ADD COLUMN tx_candidate_basis TEXT CHECK(tx_candidate_basis IN ('corroborated','operator_relay') OR tx_candidate_basis IS NULL)")
    candidates = connection.execute("SELECT id FROM claims WHERE status='TX_CANDIDATE' AND tx_candidate_basis IS NULL ORDER BY id").fetchall()
    for row in candidates:
        claim_id = row[0]
        corroborated = connection.execute(
            "SELECT 1 FROM observations WHERE claim_id=? AND source_type IN ('official','media') LIMIT 1",
            (claim_id,),
        ).fetchone()
        if corroborated:
            basis = 'corroborated'
        else:
            legacy_override = any(
                json.loads(item[0]).get('_trusted_for_relay')
                for item in connection.execute('SELECT raw_json FROM observations WHERE claim_id=?',(claim_id,))
            )
            if not legacy_override:
                raise ValueError(f'Cannot infer TX_CANDIDATE basis for legacy claim {claim_id}')
            basis = 'operator_relay'
        connection.execute('UPDATE claims SET tx_candidate_basis=? WHERE id=?', (basis, claim_id))
        connection.execute(
            'INSERT INTO workflow_audit(claim_id,previous_status,new_status,reason,changed_at,tx_candidate_basis) VALUES(?,?,?,?,?,?)',
            (claim_id,'TX_CANDIDATE','TX_CANDIDATE',MIGRATION_AUDIT_PREFIX+basis,iso(utc_now()),basis),
        )
    for row in connection.execute('SELECT id FROM claims ORDER BY id').fetchall():
        recompute_claim(connection,row[0])
    connection.execute('PRAGMA user_version=4')



AUXILIARY_SCHEMA = '''
CREATE TABLE IF NOT EXISTS observation_revisions (
    id INTEGER PRIMARY KEY, observation_id INTEGER NOT NULL REFERENCES observations(id),
    content_hash TEXT NOT NULL, payload TEXT NOT NULL, observed_at TEXT NOT NULL,
    UNIQUE(observation_id, content_hash));
CREATE TABLE IF NOT EXISTS workflow_audit (
    id INTEGER PRIMARY KEY, claim_id INTEGER NOT NULL REFERENCES claims(id),
    previous_status TEXT NOT NULL, new_status TEXT NOT NULL, reason TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    tx_candidate_basis TEXT CHECK(tx_candidate_basis IN ('corroborated','operator_relay') OR tx_candidate_basis IS NULL));
CREATE TABLE IF NOT EXISTS event_supersessions (
    source_family TEXT NOT NULL, external_id TEXT NOT NULL, superseded_at TEXT NOT NULL,
    PRIMARY KEY(source_family,external_id));
CREATE TABLE IF NOT EXISTS source_presence (
    source_id TEXT NOT NULL, external_id TEXT NOT NULL, asof TEXT NOT NULL, active INTEGER NOT NULL,
    PRIMARY KEY(source_id,external_id));
CREATE TABLE IF NOT EXISTS source_polls (
    source_id TEXT PRIMARY KEY, last_attempt TEXT NOT NULL, next_allowed TEXT NOT NULL,
    success INTEGER NOT NULL, error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS claim_merges (
    child_id INTEGER PRIMARY KEY REFERENCES claims(id),
    parent_id INTEGER NOT NULL REFERENCES claims(id),
    merged_at TEXT NOT NULL,
    CHECK(child_id != parent_id));
CREATE INDEX IF NOT EXISTS idx_claim_merges_parent ON claim_merges(parent_id);
CREATE TABLE IF NOT EXISTS wire_transmissions (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER REFERENCES claims(id),
    sent_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    message_text TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(channel, sent_at, message_text));
CREATE INDEX IF NOT EXISTS idx_wire_claim ON wire_transmissions(claim_id);
CREATE TABLE IF NOT EXISTS source_http_cache (
    source_id TEXT PRIMARY KEY, etag TEXT NOT NULL DEFAULT '', last_modified TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS operator_feedback (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    action TEXT NOT NULL CHECK(action IN (
        'accept','reject','downgrade','suppress_source','suppress_category',
        'promote','mark_tx_candidate','unmark_tx_candidate')),
    reason_code TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    source_family TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    previous_status TEXT NOT NULL,
    resulting_status TEXT NOT NULL,
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_operator_feedback_claim ON operator_feedback(claim_id);
CREATE INDEX IF NOT EXISTS idx_operator_feedback_source ON operator_feedback(source_family);
CREATE INDEX IF NOT EXISTS idx_operator_feedback_category ON operator_feedback(category);
-- Daily Wire view-only curation metadata (SHADE's own review/assembly view
-- over the existing TX_CANDIDATE queue - see docs/DAILY_WIRE.md). Deliberately
-- has no CHECK-constrained enum column and no foreign relationship to
-- anything FIGHT reads, so adding it needs no SCHEMA_VERSION bump and no
-- rename/rebuild migration - the existing REQUIRED_OBJECTS mechanism in
-- connect() already backs up and adds any missing table on its own. Ordering/
-- notes/hidden-from-view are pure display curation: nothing here is ever
-- read by transition()/queue()/FIGHT's ShadeQueueAdapter, and no code path
-- writes to claims.status as a side effect of writing here.
CREATE TABLE IF NOT EXISTS wire_annotations (
    claim_id INTEGER PRIMARY KEY REFERENCES claims(id),
    position INTEGER,
    note TEXT NOT NULL DEFAULT '',
    hidden INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL);
'''
REQUIRED_OBJECTS = {
    'observation_revisions', 'workflow_audit', 'event_supersessions',
    'source_presence', 'source_polls', 'source_http_cache', 'idx_claims_queue',
    'operator_feedback', 'idx_operator_feedback_claim', 'idx_operator_feedback_source',
    'idx_operator_feedback_category',
    'idx_observations_claim', 'idx_observations_family_external',
    'idx_observations_match', 'claim_merges', 'idx_claim_merges_parent',
    'wire_transmissions', 'idx_wire_claim', 'wire_annotations',
}


def _migrate_v6(connection):
    connection.execute('PRAGMA user_version=6')


def _migrate_v5(connection):
    if _has_column(connection, 'claims', 'significance_score'):
        connection.execute('ALTER TABLE claims RENAME COLUMN significance_score TO structured_severity_score')
    connection.execute('DROP INDEX IF EXISTS idx_claims_queue')
    connection.execute('CREATE INDEX idx_claims_queue ON claims(status, last_seen DESC)')
    connection.execute('PRAGMA user_version=5')


@contextmanager
def connect(path: str, *, allow_migrate: bool = False, verified_backup: str | None = None):
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        existing = connection.execute("SELECT 1 FROM sqlite_master WHERE name='observations'").fetchone()
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError('Database is newer than this SHADE version')
        if existing and version < SCHEMA_VERSION and path != ':memory:' and not allow_migrate:
            raise ValueError('Database migration required; run `shade migrate` to create a verified backup and upgrade it')
        objects = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
        needs_schema = not existing or version < SCHEMA_VERSION or not REQUIRED_OBJECTS <= objects
        if needs_schema:
            if existing and path != ':memory:':
                if verified_backup:
                    with closing(sqlite3.connect(verified_backup)) as backup:
                        if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                            raise RuntimeError('Backup validation failed')
                else:
                    backup_database(path)  # SQLite backup API and integrity_check before any DDL.
            # Existing v4 databases need the rename before the new schema is applied.
            if existing and version == 4:
                _migrate_v5(connection)
            connection.executescript(SCHEMA)
            connection.executescript(AUXILIARY_SCHEMA)
            if version < 4:
                _migrate_v5(connection)
                _migrate_v4(connection)
                _migrate_v6(connection)
            elif not existing:
                _migrate_v5(connection)
                _migrate_v6(connection)
            elif version <= 5:
                _migrate_v6(connection)
            if existing and version == 4:
                for claim in connection.execute('SELECT id FROM claims ORDER BY id').fetchall():
                    recompute_claim(connection, claim['id'])
            connection.execute('UPDATE source_polls SET error=? WHERE error=?',
                               (UNKNOWN_LEGACY_POLL_ERROR, LEGACY_POLL_ERROR))
            for row in connection.execute("SELECT source_family,raw_json FROM observations WHERE category='weather'"):
                record_supersessions(connection,row['source_family'],json.loads(row['raw_json']))
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _story_tokens(title, source_name):
    suffix = ' - ' + source_name
    if source_name and title.casefold().endswith(suffix.casefold()):
        title = title[:-len(suffix)]
    return token_set(title)


def _similar_claim(connection, observation, hours=6):
    # Weather/earthquake/bulletins need source event identities, never generic title similarity.
    if observation.category in {'weather','earthquake','space-weather','disaster'}:
        return None
    if len(token_set(observation.title)) < 4 or not timestamp(observation.published_at):
        return None
    # A publisher's search and direct-feed hit are one originating article.
    # Search result IDs and categories may differ; title, publisher and time
    # must agree. This also prevents a search duplicate from reviving a
    # SUPPRESSED direct-feed claim as a new claim.
    if observation.source_type == 'media' and len(token_set(observation.title)) >= 7:
        # Long identical headlines within a few hours are one syndicated article
        # even when Google News lists many outlet domains. Keep every observation.
        target = normalize_text(observation.title.removesuffix(' - ' + observation.source_name))
        matches = set()
        for row in connection.execute(
            "SELECT claim_id,title,source_name FROM observations WHERE category=? AND source_type='media' AND published_at BETWEEN ? AND ?",
            (observation.category, iso(timestamp(observation.published_at)-timedelta(hours=6)),
             iso(timestamp(observation.published_at)+timedelta(hours=6)))):
            title = row['title'].removesuffix(' - ' + row['source_name'])
            if normalize_text(title) == target:
                matches.add(canonical_claim(connection, row['claim_id']))
        if matches:
            return min(matches)
    if observation.source_type == 'media':
        matches = set()
        for row in connection.execute(
            'SELECT claim_id,title,source_name,raw_json FROM observations WHERE source_family=? AND published_at BETWEEN ? AND ?',
            (observation.source_family, iso(timestamp(observation.published_at)-timedelta(hours=48)),
             iso(timestamp(observation.published_at)+timedelta(hours=48)))):
            if (observation.raw.get('_discovery_source') or json.loads(row['raw_json']).get('_discovery_source')) \
                    and _story_tokens(row['title'], row['source_name']) == _story_tokens(observation.title, observation.source_name):
                matches.add(row['claim_id'])
        if len(matches) == 1:
            return next(iter(matches))
    if not observation.location:
        return None
    matches=set()
    for row in connection.execute('SELECT * FROM observations WHERE category=? AND location=? AND published_at BETWEEN ? AND ?',
                                  (observation.category,observation.location,
                                   iso(timestamp(observation.published_at)-timedelta(hours=hours)),
                                   iso(timestamp(observation.published_at)+timedelta(hours=hours)))):
        a,b=timestamp(row['published_at']),timestamp(observation.published_at)
        if a and b and abs((a-b).total_seconds()) <= hours*3600 and token_set(row['title']) == token_set(observation.title):
            matches.add(row['claim_id'])
    return next(iter(matches)) if len(matches)==1 else None


def canonical_claim(connection, claim_id):
    row = connection.execute('SELECT parent_id FROM claim_merges WHERE child_id=?', (claim_id,)).fetchone()
    return row['parent_id'] if row else claim_id


def merge_claims(connection, primary_id, other_ids, *, allow_low_overlap=False):
    ids = list(dict.fromkeys(int(value) for value in other_ids if int(value) != primary_id))
    if not ids:
        raise ValueError('Select at least two different reports.')
    primary = connection.execute('SELECT * FROM claims WHERE id=?', (primary_id,)).fetchone()
    if not primary or primary['status'] in {'SENT', 'MERGED', 'EXPIRED'}:
        raise ValueError('Choose a current unsent primary report.')
    for child_id in ids:
        child = connection.execute('SELECT * FROM claims WHERE id=?', (child_id,)).fetchone()
        if not child or child['status'] in {'SENT', 'MERGED', 'EXPIRED'}:
            raise ValueError(f'Report {child_id} cannot be merged.')
        if child['category'] != primary['category']:
            raise ValueError(f'Report {child_id} has a different event category.')
        overlap = len(token_set(child['title']) & token_set(primary['title'])) / max(
            1, len(token_set(child['title']) | token_set(primary['title'])))
        if overlap < 0.35 and not allow_low_overlap:
            raise ValueError(f'Report {child_id} is too different from the primary headline.')
    now = iso(utc_now())
    for child_id in ids:
        previous = connection.execute('SELECT status FROM claims WHERE id=?', (child_id,)).fetchone()['status']
        connection.execute('INSERT INTO claim_merges(child_id,parent_id,merged_at) VALUES(?,?,?)',
                           (child_id, primary_id, now))
        connection.execute("UPDATE claims SET status='MERGED' WHERE id=?", (child_id,))
        connection.execute('INSERT INTO workflow_audit(claim_id,previous_status,new_status,reason,changed_at) VALUES(?,?,?,?,?)',
                           (child_id, previous, 'MERGED', f'Merged into report {primary_id}'
                            + (' (operator override: different headlines, same event)' if allow_low_overlap else ''), now))
    recompute_claim(connection, primary_id)
    return len(ids)


def record_wire_transmission(connection, claim_id, sent_at, channel, message_text):
    from .relevance import timestamp
    moment = timestamp(sent_at)
    if not moment:
        raise ValueError('Provide the actual transmission time with a timezone.')
    channel = channel.strip()
    message_text = message_text.strip()
    if not channel or not message_text:
        raise ValueError('Channel and transmitted text are required.')
    claim = connection.execute('SELECT status FROM claims WHERE id=?', (claim_id,)).fetchone()
    if not claim or claim['status'] == 'MERGED':
        raise ValueError('Choose an existing primary report.')
    now = iso(utc_now())
    cursor = connection.execute(
        'INSERT OR IGNORE INTO wire_transmissions(claim_id,sent_at,channel,message_text,recorded_at) VALUES(?,?,?,?,?)',
        (claim_id, iso(moment), channel, message_text, now))
    if cursor.rowcount and claim['status'] != 'SENT':
        connection.execute("UPDATE claims SET status='SENT' WHERE id=?", (claim_id,))
        connection.execute('INSERT INTO workflow_audit(claim_id,previous_status,new_status,reason,changed_at) VALUES(?,?,?,?,?)',
                           (claim_id, claim['status'], 'SENT', 'Operator recorded actual wire transmission', now))
    return bool(cursor.rowcount)


def _significance(severities, title, category):
    # Stored structured source severity only; the live 0-100 policy score is computed by assess().
    return max([{'unknown':0,'minor':3,'moderate':10,'severe':20,'extreme':30}.get(x,0) for x in severities] or [0])


def recompute_claim(connection: sqlite3.Connection, claim_id: int) -> None:
    rows = evidence_rows(connection,claim_id)
    label, confidence_score, family_count, official_count, _ = confidence(rows)
    title = rows[0]["title"] if rows else ""
    claim = connection.execute("SELECT category FROM claims WHERE id = ?", (claim_id,)).fetchone()
    significance = _significance([row["severity"] for row in rows], title, claim["category"])
    connection.execute(
        """UPDATE claims SET observation_count=?, independent_families=?, official_families=?,
        confidence_label=?, confidence_score=?, structured_severity_score=? WHERE id=?""",
        (connection.execute('SELECT COUNT(*) FROM observations WHERE claim_id=? OR claim_id IN (SELECT child_id FROM claim_merges WHERE parent_id=?)',(claim_id,claim_id)).fetchone()[0],
         family_count, official_count, label, confidence_score, significance, claim_id),
    )


def record_supersessions(connection, family, raw):
    p=raw.get('properties',{})
    when=p.get('sent') or p.get('effective')
    if not timestamp(when): return
    for reference in p.get('references',[]):
        external=reference.get('identifier')
        if external:
            connection.execute('INSERT OR REPLACE INTO event_supersessions VALUES(?,?,?)',(family,external,iso(timestamp(when))))


TOMBSTONE_DAYS = 30
_TOMBSTONE_TABLE = ('CREATE TABLE IF NOT EXISTS purged_items(source_id TEXT NOT NULL, external_id TEXT NOT NULL, '
                    'purged_at TEXT NOT NULL, PRIMARY KEY(source_id, external_id)) WITHOUT ROWID')


def purged_before(connection, source_id, external_id) -> bool:
    """True when the operator's purge already removed this exact source item.
    Feeds keep re-serving old items; without this they would return as NEW
    (and REJECTED decisions would be undone)."""
    try:
        return connection.execute('SELECT 1 FROM purged_items WHERE source_id=? AND external_id=?',
                                  (source_id, external_id)).fetchone() is not None
    except sqlite3.OperationalError:          # no purge has run on this database yet
        return False


def ingest(connection: sqlite3.Connection, observation: Observation) -> tuple[int, bool]:
    if purged_before(connection, observation.source_id, observation.external_id):
        return 0, False
    record_supersessions(connection,observation.source_family,observation.raw)
    existing = connection.execute(
        'SELECT * FROM observations WHERE source_id=? AND external_id=?',
        (observation.source_id, observation.external_id),
    ).fetchone()
    if existing:
        # Preserve the immutable original, store changed event revisions separately.
        payload = asdict(observation)
        evidence = {k:v for k,v in payload.items() if k != 'observed_at'}
        digest = sha256(json.dumps(evidence,sort_keys=True,default=str).encode()).hexdigest()
        old = dict(existing)
        same = all(old[k] == getattr(observation,k) for k in ('title','body','published_at','location','severity','url')) and json.loads(old['raw_json']) == observation.raw
        if same:
            return existing['claim_id'], False
        cursor=connection.execute('INSERT OR IGNORE INTO observation_revisions(observation_id,content_hash,payload,observed_at) VALUES(?,?,?,?)',
                                  (existing['id'],digest,json.dumps(payload,sort_keys=True),observation.observed_at))
        if cursor.rowcount:
            connection.execute('UPDATE claims SET last_seen=? WHERE id=?',(observation.observed_at,existing['claim_id']))
            from .relevance import metadata
            m=metadata({'raw_json':observation.raw_json(),'category':observation.category,'source_family':observation.source_family,
                        'title':observation.title,'published_at':observation.published_at,'observed_at':observation.observed_at})
            state=connection.execute('SELECT status FROM claims WHERE id=?',(existing['claim_id'],)).fetchone()[0]
            if state=='EXPIRED' and m['expires'] and m['expires']>utc_now() and m['properties'].get('messageType')!='Cancel':
                connection.execute("UPDATE claims SET status='NEW' WHERE id=?",(existing['claim_id'],))
                connection.execute('INSERT INTO workflow_audit(claim_id,previous_status,new_status,reason,changed_at) VALUES(?,?,?,?,?)',
                                   (existing['claim_id'],'EXPIRED','NEW','Source supplied a new validity extension',iso(utc_now())))
            recompute_claim(connection,canonical_claim(connection,existing['claim_id']))
        return canonical_claim(connection,existing['claim_id']), bool(cursor.rowcount)
    fingerprint = sha256((observation.source_family+'|'+observation.external_id).encode()).hexdigest()
    claim = connection.execute('SELECT id FROM claims WHERE fingerprint=?',(fingerprint,)).fetchone()
    if not claim:
        claim=connection.execute('SELECT claim_id AS id FROM observations WHERE source_family=? AND external_id=? LIMIT 1',
                                 (observation.source_family,observation.external_id)).fetchone()
    claim_id = claim['id'] if claim else _similar_claim(connection, observation)
    if claim_id is not None:
        claim_id = canonical_claim(connection, claim_id)
    if claim_id is None:
        cursor = connection.execute(
            """INSERT INTO claims(fingerprint,title,category,location,first_seen,last_seen)
            VALUES(?,?,?,?,?,?)""",
            (fingerprint, observation.title, observation.category, observation.location, observation.observed_at, observation.observed_at),
        )
        claim_id = cursor.lastrowid
    connection.execute(
        """INSERT INTO observations(claim_id,source_id,source_name,source_type,source_family,external_id,
        content_hash,title,body,url,category,location,severity,published_at,observed_at,raw_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            claim_id, observation.source_id, observation.source_name, observation.source_type,
            observation.source_family, observation.external_id, observation.content_hash,
            observation.title, observation.body, observation.url, observation.category,
            observation.location, observation.severity, observation.published_at,
            observation.observed_at, observation.raw_json(),
        ),
    )
    connection.execute("UPDATE claims SET last_seen=? WHERE id=?", (observation.observed_at, claim_id))
    recompute_claim(connection, claim_id)
    return claim_id, True


# TX_CANDIDATE is reachable directly from any live, non-terminal state, not
# only from REVIEW - an operator should be able to mark/unmark TX_CANDIDATE
# from a list view, search result, or bulk edit without first walking a claim
# through REVIEW. This widens *which states may attempt* the transition, not
# *what it takes to succeed*: transition() below still requires the same
# corroboration (an official/media source) or an explicit, audited
# operator_relay override before TX_CANDIDATE is actually granted, from any
# starting state.
ALLOWED_TRANSITIONS = {
    "NEW": {"CORRELATING", "REVIEW", "TX_CANDIDATE", "REJECTED"},
    "CORRELATING": {"REVIEW", "TX_CANDIDATE", "REJECTED"},
    "REVIEW": {"TX_CANDIDATE", "REJECTED"},
    "TX_CANDIDATE": {"SENT", "REJECTED", "REVIEW"},
    "SENT": set(),
    "REJECTED": {"REVIEW", "TX_CANDIDATE"},
    "EXPIRED": set(),
    "SUPPRESSED": {"REVIEW", "TX_CANDIDATE", "REJECTED"},
}


def transition(connection: sqlite3.Connection, claim_id: int, new_status: str, *, operator_override: bool = False) -> None:
    row = connection.execute("SELECT status FROM claims WHERE id=?", (claim_id,)).fetchone()
    if not row:
        raise ValueError(f"claim {claim_id} not found")
    new_status = new_status.upper()
    basis = None
    if new_status == "TX_CANDIDATE":
        evidence = connection.execute("SELECT source_type FROM observations WHERE claim_id=?", (claim_id,)).fetchall()
        corroborated = any(item["source_type"] in {"official", "media"} for item in evidence)
        if operator_override:
            basis = 'operator_relay'
        elif corroborated:
            basis = 'corroborated'
        else:
            raise ValueError("TX_CANDIDATE requires at least one official or media source family")
    elif operator_override:
        raise ValueError('Operator relay override applies only to TX_CANDIDATE')
    if new_status not in ALLOWED_TRANSITIONS.get(row["status"], set()):
        raise ValueError(f"invalid transition {row['status']} -> {new_status}")
    if basis:
        connection.execute("UPDATE claims SET status=?,tx_candidate_basis=? WHERE id=?", (new_status,basis,claim_id))
    else:
        connection.execute("UPDATE claims SET status=? WHERE id=?", (new_status,claim_id))
    reason = 'operator relay override' if basis == 'operator_relay' else 'operator'
    connection.execute('INSERT INTO workflow_audit(claim_id,previous_status,new_status,reason,changed_at,tx_candidate_basis) VALUES(?,?,?,?,?,?)',
                       (claim_id,row['status'],new_status,reason,iso(utc_now()),basis))


# Auditable operator feedback (see FEEDBACK_ACTIONS). This is the first step
# toward tuning transparent rules/thresholds from real operator decisions -
# deliberately just durable, reportable records, not an ML training set and
# not a mechanism that changes scoring automatically.
FEEDBACK_ACTIONS = {
    'accept': 'REVIEW', 'reject': 'REJECTED', 'downgrade': 'REVIEW',
    'promote': 'TX_CANDIDATE', 'mark_tx_candidate': 'TX_CANDIDATE',
    'unmark_tx_candidate': 'REVIEW',
    'suppress_source': 'SUPPRESSED', 'suppress_category': 'SUPPRESSED',
}


def _claim_provenance(connection, claim_id):
    row = connection.execute(
        "SELECT source_family FROM observations WHERE claim_id=? ORDER BY id DESC LIMIT 1", (claim_id,)).fetchone()
    return row['source_family'] if row else ''


def record_feedback(connection, claim_id, action, *, reason_code='', note='', operator_override=False):
    """Records one operator decision AND, for actions that correspond to a
    workflow transition, applies that transition through the existing,
    unchanged transition() (same corroboration/override rule, same
    workflow_audit entry - this never bypasses either). suppress_source and
    suppress_category apply SUPPRESSED to only the ONE claim being reviewed
    right now, exactly like any other single-claim status change - they do
    not retroactively touch other claims from that source/category. Using
    this data to automatically suppress future claims is a separate, later
    decision (see FEEDBACK.md / the completion report); this only records it."""
    if action not in FEEDBACK_ACTIONS:
        raise ValueError(f"unknown feedback action: {action}")
    claim = connection.execute('SELECT status, category FROM claims WHERE id=?', (claim_id,)).fetchone()
    if not claim:
        raise ValueError(f'claim {claim_id} not found')
    previous_status = claim['status']
    target = FEEDBACK_ACTIONS[action]
    if action in ('suppress_source', 'suppress_category'):
        # Same direct-UPDATE pattern housekeeping() already uses for SUPPRESSED
        # (an operator/administrative decision, not a corroboration-gated
        # workflow step) - applies to only this one claim, not retroactively
        # to others from the same source/category.
        if target != previous_status:
            connection.execute('UPDATE claims SET status=? WHERE id=?', (target, claim_id))
            connection.execute(
                'INSERT INTO workflow_audit(claim_id,previous_status,new_status,reason,changed_at) VALUES(?,?,?,?,?)',
                (claim_id, previous_status, target, f'operator feedback: {action}', iso(utc_now())))
    elif target != previous_status:
        transition(connection, claim_id, target, operator_override=operator_override)
    resulting = connection.execute('SELECT status FROM claims WHERE id=?', (claim_id,)).fetchone()['status']
    connection.execute(
        'INSERT INTO operator_feedback(claim_id,action,reason_code,note,source_family,category,'
        'previous_status,resulting_status,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
        (claim_id, action, reason_code or '', note or '', _claim_provenance(connection, claim_id),
         claim['category'], previous_status, resulting, iso(utc_now())))


def feedback_report(connection, *, group_by='category', action=None, limit=50):
    """Read-only aggregation for spotting recurring rejection/suppression
    patterns by source, category, or rule (reason_code) - exactly the
    'review or export view' the operator feedback loop needs before any rule
    or threshold is manually retuned. Never touches claims or workflow state."""
    column = {'source': 'source_family', 'category': 'category',
              'rule': 'reason_code', 'geography': 'category'}.get(group_by, 'category')
    where = []; params = []
    if action:
        where.append('action=?'); params.append(action)
    sql = (f'SELECT {column} AS group_key, action, COUNT(*) AS n FROM operator_feedback'
           + (' WHERE ' + ' AND '.join(where) if where else '')
           + f' GROUP BY {column}, action ORDER BY n DESC LIMIT ?')
    params.append(limit)
    return [dict(row) for row in connection.execute(sql, params)]


def bulk_transition(connection, claim_ids, new_status, *, operator_override=False, action=None,
                     reason_code='', note=''):
    """Applies the same transition (optionally with feedback recording) to
    several claims in one call. Every claim is independent - one failure
    (e.g. a claim that can't reach TX_CANDIDATE without corroboration) does
    not roll back or block the others; each result is reported individually
    so the caller/operator can see exactly which ones succeeded."""
    results = []
    for claim_id in claim_ids:
        try:
            if action:
                record_feedback(connection, claim_id, action, reason_code=reason_code, note=note,
                                 operator_override=operator_override)
            else:
                transition(connection, claim_id, new_status, operator_override=operator_override)
            results.append({'claim_id': claim_id, 'ok': True, 'error': None})
        except ValueError as e:
            results.append({'claim_id': claim_id, 'ok': False, 'error': str(e)})
    return results


def evidence_rows(connection, claim_id):
    originals=[dict(row) for row in connection.execute('SELECT * FROM observations WHERE claim_id=? OR claim_id IN (SELECT child_id FROM claim_merges WHERE parent_id=?) ORDER BY id',(claim_id,claim_id))]
    rows=list(originals)
    for original in originals:
        for revision in connection.execute('SELECT * FROM observation_revisions WHERE observation_id=? ORDER BY id',(original['id'],)):
            data=json.loads(revision['payload']); raw=data.pop('raw')
            data.update(id=original['id'],claim_id=claim_id,raw_json=json.dumps(raw),revision_id=revision['id'])
            rows.append(data)
    for row in rows:
        raw=json.loads(row['raw_json'])
        superseded=connection.execute('SELECT superseded_at FROM event_supersessions WHERE source_family=? AND external_id=?',(row['source_family'],row['external_id'])).fetchone()
        if superseded: raw['_superseded_at']=superseded[0]
        if raw.get('_snapshot'):
            presence=connection.execute('SELECT * FROM source_presence WHERE source_id=? AND external_id=?',(row['source_id'],row['external_id'])).fetchone()
            if presence:
                raw['_snapshot_asof']=presence['asof'];raw['_snapshot_active']=bool(presence['active'])
        row['raw_json']=json.dumps(raw)
    return rows


def claim_detail(connection, claim_id, policy=None, now=None):
    claim=connection.execute('SELECT * FROM claims WHERE id=?',(claim_id,)).fetchone()
    if not claim: raise ValueError(f'claim {claim_id} not found')
    rows=evidence_rows(connection,claim_id)
    rank={'official':3,'media':2,'community':1}
    rows.sort(key=lambda row:(rank.get(row['source_type'],0),row['published_at'],row['observed_at']),reverse=True)
    for index,row in enumerate(rows):
        row['display_role']='lead' if index == 0 else 'supporting'
    result = assess(claim,rows,policy,now)
    result['wire_transmissions'] = [dict(item) for item in connection.execute(
        'SELECT sent_at,channel,message_text FROM wire_transmissions WHERE claim_id=? ORDER BY sent_at DESC',
        (claim_id,))]
    return result,rows


# --------------------------------------------------------------------------
# Bulk evidence loading for queue(). claim_detail()/evidence_rows() above are
# unchanged and still used for every single-claim command (show/format/mark/
# relay) - correct, simple, one claim at a time. queue() historically called
# claim_detail() once per candidate claim, which meant one query for the
# claim row plus ~4-10 more (observations, per-observation revisions,
# per-row supersession/presence lookups, wire transmissions) EVERY TIME - on
# this station's real database (~4,000+ NEW claims in the default inbox
# view) that was tens of thousands of individual SQLite round trips per
# queue() call and measured at 6.7s for one inbox load. The auxiliary tables
# involved (supersessions, presence, merges) are small (hundreds of rows
# station-wide); observations/revisions are a few thousand. Loading each of
# them ONCE per queue() call and grouping in Python produces byte-identical
# results to the old per-claim-query path (same assess()/evaluate() scoring,
# same evidence, same ordering) for a small, fixed number of queries instead
# of one that grows with the candidate set.
def _bulk_evidence_context(connection):
    ctx = {
        'observations_by_claim': {},
        'revisions_by_observation': {},
        'superseded_by_key': {},
        'presence_by_key': {},
        'children_by_parent': {},
        'wire_by_claim': {},
    }
    for row in connection.execute('SELECT * FROM observations ORDER BY id'):
        ctx['observations_by_claim'].setdefault(row['claim_id'], []).append(dict(row))
    for row in connection.execute('SELECT * FROM observation_revisions ORDER BY id'):
        ctx['revisions_by_observation'].setdefault(row['observation_id'], []).append(dict(row))
    for row in connection.execute('SELECT source_family, external_id, superseded_at FROM event_supersessions'):
        ctx['superseded_by_key'][(row['source_family'], row['external_id'])] = row['superseded_at']
    for row in connection.execute('SELECT * FROM source_presence'):
        ctx['presence_by_key'][(row['source_id'], row['external_id'])] = dict(row)
    for row in connection.execute('SELECT parent_id, child_id FROM claim_merges'):
        ctx['children_by_parent'].setdefault(row['parent_id'], []).append(row['child_id'])
    for row in connection.execute('SELECT claim_id, sent_at, channel, message_text FROM wire_transmissions ORDER BY sent_at DESC'):
        ctx['wire_by_claim'].setdefault(row['claim_id'], []).append(
            {'sent_at': row['sent_at'], 'channel': row['channel'], 'message_text': row['message_text']})
    return ctx


def _evidence_rows_bulk(ctx, claim_id):
    """Same output shape and semantics as evidence_rows(), from the ctx built
    by _bulk_evidence_context() instead of per-claim queries."""
    relevant_ids = [claim_id] + ctx['children_by_parent'].get(claim_id, [])
    originals = []
    for cid in relevant_ids:
        originals.extend(ctx['observations_by_claim'].get(cid, []))
    originals.sort(key=lambda r: r['id'])
    rows = list(originals)
    for original in originals:
        for revision in ctx['revisions_by_observation'].get(original['id'], []):
            data = json.loads(revision['payload']); raw = data.pop('raw')
            data.update(id=original['id'], claim_id=claim_id, raw_json=json.dumps(raw), revision_id=revision['id'])
            rows.append(data)
    for row in rows:
        # Only unpack/repack raw_json when something is actually added to it;
        # parsing every row on every refresh was ~1/3 of Inbox time. Readers
        # consume raw_json via json.loads, so the result is identical.
        superseded = ctx['superseded_by_key'].get((row['source_family'], row['external_id']))
        presence = ctx['presence_by_key'].get((row['source_id'], row['external_id'])) if '"_snapshot"' in row['raw_json'] else None
        if not superseded and not presence:
            continue
        raw = json.loads(row['raw_json'])
        if superseded: raw['_superseded_at'] = superseded
        if raw.get('_snapshot') and presence:
            raw['_snapshot_asof'] = presence['asof']; raw['_snapshot_active'] = bool(presence['active'])
        row['raw_json'] = json.dumps(raw)
    return rows


def _claim_detail_bulk(ctx, claim_row, policy=None, now=None):
    """Same output shape as claim_detail(), from a claim ROW ALREADY fetched
    by the caller (queue() fetches every candidate claim's full row in its
    own single query) plus the shared bulk evidence context."""
    claim_id = claim_row['id']
    rows = _evidence_rows_bulk(ctx, claim_id)
    rank = {'official': 3, 'media': 2, 'community': 1}
    rows.sort(key=lambda row: (rank.get(row['source_type'], 0), row['published_at'], row['observed_at']), reverse=True)
    for index, row in enumerate(rows):
        row['display_role'] = 'lead' if index == 0 else 'supporting'
    result = assess(claim_row, rows, policy, now)
    result['wire_transmissions'] = ctx['wire_by_claim'].get(claim_id, [])
    return result, rows


def queue_prefilter(raw_status, *, status=None, include_suppressed=False):
    """queue()'s SQL status condition, for a claim whose stored status is known."""
    if status and status.upper() != 'EXPIRED':
        return raw_status == status.upper()
    if not status:
        excluded = {'SENT', 'REJECTED', 'EXPIRED', 'MERGED'} | (set() if include_suppressed else {'SUPPRESSED'})
        return raw_status not in excluded
    return True


def queue_accepts(item, *, now, lane='inbox', category=None, area=None, status=None,
                  include_suppressed=False, minimum_score=None, max_age=None):
    """queue()'s per-item view filter (shared with the web console's
    in-place update after a review action, so both use identical rules)."""
    if status and item['status'] != status.upper(): return False
    if not status and item['status'] in {'SENT','REJECTED','EXPIRED','MERGED'}: return False
    if category and item['category'] != category: return False
    if area and item['area'] != area.upper(): return False
    if lane != 'all' and item['lane'] != lane: return False
    if lane=='now' and (item['expired'] or not item['expires'] or (timestamp(item['onset']) and timestamp(item['onset'])>now)): return False
    if item['reason'] and not include_suppressed and not status: return False
    if minimum_score is not None and item['score']<minimum_score: return False
    if max_age is not None and (not timestamp(item['published']) or (now-timestamp(item['published'])).total_seconds()>max_age*3600): return False
    return True


def queue(connection, minimum_score=None, limit=50, *, lane='inbox', policy=None, now=None,
          category=None, area=None, status=None, max_age=None, include_suppressed=False):
    now=now or utc_now(); results=[]
    where=[]; params=[]
    # gazetteer.resolve() is a pure function of its title text, so caching it
    # is always correct - but this clear keeps that cache scoped to a single
    # queue() call rather than letting it accumulate indefinitely across many
    # calls in a long-lived desktop process. It still pays off within this
    # call: thousands of candidate/evidence rows here repeat many of the same
    # or near-duplicate headlines (~1.55s of self-time previously spent
    # re-parsing the same titles, profiled on the production database).
    # gazetteer.resolve() is now a bounded LRU kept across calls (pure
    # function of the title), so repeated desktop refreshes reuse it.
    if status and status.upper() != 'EXPIRED':
        where.append('status=?'); params.append(status.upper())
    elif not status:
        excluded=['SENT','REJECTED','EXPIRED','MERGED']
        if not include_suppressed: excluded.append('SUPPRESSED')
        where.append('status NOT IN ('+','.join('?' for _ in excluded)+')')
        params.extend(excluded)
    if category:
        if category == 'earthquake':
            where.append("category IN ('earthquake','disaster')")
        else:
            where.append('category=?'); params.append(category)
    sql='SELECT * FROM claims'+(' WHERE '+' AND '.join(where) if where else '')
    # The bulk-prefetch path below issues several separate SELECT statements
    # (claims, then every observations/revisions/supersessions/presence/merges/
    # wire_transmissions row) rather than one query per claim. Without an
    # explicit transaction, each of those SELECTs runs in its own autocommit-
    # mode read, so a concurrent writer (the collector, running every 15
    # minutes) that commits BETWEEN two of them could hand this function a
    # torn view - e.g. a new observation visible but its claim's row not, or
    # vice versa. An explicit read transaction gives SQLite's own snapshot
    # isolation for the whole sequence: every statement inside it sees the
    # database exactly as it was at BEGIN, even if a writer commits mid-read.
    # ROLLBACK (not COMMIT) at the end because nothing here ever writes -
    # ending the transaction either way releases it; ROLLBACK is the more
    # honest statement of intent for a read-only sequence.
    #
    # Only opens a transaction if one isn't already running: a caller that
    # ingest()s (or otherwise writes) and then calls queue() on the SAME
    # connection without committing in between - the collector itself, and
    # most of this project's own tests - is relying on seeing its own
    # uncommitted writes. Unconditionally starting a new transaction here
    # would either collide with that open one (sqlite3 refuses nested
    # BEGINs) or, if silently ignored, roll back the caller's pending writes
    # out from under it. Riding along inside an already-open transaction is
    # exactly as consistent as starting a fresh one - it's still one snapshot
    # for every statement below - so this only ever adds isolation, never
    # removes it.
    own_transaction = not connection.in_transaction
    if own_transaction:
        connection.execute('BEGIN')
    try:
        candidates = connection.execute(sql,params).fetchall()
        ctx = _bulk_evidence_context(connection)
    finally:
        if own_transaction:
            connection.execute('ROLLBACK')
    for row in candidates:
        item,_=_claim_detail_bulk(ctx,row,policy,now)
        if queue_accepts(item, now=now, lane=lane, category=category, area=area, status=status,
                         include_suppressed=include_suppressed, minimum_score=minimum_score, max_age=max_age):
            results.append(item)
    rank={'ETN/WNC':4,'REGIONAL':3,'NATIONAL':2,'GLOBAL':1,'DISTANT':0}
    results.sort(key=lambda r:(rank.get(r['area'],0),r['score'],r['published'],-r['id']),reverse=True)
    return results[:limit]


# --------------------------------------------------------------------------
# Daily Wire: a SHADE-side review/assembly view over the existing
# TX_CANDIDATE queue (see docs/DAILY_WIRE.md for the full boundary rationale).
# Membership is exactly claims.status='TX_CANDIDATE' - the same signal
# FIGHT's own ShadeQueueAdapter reads - so this reuses queue() itself
# (status='TX_CANDIDATE') rather than re-implementing its bulk-prefetch
# query path. Ordering/notes/hidden-from-view are pure display curation from
# wire_annotations (see AUXILIARY_SCHEMA); nothing here ever writes to
# claims.status, and FIGHT never reads wire_annotations.
def _wire_annotations(connection):
    return {row['claim_id']: dict(row) for row in connection.execute(
        'SELECT claim_id, position, note, hidden, updated_at FROM wire_annotations')}


def _correlated_child_statuses(connection):
    """parent_id -> list of {child_id, status} for every merged child - one
    query, used by daily_wire()'s warning computation instead of a per-item
    lookup."""
    out = {}
    for row in connection.execute(
            'SELECT cm.parent_id AS parent_id, cm.child_id AS child_id, c.status AS status '
            'FROM claim_merges cm JOIN claims c ON c.id = cm.child_id'):
        out.setdefault(row['parent_id'], []).append({'child_id': row['child_id'], 'status': row['status']})
    return out


def _wire_warnings(item, children):
    warnings = []
    if item['area'] in {'GLOBAL', 'NATIONAL', 'DISTANT'}:
        warnings.append(f"{item['area']} item in a regional Daily Wire")
    # area is the primary geography signal, not location: a genuine ETN/WNC
    # item is often classified local via local_terms/county matching in the
    # title/body text, with no separately populated location field - that is
    # normal, not a defect, so only warn about a missing location for items
    # whose broader area doesn't already establish a home-region tie.
    if not item['location'] and item['area'] != 'ETN/WNC':
        warnings.append('No location stated')
    if item['expired']:
        warnings.append('Stale or expired')
    if children:
        warnings.append(f"{len(children)} correlated/merged report(s) folded into this claim")
        if any(child['status'] == 'SENT' for child in children):
            warnings.append('A correlated child claim was previously marked SENT')
    if item['area'] != 'ETN/WNC':
        warnings.append('Weak or unclear regional impact')
    return warnings


def daily_wire(connection, policy=None, now=None, include_hidden=False):
    """Current TX_CANDIDATE claims, annotated with Daily Wire view metadata
    and triage warnings. Never changes claims.status. Operator-positioned
    items sort first (by position); everything else keeps queue()'s normal
    relevance ordering.

    Deliberately does NOT call queue(status='TX_CANDIDATE'): queue()'s row
    loop re-filters on assess()'s RECOMPUTED display status (which overrides
    to 'EXPIRED' once a claim's evidence has expired, regardless of the raw
    claims.status column), so a stale-but-still-TX_CANDIDATE claim would
    silently vanish from that call instead of surfacing as a warning - the
    opposite of what this view needs, and a real divergence from FIGHT's
    ShadeQueueAdapter, which reads claims.status directly. This queries that
    same raw column directly and reuses the identical bulk-prefetch/
    transaction pattern queue() uses (_bulk_evidence_context/
    _claim_detail_bulk) - same optimized path, correct membership signal.
    """
    now = now or utc_now()
    own_transaction = not connection.in_transaction
    if own_transaction:
        connection.execute('BEGIN')
    try:
        candidates = connection.execute("SELECT * FROM claims WHERE status='TX_CANDIDATE'").fetchall()
        ctx = _bulk_evidence_context(connection)
    finally:
        if own_transaction:
            connection.execute('ROLLBACK')
    items = [_claim_detail_bulk(ctx, row, policy, now)[0] for row in candidates]
    annotations = _wire_annotations(connection)
    children_by_parent = _correlated_child_statuses(connection)
    for item in items:
        annotation = annotations.get(item['id'], {})
        item['wire_position'] = annotation.get('position')
        item['wire_note'] = annotation.get('note', '')
        item['wire_hidden'] = bool(annotation.get('hidden', 0))
        item['wire_warnings'] = _wire_warnings(item, children_by_parent.get(item['id'], []))
    if not include_hidden:
        items = [item for item in items if not item['wire_hidden']]
    # Same relevance ranking queue() itself sorts by, applied here since this
    # function queries claims directly rather than going through queue()'s
    # own row loop (see the docstring above for why).
    rank = {'ETN/WNC': 4, 'REGIONAL': 3, 'NATIONAL': 2, 'GLOBAL': 1, 'DISTANT': 0}
    positioned = sorted((item for item in items if item['wire_position'] is not None),
                        key=lambda item: item['wire_position'])
    unpositioned = sorted((item for item in items if item['wire_position'] is None),
                          key=lambda item: (rank.get(item['area'], 0), item['score'], item['published'], -item['id']),
                          reverse=True)
    return positioned + unpositioned


def set_wire_annotation(connection, claim_id, *, position=None, note=None, hidden=None):
    """Partial update of one claim's Daily Wire curation metadata. Never
    touches claims.status or workflow_audit - this is view-only state."""
    if not connection.execute('SELECT 1 FROM claims WHERE id=?', (claim_id,)).fetchone():
        raise ValueError(f'claim {claim_id} not found')
    existing = connection.execute(
        'SELECT position, note, hidden FROM wire_annotations WHERE claim_id=?', (claim_id,)).fetchone()
    new_position = position if position is not None else (existing['position'] if existing else None)
    new_note = note if note is not None else (existing['note'] if existing else '')
    new_hidden = int(hidden) if hidden is not None else (existing['hidden'] if existing else 0)
    connection.execute(
        'INSERT INTO wire_annotations(claim_id,position,note,hidden,updated_at) VALUES(?,?,?,?,?) '
        'ON CONFLICT(claim_id) DO UPDATE SET position=excluded.position, note=excluded.note, '
        'hidden=excluded.hidden, updated_at=excluded.updated_at',
        (claim_id, new_position, new_note, new_hidden, iso(utc_now())))


def reorder_wire(connection, ordered_claim_ids):
    """Assigns display position 1..N to the given claim ids, in order. The
    caller passes the FULL currently-visible, operator-arranged Daily Wire
    list after a move - never partial - so this always produces a consistent
    total order. View-only; never touches claims.status."""
    now = iso(utc_now())
    for position, claim_id in enumerate(ordered_claim_ids, 1):
        set_wire_annotation(connection, claim_id, position=position)


def housekeeping(connection, policy=None, *, apply=False, suppress=False, now=None, only=None):
    """`only`: optional set of (claim_id, target_status) - act on just those."""
    changes=[]
    # SUPPRESSED is non-final: keep it held, but allow evidence expiry to move
    # it to the terminal EXPIRED state. Never re-run routine suppression rules
    # against an already-suppressed claim or silently restore it to NEW.
    for row in connection.execute("SELECT * FROM claims WHERE status NOT IN ('SENT','REJECTED','EXPIRED')"):
        item,_=claim_detail(connection,row['id'],policy,now)
        target='EXPIRED' if item['expired'] else 'SUPPRESSED' if suppress and item['reason']=='Routine newsletter or announcement' and row['status']=='NEW' else None
        if target and only is not None and (row['id'], target) not in only:
            continue
        if target:
            changes.append({'id':row['id'],'status':target,'reason':item['reason']})
            if apply:
                connection.execute('UPDATE claims SET status=? WHERE id=?',(target,row['id']))
                connection.execute('INSERT INTO workflow_audit(claim_id,previous_status,new_status,reason,changed_at) VALUES(?,?,?,?,?)',
                                   (row['id'],row['status'],target,item['reason'],iso(now or utc_now())))
    return changes


PURGE_MAX_HOURS = 72
PURGE_PROTECTED_STATUSES = ('SENT', 'TX_CANDIDATE')


def purge_plan(connection, hours=PURGE_MAX_HOURS, now=None):
    """Claims eligible for permanent deletion: first collected (claims.first_seen), or
    with every report published, more than `hours` ago (capped at 72), whatever their state: NEW, suppressed,
    REVIEW, REJECTED, EXPIRED. Feeds keep re-serving old items, so re-observation
    no longer keeps a report alive; purged items are tombstoned so they do not
    come back. Protected and kept:
    SENT and TX_CANDIDATE claims, anything with a wire_transmissions record,
    and every claim merged with a protected one (in either direction), so the
    record of what actually went out and FIGHT's current candidates survive."""
    hours = float(hours)
    if not 1 <= hours <= PURGE_MAX_HOURS:
        raise ValueError(f'Purge age must be between 1 and {PURGE_MAX_HOURS} hours.')
    cutoff = iso((now or utc_now()) - timedelta(hours=hours))
    keep = {row[0] for row in connection.execute(
        'SELECT id FROM claims WHERE status IN (%s) UNION SELECT claim_id FROM wire_transmissions WHERE claim_id IS NOT NULL'
        % ','.join('?' for _ in PURGE_PROTECTED_STATUSES), PURGE_PROTECTED_STATUSES)}
    merges = connection.execute('SELECT parent_id, child_id FROM claim_merges').fetchall()
    changed = True
    while changed:
        changed = False
        for parent, child in merges:
            if (parent in keep) != (child in keep):
                keep.update((parent, child)); changed = True
    # Old = first collected before the cutoff, OR every one of its reports was published before
    # the cutoff (what the Inbox Age column shows). Feeds and newly added sources often serve
    # week-old stories that SHADE collects for the first time today; those count as old too.
    old_rows = connection.execute(
        "SELECT c.id, c.status FROM claims c WHERE c.first_seen < ? "
        "OR (SELECT MAX(o.published_at) FROM observations o WHERE o.claim_id = c.id AND o.published_at != '') < ? "
        "ORDER BY c.id", (cutoff, cutoff)).fetchall()
    old = [row[0] for row in old_rows]
    purge = [claim_id for claim_id in old if claim_id not in keep]
    by_status = {}
    wanted = set(purge)
    for claim_id, status in old_rows:
        if claim_id in wanted:
            by_status[status] = by_status.get(status, 0) + 1
    return {'cutoff': cutoff, 'hours': hours, 'claim_ids': purge, 'by_status': by_status,
            'protected_old': len(old) - len(purge), 'total_claims': connection.execute('SELECT COUNT(*) FROM claims').fetchone()[0]}


def purge(connection, plan):
    """Delete the claims in `plan` and everything that references them, in
    one transaction. The caller must take a verified backup first."""
    ids = plan['claim_ids']
    if not ids:
        return {'claims': 0, 'observations': 0}
    own = not connection.in_transaction
    if own:
        connection.execute('BEGIN IMMEDIATE')
    try:
        connection.execute('CREATE TEMP TABLE IF NOT EXISTS purge_ids(id INTEGER PRIMARY KEY)')
        connection.execute('DELETE FROM purge_ids')
        connection.executemany('INSERT INTO purge_ids(id) VALUES (?)', ((i,) for i in ids))
        protected = connection.execute(
            'SELECT COUNT(*) FROM claims WHERE id IN (SELECT id FROM purge_ids) AND status IN (%s)'
            % ','.join('?' for _ in PURGE_PROTECTED_STATUSES), PURGE_PROTECTED_STATUSES).fetchone()[0]
        if protected:
            raise ValueError('Purge plan is stale: a planned claim is now protected. Preview again.')
        observations = connection.execute(
            'SELECT COUNT(*) FROM observations WHERE claim_id IN (SELECT id FROM purge_ids)').fetchone()[0]
        connection.execute(_TOMBSTONE_TABLE)
        stamp = iso(utc_now())
        connection.execute('INSERT OR REPLACE INTO purged_items(source_id, external_id, purged_at) '
                           'SELECT source_id, external_id, ? FROM observations WHERE claim_id IN (SELECT id FROM purge_ids)',
                           (stamp,))
        connection.execute('DELETE FROM purged_items WHERE purged_at < ?',
                           (iso(utc_now() - timedelta(days=TOMBSTONE_DAYS)),))
        for statement in (
            'DELETE FROM observation_revisions WHERE observation_id IN (SELECT id FROM observations WHERE claim_id IN (SELECT id FROM purge_ids))',
            'DELETE FROM claim_merges WHERE child_id IN (SELECT id FROM purge_ids) OR parent_id IN (SELECT id FROM purge_ids)',
            'DELETE FROM workflow_audit WHERE claim_id IN (SELECT id FROM purge_ids)',
            'DELETE FROM operator_feedback WHERE claim_id IN (SELECT id FROM purge_ids)',
            'DELETE FROM wire_annotations WHERE claim_id IN (SELECT id FROM purge_ids)',
            'DELETE FROM observations WHERE claim_id IN (SELECT id FROM purge_ids)',
            'DELETE FROM claims WHERE id IN (SELECT id FROM purge_ids)',
        ):
            connection.execute(statement)
        connection.execute('DELETE FROM purge_ids')
        if own:
            connection.execute('COMMIT')
    except Exception:
        if own:
            connection.execute('ROLLBACK')
        raise
    return {'claims': len(ids), 'observations': observations}


def compact(path):
    """Reclaim disk space after a purge and switch to WAL so the scheduled
    collector's writes no longer block desktop reads. Needs a moment of
    exclusive access; retried on the next purge if the database is busy."""
    with closing(sqlite3.connect(path, timeout=30)) as connection:
        connection.execute('VACUUM')
        return connection.execute('PRAGMA journal_mode=WAL').fetchone()[0]


def suppression_review(connection, policy=None, now=None):
    """Read-only review of previously suppressed claims affected by retired rules."""
    retired = {'No established domestic/regional impact',
               'No concrete operational impact identified',
               'No civil-unrest event signal',
               'Below configured relevance threshold'}
    results = []
    for row in connection.execute("SELECT id FROM claims WHERE status='SUPPRESSED' ORDER BY id"):
        audit = connection.execute(
            "SELECT reason FROM workflow_audit WHERE claim_id=? AND new_status='SUPPRESSED' ORDER BY id DESC LIMIT 1",
            (row['id'],)).fetchone()
        if not audit or audit['reason'] not in retired:
            continue
        item, _ = claim_detail(connection, row['id'], policy, now)
        if item['reason'] != audit['reason']:
            results.append({'id': row['id'], 'title': item['title'],
                            'previous_reason': audit['reason'], 'current_reason': item['reason'],
                            'would_be_visible': not bool(item['reason']), 'score': item['score'],
                            'area': item['area'], 'status': 'SUPPRESSED'})
    return results
