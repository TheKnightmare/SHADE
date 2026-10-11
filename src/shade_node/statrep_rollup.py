"""Roll up recent CommStat STATREPs into one wire-ready summary (read-only).

SHADE already stores every STATREP it collected (from CommStat's database and from
NODE's on-air logs) as an observation with a `_commstat` block. This reads those,
keeps each operator's latest report in the window, limits it to the chosen area,
and produces counts, concerns, operator notes, a JS8 burst and a longer write-up.
Self-reported amateur traffic: always labelled as such, never treated as confirmed.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOCAL_TIERS = {'ETN/WNC', 'REGIONAL'}
US_STATES = set('AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ '
                'NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR'.split())
ORDER = ('RED', 'YELLOW', 'EVENT', 'UNKNOWN', 'GREEN')


def _when(value: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def split_remarks(remarks: str) -> tuple[str, str]:
    """CommStat operators lead remarks with their state: "AL-Blew thru fast", "TN"."""
    text = ' '.join((remarks or '').split()).strip(' -|,')
    m = re.match(r'^([A-Za-z]{2})(?:\b[\s\-:,/]*|$)(.*)$', text)
    if m and m[1].upper() in US_STATES:
        return m[1].upper(), m[2].strip(' -|,')
    return '', text


def rollup(db_path: str | Path, *, hours: float = 24, scope: str = 'region',
           now: datetime | None = None, area_label: str = '') -> dict:
    now = now or datetime.now(timezone.utc)
    hours = max(1.0, min(float(hours), 72.0))
    cutoff = now - timedelta(hours=hours)
    reports = sorted(latest_reports(db_path, cutoff, scope).values(),
                     key=lambda r: (ORDER.index(r['overall']) if r['overall'] in ORDER else 9, r['state'], r['sender']))
    return _summarise(reports, hours, scope, area_label)


def latest_reports(db_path, cutoff: datetime, scope: str = 'all') -> dict[str, dict]:
    """Each operator's latest STATREP since `cutoff` (read-only)."""
    latest: dict[str, dict] = {}
    with closing(sqlite3.connect(f'file:{Path(db_path).as_posix()}?mode=ro', uri=True, timeout=10)) as db:
        rows = db.execute("SELECT raw_json, published_at FROM observations "
                          "WHERE raw_json LIKE '%\"_commstat\"%' AND published_at >= ?",
                          (cutoff.isoformat().replace('+00:00', 'Z')[:19],)).fetchall()
    for raw_json, published in rows:
        try:
            raw = json.loads(raw_json)
        except ValueError:
            continue
        cs = raw.get('_commstat') or {}
        heard = _when(published)
        if cs.get('kind') != 'statrep' or not cs.get('sender') or heard is None or heard < cutoff:
            continue
        tier = raw.get('_area_tier', '')
        if scope == 'region' and tier not in LOCAL_TIERS:
            continue
        prior = latest.get(cs['sender'])
        if prior is None or heard > prior['heard']:
            state, note = split_remarks(cs.get('remarks', ''))
            codes = cs.get('brevity')
            if codes is None and re.search(r'\b[0-9][A-Z]{7}\b', note or ''):
                from .commstat import brevity_lists, decode_brevity_in   # reports filed before decoding existed
                codes = decode_brevity_in(note, brevity_lists())
            for b in codes or []:                  # CommStat brevity code -> plain words
                plain = b.get('event', '') + (f" ({b['phase']})" if b.get('phase') else '')
                note = note.replace(b['code'], plain) if plain else note
            latest[cs['sender']] = {'sender': cs['sender'], 'grid': cs.get('grid', ''), 'state': state,
                                    'note': note, 'overall': cs.get('overall', 'UNKNOWN'),
                                    'concerns': cs.get('concerns') or [], 'tier': tier, 'heard': heard,
                                    'via': cs.get('via', '')}
    return latest


def _summarise(reports, hours, scope, area_label):
    counts = {k: sum(r['overall'] == k for r in reports) for k in ('GREEN', 'YELLOW', 'RED', 'UNKNOWN')}
    # e.g. "POWER YELLOW (AL)", merged across operators
    concern_places: dict[str, set] = {}
    for r in reports:
        for c in r['concerns']:
            concern_places.setdefault(c.upper(), set()).add(r['state'] or r['grid'][:4])
    concerns = [f"{c} ({'/'.join(sorted(p))})" for c, p in sorted(concern_places.items(),
                key=lambda kv: (0 if 'RED' in kv[0] else 1, kv[0]))]
    quiet = {'NTR', 'NIL', 'NONE', 'N/A', 'NA', 'ALL GOOD', 'OK'}
    notes = [f"{r['state'] or r['grid'][:4]}: {r['note'].rstrip(' .')}" for r in reports
             if r['note'] and r['note'].strip(' .').upper() not in quiet]
    label = (area_label or 'REGION') if scope == 'region' else 'ALL AREAS'
    window = f'{hours:g}H'
    if not reports:
        burst = f'STATREPS {label} LAST {window}: NONE RECEIVED.'
    else:
        burst = (f"STATREPS {label} LAST {window}: {len(reports)} STATIONS - {counts['GREEN']} GREEN "
                 f"{counts['YELLOW']} YELLOW {counts['RED']} RED"
                 + (f" {counts['UNKNOWN']} UNK" if counts['UNKNOWN'] else '') + '.')
        if concerns:
            burst += ' ' + ', '.join(concerns[:6]) + '.'
        if notes:
            burst += ' NOTES: ' + '; '.join(notes[:4]).upper() + '.'
        burst += ' SELF-REPORTED, UNVERIFIED.'
    states = sorted({r['state'] for r in reports if r['state']})
    lines = [f"{len(reports)} stations reported in the last {hours:g} hours"
             + (f" ({', '.join(states)})" if states else '') + ': '
             + ', '.join(f"{v} {k.lower()}" for k, v in counts.items() if v) + '.' if reports
             else f'No STATREPs received in the last {hours:g} hours.']
    for r in reports:
        if r['overall'] != 'GREEN' or r['note']:
            detail = ', '.join(c.title() for c in r['concerns']) or r['overall'].title()
            lines.append(f"{r['sender']} ({r['state'] or r['grid']}): {detail}"
                         + (f" - {r['note']}" if r['note'] else ''))
    lines.append('Self-reported by amateur radio operators via CommStat; not independently verified.')
    return {'hours': hours, 'scope': scope, 'label': label, 'stations': len(reports), 'counts': counts,
            'concerns': concerns, 'notes': notes, 'states': states, 'burst': burst,
            'writeup': '\n'.join(lines),
            'reports': [{k: (v.isoformat().replace('+00:00', 'Z') if isinstance(v, datetime) else v)
                         for k, v in r.items()} for r in reports]}


# --------------------------------------------------------------------------- field corroboration
TOPICS = {
    'POWER': r'power|outage|electric|blackout|utility|grid',
    'WATER': r'water|boil|flood|dam\b|sewer',
    'MEDICAL': r'hospital|medical|\bems\b|ambulance|health',
    'COMMS': r'\bcell|phone|911|radio|tower|communicat',
    'TRAVEL': r'road|highway|interstate|\bi-\d+|bridge|closure|closed|traffic|crash|travel',
    'INTERNET': r'internet|broadband|fiber|\bisp\b',
    'FUEL': r'fuel|gasoline|gas station|diesel|propane',
    'FOOD': r'food|grocery|groceries|shelter|supplies',
    'CRIME': r'shoot|crime|police|arrest|murder|robber|stabb|homicide',
    'CIVIL': r'protest|unrest|riot|evacuat|curfew',
    'WEATHER': r'storm|hurricane|tropical|tornado|flood|wind|rain|snow|\bice\b|weather|isaias',
}


def field_matches(reports: dict[str, dict], *, title: str, location: str = '', area: str = '',
                  category: str = '') -> list[dict]:
    """STATREPs whose yellow/red concerns line up with this report's topic and place.
    Place: same state, or both local to this station when the report names no state."""
    from .gazetteer import state_mentions
    text = f'{title} {category}'
    topics = {t for t, rx in TOPICS.items() if re.search(rx, text, re.I)}
    if not topics:
        return []
    states = set(state_mentions(f'{title} {location}'))
    out = []
    for r in reports.values():
        hits = [c for c in r['concerns'] if c.split()[0].upper() in topics]
        if not hits:
            continue
        same_state = r['state'] and r['state'] in states
        both_local = not states and area in LOCAL_TIERS and r['tier'] == 'ETN/WNC'
        if same_state or both_local:
            out.append({'sender': r['sender'], 'place': r['state'] or r['grid'], 'concerns': hits,
                        'note': r['note'], 'heard': r['heard'].isoformat().replace('+00:00', 'Z')})
    return sorted(out, key=lambda m: m['heard'], reverse=True)[:5]


def describe(matches: list[dict]) -> str:
    if not matches:
        return ''
    parts = [f"{m['sender']} ({m['place']}) {', '.join(c.title() for c in m['concerns'])}" for m in matches[:3]]
    more = f' +{len(matches) - 3} more' if len(matches) > 3 else ''
    return f"Field reports agree: {len(matches)} STATREP(s) - " + '; '.join(parts) + more + ' (self-reported)'
