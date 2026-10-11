"""Read CommStat STATREPs and alerts: on-air copies NODE logged, and CommStat's own database.

CommStat (AmRRON's JS8Call companion) exchanges structured status reports. This
module is an independent reader of the documented message layout; it contains no
CommStat code. The wire layout is:

    STATREP  SENDER: @GROUP ,GRID,SCOPE,ID,STATUS,REMARKS,{&%}     ({&%3} internet, {F%} forwarded)
    ALERT    SENDER: @GROUP ,ALERT_ID,COLOR,TITLE,MESSAGE,{%%}     (older: no ALERT_ID; "LRT" prefix)

STATUS is twelve digits in a fixed category order (or "+" for all green):
1 green, 2 yellow, 3 red, 4 unknown. Alert colour: 1 yellow, 2 orange, 3 red, 4 black.

Everything here is self-reported amateur traffic: community-sourced, unverified.
Read-only: CommStat's database is opened with mode=ro and is never written.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .mcforms import area_tier, maidenhead_center
from .model import Observation

CATEGORIES = ('Overall', 'Power', 'Water', 'Medical', 'Comms', 'Travel', 'Internet',
              'Fuel', 'Food', 'Crime', 'Civil', 'Weather')
DB_COLUMNS = ('map', 'power', 'water', 'med', 'telecom', 'travel', 'internet',
              'fuel', 'food', 'crime', 'civil', 'political')      # CommStat's statrep columns, same order
STATUS = {'1': 'GREEN', '2': 'YELLOW', '3': 'RED', '4': 'UNKNOWN', '6': 'EVENT'}
RANK = {'RED': 3, 'YELLOW': 2, 'EVENT': 2, 'UNKNOWN': 0, 'GREEN': 0}
SCOPE = {'1': 'operator QTH', '2': 'community', '3': 'county', '4': 'region', '5': 'other location'}
ALERT_COLOR = {'1': 'YELLOW', '2': 'ORANGE', '3': 'RED', '4': 'BLACK'}
SEVERITY = {'RED': 'severe', 'BLACK': 'extreme', 'ORANGE': 'severe', 'YELLOW': 'moderate', 'GREEN': 'minor'}
DAYS_READ = 3

_STATREP = re.compile(
    r'(?:(?P<sender>[A-Z0-9/]{3,12}):\s*)?(?P<target>@?[A-Z0-9/]{2,16})\s*,'
    r'(?P<grid>[A-R]{2}\d{2}(?:[A-X]{2})?)\s*,(?P<scope>\d)\s*,(?P<id>[A-Z0-9]{1,8})\s*,'
    r'(?P<status>\+|[0-9]{12})\s*,(?P<remarks>.*?),?\s*\{(?P<marker>&%3?|F%)\}', re.I)
_ALERT = re.compile(
    r'(?:(?P<sender>[A-Z0-9/]{3,12}):\s*)?(?P<target>@?[A-Z0-9/]{2,16})\s*(?:LRT\s*)?,'
    r'(?:(?P<id>[A-Z0-9]{1,8})\s*,)?(?P<color>[1-4])\s*,(?P<title>[^,]{0,80}),(?P<message>.*?),?\s*\{%%3?\}', re.I)


def decode_statrep(text: str) -> dict | None:
    m = _STATREP.search(text or '')
    if not m:
        return None
    digits = '1' * 12 if m['status'] == '+' else m['status']
    statuses = {cat: STATUS.get(d, 'UNKNOWN') for cat, d in zip(CATEGORIES, digits)}
    return _statrep(m['sender'], m['target'], m['grid'], m['scope'], m['id'], statuses,
                    m['remarks'], {'&%': 'radio', '&%3': 'internet', 'F%': 'forwarded'}[m['marker']])


def _statrep(sender, target, grid, scope, sr_id, statuses, remarks, via):
    worst = max((s for c, s in statuses.items()), key=lambda s: RANK.get(s, 0), default='GREEN')
    concerns = [f'{c} {s}' for c, s in statuses.items() if c != 'Overall' and s in {'RED', 'YELLOW', 'EVENT'}]
    overall = statuses.get('Overall', 'UNKNOWN')
    if RANK.get(worst, 0) > RANK.get(overall, 0):
        overall = worst
    remarks = re.sub(r'\s*\|\|\s*', '\n', (remarks or '').strip()).strip(' ,')
    return {'kind': 'statrep', 'sender': (sender or '').upper(), 'target': (target or '').upper(),
            'grid': (grid or '').upper(), 'scope': SCOPE.get(str(scope), str(scope or '')), 'id': (sr_id or '').upper(),
            'statuses': statuses, 'overall': overall, 'concerns': concerns, 'remarks': remarks, 'via': via}


def decode_alert(text: str) -> dict | None:
    m = _ALERT.search(text or '')
    if not m or '{&%' in text:
        return None
    return {'kind': 'alert', 'sender': (m['sender'] or '').upper(), 'target': (m['target'] or '').upper(),
            'id': (m['id'] or '').upper(), 'color': ALERT_COLOR[m['color']], 'title': m['title'].strip(),
            'message': re.sub(r'\s*\|\|\s*', '\n', m['message'].strip()), 'via': 'internet' if '{%%3}' in text else 'radio'}


def headline(d: dict) -> str:
    if d['kind'] == 'alert':
        return f"COMMSTAT {d['color']} ALERT from {d['sender'] or 'unknown'}: {d['title'] or 'untitled'}"
    where = d['grid'] + (f" ({d['scope']})" if d['scope'] else '')
    if d['concerns']:
        return f"STATREP {d['sender']} {where}: {d['overall']} - {', '.join(d['concerns'][:4])}"
    return f"STATREP {d['sender']} {where}: ALL GREEN" if d['overall'] == 'GREEN' else \
        f"STATREP {d['sender']} {where}: {d['overall']}"


def observation(source, d: dict, *, heard: datetime, origin: str, extra: dict | None = None) -> Observation:
    """One SHADE observation for a decoded STATREP or alert."""
    stamp = heard.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    lines = [f"{d['sender'] or 'Unknown sender'} to {d['target'] or '?'} via {d['via']}; {origin}.",
             'CommStat traffic: self-reported by an amateur radio operator. Not independently verified.']
    raw = {'_commstat': {k: v for k, v in d.items() if k != 'kind'} | {'kind': d['kind']},
           **(extra or {}), 'status': 'actual', '_area': ''}
    codes = decode_brevity_in(f"{d.get('remarks', '')} {d.get('message', '')} {d.get('title', '')}",
                              brevity_lists(getattr(source, 'brevity_dir', None) or _commstat_dir(source)))
    if codes:
        d = {**d, 'brevity': [{k: c[k] for k in ('code', 'list', 'event', 'phase', 'severity', 'impact', 'text')}
                              for c in codes]}
        raw['_commstat'] = {k: v for k, v in d.items() if k != 'kind'} | {'kind': d['kind']}
    if d['kind'] == 'statrep':
        lines.append(f"Scope: {d['scope'] or 'not stated'}. Report ID {d['id']}.")
        lines += [f'{c}: {s}' for c, s in d['statuses'].items()]
        if d['remarks']:
            lines.append(f"Remarks: {d['remarks']}")
        key = '|'.join(('statrep', d['sender'], d['id'], d['grid']))
        color = d['overall']
        routine = color == 'GREEN' and not d['remarks']
        category = 'chatter' if routine else 'infrastructure'
        grid = d['grid']
    else:
        lines += [f"Alert colour: {d['color']}", f"Title: {d['title']}", d['message']]
        key = '|'.join(('alert', d['sender'], d['id'] or d['title'][:40]))
        color, category, grid = d['color'], 'infrastructure', ''
    for c in codes:
        lines.append(f"Brevity code {c['text']}")
    tier = area_tier(grid, getattr(source, 'home_grid', '')) if grid else ''
    if tier:
        raw['_area_tier'] = tier
    center = maidenhead_center(grid) if grid else None
    if center:
        raw['geometry'] = {'type': 'Point', 'coordinates': [center[1], center[0]]}
    title = headline(d) + (f" [{codes[0]['event']}" + (f", {codes[0]['phase']}" if codes[0]['phase'] else '') + ']'
                           if codes else '')
    return Observation(source.id, source.name, source.source_type, source.source_family, key,
                       title, '\n'.join(x for x in lines if x), '', category=category,
                       location=grid, severity=SEVERITY.get(color, 'unknown'),
                       published_at=stamp, observed_at=stamp, raw=raw)


def _commstat_dir(source) -> Path | None:
    """The CommStat folder for brevity lists: the commstat_db source's own path, else discovered."""
    if getattr(source, 'kind', '') == 'commstat_db' and getattr(source, 'url', ''):
        path = Path(source.url)
        return path if path.is_dir() else path.parent
    db = find_database()
    return db.parent if db else None


def find_database(configured: str = '') -> Path | None:
    """CommStat keeps traffic.db3 beside its program files. Use the configured path,
    else look for an install under C:\\Station\\Apps or the user's home."""
    if configured:
        path = Path(configured)
        path = path / 'traffic.db3' if path.is_dir() else path
        return path if path.is_file() else None
    roots = [Path(r'C:\Station\Apps'), Path.home(), Path.home() / 'Documents', Path.home() / 'Desktop']
    for root in roots:
        try:
            for candidate in sorted(root.glob('CommStat*/traffic.db3'), reverse=True):
                return candidate
        except OSError:
            continue
    return None


def _parse_local(value: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)   # CommStat stores UTC


def collect_commstat_db(source, now: datetime | None = None) -> list[Observation]:
    """Read STATREPs and alerts from CommStat's own database (radio and internet)."""
    from .collectors import CollectionError
    now = now or datetime.now(timezone.utc)
    db = find_database(source.url)
    if db is None:
        raise CollectionError('CommStat database (traffic.db3) not found; run CommStat once, or set its path')
    since = (now - timedelta(days=DAYS_READ)).strftime('%Y-%m-%d')
    out = []
    try:
        with closing(sqlite3.connect(f'file:{db.as_posix()}?mode=ro', uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'statrep' in tables:
                for r in conn.execute('SELECT * FROM statrep WHERE COALESCE(date, datetime) >= ? ORDER BY id', (since,)):
                    statuses = {cat: STATUS.get(str(r[col] or '').strip()[:1], 'UNKNOWN')
                                for cat, col in zip(CATEGORIES, DB_COLUMNS)}
                    d = _statrep(r['from_callsign'], r['target'], r['grid'], _scope_code(r['scope']), r['sr_id'],
                                 statuses, r['comments'] or '', 'internet' if r['source'] in (2, 3) else 'radio')
                    out.append(observation(source, d, heard=_parse_local(r['datetime']) or now,
                                           origin='from CommStat on this station', extra={'commstat_row': r['id']}))
            if 'alerts' in tables:
                for r in conn.execute('SELECT * FROM alerts WHERE COALESCE(date, datetime) >= ? ORDER BY id', (since,)):
                    d = {'kind': 'alert', 'sender': (r['from_callsign'] or '').upper(), 'target': (r['target'] or '').upper(),
                         'id': (r['alert_id'] or '').upper(), 'color': ALERT_COLOR.get(str(r['color']), 'YELLOW'),
                         'title': (r['title'] or '').strip(), 'message': (r['message'] or '').strip(),
                         'via': 'internet' if r['source'] in (2, 3) else 'radio'}
                    out.append(observation(source, d, heard=_parse_local(r['datetime']) or now,
                                           origin='from CommStat on this station', extra={'commstat_alert_row': r['id']}))
    except sqlite3.Error as exc:
        raise CollectionError(f'CommStat database could not be read: {exc}') from exc
    return out


def _scope_code(value) -> str:
    text = str(value or '').strip()
    if text.isdigit():
        return text
    lowered = text.lower()
    for code, words in {'1': ('qth', 'my location'), '2': ('community',), '3': ('county',),
                        '4': ('region',), '5': ('other',)}.items():
        if any(w in lowered for w in words):
            return code
    return text


# --------------------------------------------------------------------------- brevity codes
# CommStat's brevity code: list digit + 7 letters, in wire order
# Event, Phase, Severity, Impact, Official response, Source, Station status.
# The meanings live in CommStat's own list files ("1-Natural Events_V2.0.json" ...);
# SHADE reads those files (data only) from the CommStat install.
_BREVITY = re.compile(r'(?<![A-Z0-9])([0-9][A-Z]{7})(?![A-Z0-9])')
_LISTS: dict[str, dict] = {}


def _letters(section) -> dict:
    out = {}
    if isinstance(section, dict):
        for key, val in section.items():
            if re.fullmatch(r'[A-Z]', str(key)) and isinstance(val, dict) and 'name' in val:
                out[key] = val
            elif str(key).startswith('***') and isinstance(val, dict):
                out.update(_letters(val))
                out.update(_letters(val.get('items')))
    return out


def brevity_lists(folder: Path | None = None) -> dict[str, dict]:
    """{list digit: {'title': ..., 'data': json}} from a CommStat folder (cached)."""
    folder = Path(folder) if folder else (find_database().parent if find_database() else None)
    if folder is None or not folder.is_dir():
        return {}
    key = str(folder)
    if key not in _LISTS:
        lists = {}
        for path in sorted(folder.glob('[0-9]-*.json')):
            try:
                import json
                data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and 'emergency_type' in data and path.name[0] not in lists:
                title = re.sub(r'_V[\d.]+$', '', path.stem[2:]).strip()
                lists[path.name[0]] = {'title': title, 'data': data}
        _LISTS[key] = lists
    return _LISTS[key]


def decode_brevity(code: str, lists: dict[str, dict]) -> dict | None:
    """One brevity code -> its parts and a short plain-English line, or None."""
    code = (code or '').strip().upper()
    if not re.fullmatch(r'[0-9][A-Z]{7}', code) or code[0] not in lists:
        return None
    data, title = lists[code[0]]['data'], lists[code[0]]['title']
    event, phase, severity, impact, response, source, station = code[1:]
    overrides = data.get('impact_overrides') or {}
    impacts = overrides.get(event, {}) if isinstance(overrides.get(event), dict) else {}
    impacts = impacts.get('impacts', impacts) or data.get('shared_impacts', {})
    sections = (('event', data.get('emergency_type'), event), ('phase', data.get('status_codes'), phase),
                ('severity', data.get('severity') or data.get('public_reaction'), severity),
                ('impact', impacts, impact),
                ('response', data.get('official_response') or data.get('shared_impacts'), response),
                ('source', data.get('information_codes'), source), ('station', data.get('station_response'), station))
    parts = {}
    for name, section, letter in sections:
        entry = _letters(section).get(letter) or {}
        label = str(entry.get('name') or '').strip()
        parts[name] = '' if label.lower() in {'', 'unknown', 'reserved', 'unspecified', 'none offered'} else label
    parts['event'] = parts['event'] or 'Unspecified event'
    bits = [parts['event']] + [parts[k] for k in ('phase', 'severity', 'impact', 'response') if parts[k]]
    line = f"{code} = {title}: " + '; '.join(bits)
    if parts['source']:
        line += f" (source: {parts['source']})"
    return {'code': code, 'list': title, **parts, 'text': line}


def decode_brevity_in(text: str, lists: dict[str, dict]) -> list[dict]:
    found, seen = [], set()
    for m in _BREVITY.finditer((text or '').upper()):
        if m[1] not in seen:
            seen.add(m[1])
            d = decode_brevity(m[1], lists)
            if d:
                found.append(d)
    return found
