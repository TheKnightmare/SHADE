from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import json
from dataclasses import asdict
from hashlib import sha256
from .relevance import assess, timestamp, iso

from .model import Observation, token_set, utc_now


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
    significance_score INTEGER NOT NULL DEFAULT 0,
    observation_count INTEGER NOT NULL DEFAULT 0,
    independent_families INTEGER NOT NULL DEFAULT 0,
    official_families INTEGER NOT NULL DEFAULT 0,
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
CREATE INDEX IF NOT EXISTS idx_claims_queue ON claims(status, significance_score DESC, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_observations_claim ON observations(claim_id);
"""


def backup_database(path):
    from contextlib import closing
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = Path(path).with_name(Path(path).name + '.pre-v0.3-' + stamp + '.bak')
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup validation failed')
    return str(target)


@contextmanager
def connect(path: str):
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        existing = connection.execute("SELECT 1 FROM sqlite_master WHERE name='observations'").fetchone()
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version > 3:
            raise ValueError('Database is newer than this SHADE version')
        if existing and version < 3 and path != ':memory:':
            backup_database(path)
        connection.executescript(SCHEMA)
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS observation_revisions (
            id INTEGER PRIMARY KEY, observation_id INTEGER NOT NULL REFERENCES observations(id),
            content_hash TEXT NOT NULL, payload TEXT NOT NULL, observed_at TEXT NOT NULL,
            UNIQUE(observation_id, content_hash));
        CREATE TABLE IF NOT EXISTS workflow_audit (
            id INTEGER PRIMARY KEY, claim_id INTEGER NOT NULL REFERENCES claims(id),
            previous_status TEXT NOT NULL, new_status TEXT NOT NULL, reason TEXT NOT NULL,
            changed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS event_supersessions (
            source_family TEXT NOT NULL, external_id TEXT NOT NULL, superseded_at TEXT NOT NULL,
            PRIMARY KEY(source_family,external_id));
        CREATE TABLE IF NOT EXISTS source_presence (
            source_id TEXT NOT NULL, external_id TEXT NOT NULL, asof TEXT NOT NULL, active INTEGER NOT NULL,
            PRIMARY KEY(source_id,external_id));
        CREATE TABLE IF NOT EXISTS source_polls (
            source_id TEXT PRIMARY KEY, last_attempt TEXT NOT NULL, next_allowed TEXT NOT NULL,
            success INTEGER NOT NULL, error TEXT NOT NULL DEFAULT '');
        PRAGMA user_version=3;
        """)
        # Idempotently recover references from legacy raw evidence as well.
        for row in connection.execute("SELECT source_family,raw_json FROM observations WHERE category='weather'"):
            record_supersessions(connection,row['source_family'],json.loads(row['raw_json']))
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _similar_claim(connection, observation, hours=6):
    # Weather/earthquake/bulletins need source event identities, never generic title similarity.
    if observation.category in {'weather','earthquake','space-weather','disaster'}:
        return None
    if not observation.location or len(token_set(observation.title)) < 4:
        return None
    matches=set()
    for row in connection.execute('SELECT * FROM observations WHERE category=? AND location=?', (observation.category,observation.location)):
        a,b=timestamp(row['published_at']),timestamp(observation.published_at)
        if a and b and abs((a-b).total_seconds()) <= hours*3600 and token_set(row['title']) == token_set(observation.title):
            matches.add(row['claim_id'])
    return next(iter(matches)) if len(matches)==1 else None


def _significance(severities, title, category):
    # Compatibility field; the inbox recomputes the full policy with a fixed clock.
    return max([{'unknown':0,'minor':3,'moderate':10,'severe':20,'extreme':30}.get(x,0) for x in severities] or [0])


def recompute_claim(connection: sqlite3.Connection, claim_id: int) -> None:
    rows = connection.execute(
        "SELECT source_type, source_family, severity, title FROM observations WHERE claim_id = ?",
        (claim_id,),
    ).fetchall()
    families = {row["source_family"] for row in rows}
    official = {row["source_family"] for row in rows if row["source_type"] == "official"}
    community = {row["source_family"] for row in rows if row["source_type"] == "community"}
    media = {row["source_family"] for row in rows if row["source_type"] == "media"}
    trusted = any(json.loads(row["raw_json"]).get("_trusted_for_relay") for row in connection.execute("SELECT raw_json FROM observations WHERE claim_id=?", (claim_id,)))
    corroborating = official | media
    if official and len(families) >= 2:
        label, confidence = "CONFIRMED", 90
    elif corroborating and len(corroborating) >= 2:
        label, confidence = "CORROBORATED", 60
    elif official:
        label, confidence = "OFFICIAL-REPORT", 75
    elif trusted:
        label, confidence = "TRUSTED-RELAY", 75
    elif community:
        label, confidence = "UNVERIFIED", 20
    else:
        label, confidence = "REPORTED", 35
    title = rows[0]["title"] if rows else ""
    claim = connection.execute("SELECT category FROM claims WHERE id = ?", (claim_id,)).fetchone()
    significance = _significance([row["severity"] for row in rows], title, claim["category"])
    connection.execute(
        """UPDATE claims SET observation_count=?, independent_families=?, official_families=?,
        confidence_label=?, confidence_score=?, significance_score=? WHERE id=?""",
        (len(rows), len(families), len(official), label, confidence, significance, claim_id),
    )


def record_supersessions(connection, family, raw):
    p=raw.get('properties',{})
    when=p.get('sent') or p.get('effective')
    if not timestamp(when): return
    for reference in p.get('references',[]):
        external=reference.get('identifier')
        if external:
            connection.execute('INSERT OR REPLACE INTO event_supersessions VALUES(?,?,?)',(family,external,iso(timestamp(when))))


def ingest(connection: sqlite3.Connection, observation: Observation) -> tuple[int, bool]:
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
        return existing['claim_id'], bool(cursor.rowcount)
    fingerprint = sha256((observation.source_family+'|'+observation.external_id).encode()).hexdigest()
    claim = connection.execute('SELECT id FROM claims WHERE fingerprint=?',(fingerprint,)).fetchone()
    if not claim:
        claim=connection.execute('SELECT claim_id AS id FROM observations WHERE source_family=? AND external_id=? LIMIT 1',
                                 (observation.source_family,observation.external_id)).fetchone()
    claim_id = claim['id'] if claim else _similar_claim(connection, observation)
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


ALLOWED_TRANSITIONS = {
    "NEW": {"CORRELATING", "REVIEW", "REJECTED"},
    "CORRELATING": {"REVIEW", "REJECTED"},
    "REVIEW": {"TX_CANDIDATE", "REJECTED"},
    "TX_CANDIDATE": {"SENT", "REJECTED", "REVIEW"},
    "SENT": set(),
    "REJECTED": {"REVIEW"},
    "EXPIRED": set(),
    "SUPPRESSED": {"REVIEW","REJECTED"},
}


def transition(connection: sqlite3.Connection, claim_id: int, new_status: str) -> None:
    row = connection.execute("SELECT status FROM claims WHERE id=?", (claim_id,)).fetchone()
    if not row:
        raise ValueError(f"claim {claim_id} not found")
    new_status = new_status.upper()
    if new_status == "TX_CANDIDATE":
        evidence = connection.execute("SELECT source_type,raw_json FROM observations WHERE claim_id=?", (claim_id,)).fetchall()
        if not any(row["source_type"] in {"official", "media"} or json.loads(row["raw_json"]).get("_trusted_for_relay") for row in evidence):
            raise ValueError("TX_CANDIDATE requires at least one official or media source family")
    if new_status not in ALLOWED_TRANSITIONS.get(row["status"], set()):
        raise ValueError(f"invalid transition {row['status']} -> {new_status}")
    connection.execute("UPDATE claims SET status=? WHERE id=?", (new_status, claim_id))
    connection.execute('INSERT INTO workflow_audit(claim_id,previous_status,new_status,reason,changed_at) VALUES(?,?,?,?,?)',
                       (claim_id,row['status'],new_status,'operator',iso(utc_now())))


def evidence_rows(connection, claim_id):
    originals=[dict(row) for row in connection.execute('SELECT * FROM observations WHERE claim_id=? ORDER BY id',(claim_id,))]
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
    return assess(claim,rows,policy,now),rows


def queue(connection, minimum_score=None, limit=50, *, lane='inbox', policy=None, now=None,
          category=None, area=None, status=None, max_age=None, include_suppressed=False):
    now=now or utc_now(); results=[]
    for row in connection.execute('SELECT * FROM claims'):
        item,_=claim_detail(connection,row['id'],policy,now)
        if status and item['status'] != status.upper(): continue
        if not status and item['status'] in {'SENT','REJECTED','EXPIRED','SUPPRESSED'}: continue
        if category and item['category'] != category: continue
        if area and item['area'] != area.upper(): continue
        if lane != 'all' and item['lane'] != lane: continue
        if lane=='now' and (item['expired'] or not item['expires'] or (timestamp(item['onset']) and timestamp(item['onset'])>now)): continue
        if item['reason'] and not include_suppressed: continue
        if minimum_score is not None and item['score']<minimum_score: continue
        if max_age is not None and (not timestamp(item['published']) or (now-timestamp(item['published'])).total_seconds()>max_age*3600): continue
        results.append(item)
    rank={'ETN/WNC':4,'REGIONAL':3,'NATIONAL':2,'GLOBAL':1,'DISTANT':0}
    results.sort(key=lambda r:(rank.get(r['area'],0),r['published'],r['score'],-r['id']),reverse=True)
    return results[:limit]


def housekeeping(connection, policy=None, *, apply=False, suppress=False, now=None):
    changes=[]
    for row in connection.execute("SELECT * FROM claims WHERE status NOT IN ('SENT','REJECTED','EXPIRED','SUPPRESSED')"):
        item,_=claim_detail(connection,row['id'],policy,now)
        target='EXPIRED' if item['expired'] else 'SUPPRESSED' if suppress and item['reason'] and row['status']=='NEW' else None
        if target:
            changes.append({'id':row['id'],'status':target,'reason':item['reason']})
            if apply:
                connection.execute('UPDATE claims SET status=? WHERE id=?',(target,row['id']))
                connection.execute('INSERT INTO workflow_audit(claim_id,previous_status,new_status,reason,changed_at) VALUES(?,?,?,?,?)',
                                   (row['id'],row['status'],target,item['reason'],iso(now or utc_now())))
    return changes
