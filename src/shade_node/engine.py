from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import json
import tomllib
from importlib import resources
from .relevance import DEFAULT_POLICY, timestamp, iso, evaluate
from .model import utc_now

from .collectors import CollectionError, PartialCollectionError, collect
from .db import connect, ingest
from .model import SourceConfig
from .keywords import load_keywords, normalize_keywords
from .chokepoints import load_chokepoints, tag_chokepoints
from .discovery_audit import append_audit
from urllib.parse import urlparse


@dataclass(slots=True)
class Settings:
    database: str
    max_age_days: int
    timeout: int
    max_bytes: int
    user_agent: str
    callsign: str
    network: str
    region_label: str
    max_message_chars: int
    operating_mode: str
    standard_min_queue_score: int
    emcomm_min_queue_score: int
    emcomm_network: str
    emcomm_region_label: str
    emcomm_max_message_chars: int
    sources: list[SourceConfig]
    policy: dict = field(default_factory=dict)
    keywords: dict[str, list[str]] = field(default_factory=dict)
    chokepoints: list[dict] = field(default_factory=list)


VALID_MODES = {"standard", "exercise", "actual"}


def normalize_mode(value: str | None) -> str:
    mode = (value or "standard").strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"invalid operating mode {value!r}; choose standard, exercise, or actual")
    return mode


def load_settings(path: str) -> Settings:
    config_path = Path(path)
    with config_path.open("rb") as handle:
        payload = tomllib.load(handle)
    keyword_path = config_path.parent / "keywords.toml"
    if not keyword_path.exists():
        keyword_path = Path(__file__).resolve().parents[2] / "keywords.toml"
    if not keyword_path.exists():
        packaged_keywords = resources.files('shade_node').joinpath('keywords.toml')
        if packaged_keywords.is_file():
            keywords = normalize_keywords(tomllib.loads(packaged_keywords.read_text(encoding='utf-8')))
        else:
            raise ValueError(f"keywords.toml not found beside {config_path} or repository root")
    else:
        keywords = load_keywords(keyword_path)
    local_path=config_path.with_name('config.local.toml')
    switches={}
    if local_path.exists() and local_path != config_path:
        with local_path.open('rb') as handle: local=tomllib.load(handle)
        switches=local.get('source_overrides',{}) if isinstance(local.get('source_overrides'),dict) else {}
        # Additive local source/policy tuning; station identity stays in config.toml.
        payload.setdefault('sources',[]).extend(local.get('sources',[]))
        if 'user_agent' in local.get('collection',{}):
            payload.setdefault('collection',{})['user_agent']=local['collection']['user_agent']
        for section in ('relevance','source_packs'):
            payload.setdefault(section,{}).update(local.get(section,{}))
    packs=payload.get('source_packs',{}).get('enabled',[])
    if packs:
        catalog=tomllib.loads(resources.files('shade_node').joinpath('source_packs.toml').read_text(encoding='utf-8'))
        known={s['pack'] for s in catalog['sources']}
        if set(packs)-known: raise ValueError('Unknown source pack')
        payload.setdefault('sources',[]).extend(s for s in catalog['sources'] if s['pack'] in packs)
    ids=[item['id'] for item in payload.get('sources',[])]
    if len(ids)!=len(set(ids)): raise ValueError('Duplicate source IDs')
    policy={**DEFAULT_POLICY,**payload.get('relevance',{})}
    for key in ('minimum_score','max_age_hours','global_quake_magnitude','regional_quake_magnitude','domestic_quake_magnitude'):
        if not isinstance(policy[key],(int,float)) or policy[key]<0: raise ValueError('Invalid relevance setting: '+key)
    operator = payload.get("operator", {})
    collection = payload.get("collection", {})
    operation = payload.get("operation", {})
    emcomm = payload.get("emcomm", {})
    source_items=[]
    for configured in payload.get('sources',[]):
        item=dict(configured)
        # Safe upgrade path: the former bypass flag becomes display-only.
        if 'trusted_for_relay' in item:
            item.setdefault('high_credibility',bool(item.pop('trusted_for_relay')))
        source_items.append(item)
    sources = [SourceConfig(**item) for item in source_items]
    # Operator on/off switches from the web console (config.local.toml) win over defaults.
    off, on = set(switches.get('disabled') or []), set(switches.get('enabled') or [])
    for source in sources:
        if source.id in off:
            source.enabled = False
        elif source.id in on:
            source.enabled = True
    origin_families = {
        (urlparse(item.url).hostname or '').removeprefix('www.').lower(): item.source_family
        for item in sources if item.kind == 'rss' and item.source_type == 'media'
    }
    for source in sources:
        source.keywords = keywords
        if source.kind == 'google_news_search':
            source.origin_families = origin_families
    database = str(collection.get("database", "data/shade.db"))
    if not Path(database).is_absolute():
        database = str((config_path.parent / database).resolve())
    return Settings(
        database=database,
        max_age_days=int(collection.get("max_age_days", 7)),
        timeout=int(collection.get("request_timeout_seconds", 20)),
        max_bytes=int(collection.get("max_response_bytes", 5_000_000)),
        user_agent=str(collection.get("user_agent", "SHADE/0.3 (set-a-real-contact@example.invalid)")),
        callsign=str(operator.get("callsign", "SET-ME")),
        network=str(operator.get("network", "GHOSTNET")),
        region_label=str(operator.get("region_label", "SET-REGION")),
        max_message_chars=int(operator.get("max_message_chars", 500)),
        operating_mode=normalize_mode(str(operation.get("mode", "standard"))),
        standard_min_queue_score=int(operation.get("minimum_queue_score", 35)),
        emcomm_min_queue_score=int(emcomm.get("minimum_queue_score", 10)),
        emcomm_network=str(emcomm.get("network", operator.get("network", "GHOSTNET"))),
        emcomm_region_label=str(emcomm.get("region_label", operator.get("region_label", "SET-REGION"))),
        emcomm_max_message_chars=int(emcomm.get("max_message_chars", operator.get("max_message_chars", 500))),
        sources=sources,
        policy=policy,
        keywords=keywords,
        chokepoints=load_chokepoints(config_path.parent / "chokepoints.toml"),
    )


def active_sources(settings: Settings, mode: str = "standard") -> list[SourceConfig]:
    mode = normalize_mode(mode)
    return [
        source
        for source in settings.sources
        if source.enabled and (mode != "standard" or not source.emcomm_only)
    ]


def cooldown_budget(source: SourceConfig) -> str:
    """Return the persistent upstream budget shared by related sources."""
    if source.kind == 'telegram_preview':
        return 'telegram-preview:global'
    if source.kind == 'mastodon_hashtag':
        instance = (source.instance or urlparse(source.url).hostname or '').lower()
        return 'mastodon-instance:' + instance
    return 'source:' + source.id


def run_once(settings: Settings, mode: str | None = None) -> dict:
    mode = normalize_mode(mode or settings.operating_mode)
    result = {
        "mode": mode,
        "sources_selected": 0,
        "sources_ok": 0,
        "sources_failed": 0,
        "sources_partial": 0,
        "seen": 0,
        "inserted": 0,
        "errors": [],
    }
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.max_age_days)
    selected = active_sources(settings, mode)
    with connect(settings.database) as connection:
        previous={row['source_id']:dict(row) for row in connection.execute('SELECT * FROM source_polls')}
        cache={row['source_id']:dict(row) for row in connection.execute('SELECT * FROM source_http_cache')}
    now = utc_now()
    by_budget = {}
    for source in selected:
        by_budget.setdefault(cooldown_budget(source), []).append(source)
    enabled = []
    for sources in by_budget.values():
        group_next = [timestamp(previous[s.id]['next_allowed']) for s in sources if previous.get(s.id)]
        if any(value and value > now for value in group_next):
            continue
        due = [s for s in sources if not previous.get(s.id) or timestamp(previous[s.id]['next_allowed']) <= now]
        if due:
            # Shared budgets poll one source per run, oldest first, so large
            # Telegram and per-instance Mastodon sets advance fairly.
            enabled.append(min(due, key=lambda s: previous.get(s.id, {}).get('last_attempt', '')))
    result['sources_skipped_cooldown']=len(selected)-len(enabled)
    result["sources_selected"] = len(enabled)
    collected: dict[str, tuple[SourceConfig, list]] = {}
    failures = {}
    response_meta = {source.id: {} for source in enabled}
    workers = max(1, min(8, len(enabled)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="shade") as pool:
        futures = {
            pool.submit(
                collect,
                source,
                user_agent=settings.user_agent,
                timeout=settings.timeout,
                max_bytes=settings.max_bytes,
                conditional=cache.get(source.id),
                response_meta=response_meta[source.id],
            ): source
            for source in enabled
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                collected[source.id] = (source, future.result())
                result["sources_ok"] += 1
            except PartialCollectionError as exc:
                collected[source.id] = (source, exc.observations)
                failures[source.id] = (str(exc), 0)
                result['sources_partial'] += 1
                result['errors'].append(f'{source.id}: {exc}')
            except (CollectionError, ValueError, TypeError, KeyError) as exc:
                failures[source.id]=(str(exc),getattr(exc,"retry_after",0))
                result["sources_failed"] += 1
                result["errors"].append(f"{source.id}: {exc}")
    audit_records = []
    with connect(settings.database) as connection:
        for source in enabled:
            ok=source.id in collected and source.id not in failures
            error,retry_after=failures.get(source.id,('',0))
            connection.execute('INSERT OR REPLACE INTO source_polls(source_id,last_attempt,next_allowed,success,error) VALUES(?,?,?,?,?)',
                               (source.id,iso(utc_now()),iso(utc_now()+timedelta(seconds=source.min_poll_seconds if ok else max(1800,source.min_poll_seconds,retry_after))),int(ok),error))
            meta=response_meta[source.id]
            for attempt in meta.get('query_attempts', []):
                audit_records.append({'kind': 'query', 'at': iso(utc_now()), 'source_id': source.id, **attempt})
            if ok and (meta.get('etag') or meta.get('last_modified')):
                connection.execute('INSERT OR REPLACE INTO source_http_cache(source_id,etag,last_modified) VALUES(?,?,?)',
                                   (source.id,meta.get('etag',''),meta.get('last_modified','')))
        for configured_source in enabled:
            batch = collected.get(configured_source.id)
            if batch is None:
                continue
            _, observations = batch
            if response_meta[configured_source.id].get('not_modified'):
                continue
            if configured_source.kind in {'faa_status','status_api','tdot_events'}:
                connection.execute('UPDATE source_presence SET active=0 WHERE source_id=?',(configured_source.id,))
                for item in observations:
                    connection.execute('INSERT OR REPLACE INTO source_presence(source_id,external_id,asof,active) VALUES(?,?,?,1)',
                                       (item.source_id,item.external_id,iso(utc_now())))
            for observation in observations:
                tag_chokepoints(observation, settings.chokepoints)
                if configured_source.high_credibility:
                    observation.raw['_high_credibility_source'] = True
                try:
                    published = datetime.fromisoformat(observation.published_at.replace("Z", "+00:00"))
                except ValueError:
                    published = datetime.now(timezone.utc)
                if configured_source.kind == 'google_news_search':
                    preliminary = evaluate({
                        'raw_json': observation.raw_json(), 'category': observation.category,
                        'source_family': observation.source_family, 'source_type': observation.source_type,
                        'title': observation.title, 'body': observation.body,
                        'location': observation.location, 'severity': observation.severity,
                        'published_at': observation.published_at, 'observed_at': observation.observed_at,
                    }, settings.policy)
                    rejection = ('Outside collection freshness window' if published < cutoff else
                                 preliminary['reason'] or ('Below display threshold' if preliminary['score'] < settings.policy['minimum_score'] else ''))
                    audit_records.append({
                        'kind': 'candidate', 'at': iso(utc_now()), 'source_id': observation.source_id,
                        'source_family': observation.source_family, 'discovery_query': observation.raw.get('_discovery_query', ''),
                        'url': observation.url, 'title': observation.title, 'published_at': observation.published_at,
                        'event_at': observation.raw.get('_event_at', ''), 'location': observation.location,
                        'area': preliminary['area'], 'locality_score': preliminary['locality_score'],
                        'freshness_score': preliminary['breakdown']['freshness'],
                        'ghostnet_interest_score': preliminary['ghostnet_interest_score'],
                        'novelty_score': preliminary['novelty_score'], 'reliability_score': 35,
                        'score': preliminary['score'], 'score_breakdown': preliminary['breakdown'],
                        'stage': 'collection_freshness' if published < cutoff else ('display_filter' if rejection else 'ingested'),
                        'accepted': not rejection, 'rejection_reason': rejection,
                    })
                if published < cutoff:
                    continue
                result["seen"] += 1
                _, inserted = ingest(connection, observation)
                result["inserted"] += int(inserted)
    try:
        append_audit(settings.database, audit_records)
    except OSError as exc:
        result['errors'].append(f'discovery audit: {exc}')
    return result
