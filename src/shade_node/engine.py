from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import tomllib
from importlib import resources
from .relevance import DEFAULT_POLICY, timestamp, iso
from .model import utc_now

from .collectors import CollectionError, collect
from .db import connect, ingest
from .model import SourceConfig


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
    local_path=config_path.with_name('config.local.toml')
    if local_path.exists() and local_path != config_path:
        with local_path.open('rb') as handle: local=tomllib.load(handle)
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
    sources = [SourceConfig(**item) for item in payload.get("sources", [])]
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
    )


def active_sources(settings: Settings, mode: str = "standard") -> list[SourceConfig]:
    mode = normalize_mode(mode)
    return [
        source
        for source in settings.sources
        if source.enabled and (mode != "standard" or not source.emcomm_only)
    ]


def run_once(settings: Settings, mode: str | None = None) -> dict:
    mode = normalize_mode(mode or settings.operating_mode)
    result = {
        "mode": mode,
        "sources_selected": 0,
        "sources_ok": 0,
        "sources_failed": 0,
        "seen": 0,
        "inserted": 0,
        "errors": [],
    }
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.max_age_days)
    selected = active_sources(settings, mode)
    with connect(settings.database) as connection:
        previous={row['source_id']:dict(row) for row in connection.execute('SELECT * FROM source_polls')}
    enabled=[s for s in selected if not previous.get(s.id) or timestamp(previous[s.id]['next_allowed'])<=utc_now()]
    result['sources_skipped_cooldown']=len(selected)-len(enabled)
    result["sources_selected"] = len(enabled)
    collected: dict[str, tuple[SourceConfig, list]] = {}
    backoff = {}
    workers = max(1, min(8, len(enabled)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="shade") as pool:
        futures = {
            pool.submit(
                collect,
                source,
                user_agent=settings.user_agent,
                timeout=settings.timeout,
                max_bytes=settings.max_bytes,
            ): source
            for source in enabled
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                collected[source.id] = (source, future.result())
                result["sources_ok"] += 1
            except (CollectionError, ValueError, TypeError, KeyError) as exc:
                backoff[source.id]=getattr(exc,"retry_after",0)
                result["sources_failed"] += 1
                result["errors"].append(f"{source.id}: {exc}")
    with connect(settings.database) as connection:
        for source in enabled:
            ok=source.id in collected
            connection.execute('INSERT OR REPLACE INTO source_polls(source_id,last_attempt,next_allowed,success,error) VALUES(?,?,?,?,?)',
                               (source.id,iso(utc_now()),iso(utc_now()+timedelta(seconds=source.min_poll_seconds if ok else max(1800,source.min_poll_seconds,backoff.get(source.id,0)))),int(ok),'' if ok else 'Collection failed; see run output'))
        for configured_source in enabled:
            batch = collected.get(configured_source.id)
            if batch is None:
                continue
            _, observations = batch
            if configured_source.kind in {'faa_status','status_api','tdot_events'}:
                connection.execute('UPDATE source_presence SET active=0 WHERE source_id=?',(configured_source.id,))
                for item in observations:
                    connection.execute('INSERT OR REPLACE INTO source_presence(source_id,external_id,asof,active) VALUES(?,?,?,1)',
                                       (item.source_id,item.external_id,iso(utc_now())))
            for observation in observations:
                if configured_source.trusted_for_relay:
                    observation.raw['_trusted_for_relay'] = True
                try:
                    published = datetime.fromisoformat(observation.published_at.replace("Z", "+00:00"))
                except ValueError:
                    published = datetime.now(timezone.utc)
                if published < cutoff:
                    continue
                result["seen"] += 1
                _, inserted = ingest(connection, observation)
                result["inserted"] += int(inserted)
    return result
