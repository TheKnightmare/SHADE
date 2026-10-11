"""Local, bounded discovery trace and read-only missed-story diagnosis."""
from datetime import datetime, timezone
from pathlib import Path
import json
from urllib.parse import urlparse, unquote

from .model import token_set


def audit_path(database: str) -> Path:
    return Path(database).with_suffix('.discovery.jsonl')


def append_audit(database: str, records: list[dict]) -> None:
    if not records:
        return
    path = audit_path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 10_000_000:
        path.replace(path.with_suffix('.discovery.previous.jsonl'))
    with path.open('a', encoding='utf-8') as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')


def diagnose(connection, database: str, query: str, policy: dict) -> dict:
    """Distinguish recorded filtering from absence; never invent a miss stage."""
    from .db import claim_detail
    from .relevance import timestamp
    term = query.strip()
    if not term:
        raise ValueError('Provide a headline or URL')
    parsed = urlparse(term)
    is_url = parsed.scheme in {'http', 'https'} and bool(parsed.hostname)
    target = token_set(unquote(parsed.path).replace('-', ' ')) if is_url else token_set(term)
    target_host = (parsed.hostname or '').removeprefix('www.').lower() if is_url else ''
    candidates = []
    cutoff = (datetime.now(timezone.utc).timestamp() - 8 * 86400)
    for row in connection.execute('SELECT claim_id,title,url,source_id,published_at,raw_json FROM observations ORDER BY observed_at DESC'):
        published = timestamp(row['published_at'])
        if published and published.timestamp() < cutoff:
            continue
        overlap = len(target & token_set(row['title'])) / max(1, len(target | token_set(row['title']))) if target else 0
        raw = json.loads(row['raw_json'])
        if is_url and target_host and raw.get('_origin_host') == target_host:
            overlap = min(1, overlap + 0.2)
        if term == row['url'] or (not is_url and term.casefold() in row['title'].casefold()):
            overlap = 1
        if overlap >= 0.4:
            candidates.append((overlap, row))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    if candidates:
        overlap, row = candidates[0]
        claim, _ = claim_detail(connection, row['claim_id'], policy)
        raw = json.loads(row['raw_json'])
        return {
            'stage': 'ingested' if not claim['reason'] and claim['score'] >= policy['minimum_score'] else 'display_filter',
            'match_strength': round(overlap, 2), 'claim_id': row['claim_id'],
            'title': row['title'], 'source_id': row['source_id'], 'published_at': row['published_at'],
            'discovery_query': raw.get('_discovery_query', ''), 'area': claim['area'],
            'score': claim['score'], 'score_breakdown': claim['breakdown'],
            'ghostnet_interest_score': claim['ghostnet_interest_score'], 'novelty_score': claim['novelty_score'],
            'confidence': claim['confidence_label'],
            'reason': claim['reason'] or ('Below display threshold' if claim['score'] < policy['minimum_score'] else ''),
        }
    path = audit_path(database)
    if path.exists():
        for line in reversed(path.read_text(encoding='utf-8').splitlines()):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get('kind') != 'candidate':
                continue
            title = record.get('title', '')
            overlap = len(target & token_set(title)) / max(1, len(target | token_set(title))) if target else 0
            if term == record.get('url') or term.casefold() in title.casefold() or overlap >= 0.4:
                return {'stage': record.get('stage'), 'title': title, 'source_id': record.get('source_id'),
                        'published_at': record.get('published_at'), 'discovery_query': record.get('discovery_query'),
                        'reason': record.get('rejection_reason'), 'score_breakdown': record.get('score_breakdown')}
    polls = [dict(row) for row in connection.execute(
        "SELECT source_id,last_attempt,success,error FROM source_polls WHERE source_id LIKE 'regional-search-%' ORDER BY last_attempt DESC")]
    return {'stage': 'not_observed', 'reason': 'No matching candidate was recorded. Source coverage, search result omission, and article fetch cannot be distinguished without a matching query trace.',
            'recent_search_polls': polls[:12]}
