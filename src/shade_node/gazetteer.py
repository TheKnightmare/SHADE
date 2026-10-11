"""Conservative state-aware location extraction from the 2026 Census Gazetteer."""
from __future__ import annotations
from functools import lru_cache
from importlib import resources
import csv
import re

STATE_NAMES = {'AL': 'Alabama', 'GA': 'Georgia', 'KY': 'Kentucky',
               'NC': 'North Carolina', 'TN': 'Tennessee', 'WV': 'West Virginia'}
STATE_PATTERN = re.compile(r'(?<![A-Za-z])(?:' +
    '|'.join(re.escape(x) for x in sorted(list(STATE_NAMES.values()) + list(STATE_NAMES), key=len, reverse=True)) +
    r')(?![A-Za-z])', re.I)

@lru_cache(maxsize=1)
def data():
    with resources.files('shade_node').joinpath('assets/gazetteer_2026.csv').open(encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    index = {}
    for row in rows:
        index.setdefault((row['kind'], row['name'].casefold()), set()).add(row['state'])
    places = sorted((name for kind, name in index if kind == 'place' and len(name) >= 4), key=len, reverse=True)
    pattern = re.compile(r'(?<![A-Za-z])(' + '|'.join(re.escape(x) for x in places) +
                         r')\s*,?\s+(' + '|'.join(re.escape(x) for x in
                         sorted(list(STATE_NAMES.values()) + list(STATE_NAMES), key=len, reverse=True)) +
                         r')(?![A-Za-z])', re.I)
    generic = {'county', 'city', 'town', 'power', 'center', 'union', 'liberty', 'hope',
               'justice', 'spring', 'summer', 'north', 'south', 'east', 'west', 'pilot'}
    unique = sorted((name for kind, name in index if kind == 'place' and
                     len(name) >= 7 and len(index[(kind, name)]) == 1 and name not in generic),
                    key=len, reverse=True)
    unique_pattern = re.compile(r'(?<![A-Za-z])(' + '|'.join(re.escape(x.title()) for x in unique) +
                                r')(?![A-Za-z])')
    return index, pattern, unique_pattern

def state_mentions(text):
    found = set()
    for match in STATE_PATTERN.finditer(text):
        word = match.group().casefold()
        for code, name in STATE_NAMES.items():
            if word in (code.casefold(), name.casefold()):
                found.add(code)
    return found

@lru_cache(maxsize=65536)
def resolve(text):
    """Return a location only when the headline identifies one unambiguous place.

    Pure function of `text` and the (load-once, never-mutated) gazetteer data,
    so memoizing it is always correct - not just within one queue() call, but
    forever. Bounded LRU (65,536 titles) kept across queue() calls so repeated
    desktop refreshes reuse it without unbounded growth in a long-lived process."""
    text = text or ''
    index, place_pattern, unique_pattern = data()
    states = state_mentions(text)
    candidates = []
    for match in re.finditer(r'\b([A-Z][A-Za-z.\' -]{1,45}?)\s+(?:County|Co\.?)(?=\W|$)', text, re.I):
        fragment = match.group(1).strip()
        for length in (3, 2, 1):
            part = ' '.join(fragment.split()[-length:])
            possible = index.get(('county', part.casefold()), set())
            if possible:
                explicit = state_mentions(text[match.end():match.end()+24])
                chosen = possible & (explicit or states or possible)
                if len(chosen) == 1:
                    state = next(iter(chosen))
                    candidates.append((f'{part.title()} County, {state}', state, 'county'))
                break
    for match in place_pattern.finditer(text):
        place = match.group(1)
        chosen = index.get(('place', place.casefold()), set()) & state_mentions(match.group(2))
        if len(chosen) == 1:
            state = next(iter(chosen))
            candidates.append((f'{place.title()}, {state}', state, 'place'))
    if not candidates:
        for match in unique_pattern.finditer(text):
            place = match.group(1)
            if re.match(r'\s+(?:Co\.?|County|City|Town|Village)(?=\W|$)', text[match.end():], re.I):
                continue
            state = next(iter(index[('place', place.casefold())]))
            if not states or states == {state}:
                candidates.append((f'{place}, {state}', state, 'place'))
    distinct = {(location, state) for location, state, _ in candidates}
    if len(distinct) == 1:
        location, state, kind = candidates[0]
        return {'location': location, 'state': state, 'kind': kind,
                'confidence': 'explicit' if state in states else 'unique_place'}
    if len(distinct) > 1:
        return {'location': '', 'state': '', 'kind': '', 'confidence': 'ambiguous'}
    if len(states) == 1:
        state = next(iter(states))
        return {'location': STATE_NAMES[state], 'state': state, 'kind': 'state', 'confidence': 'state_only'}
    return {'location': '', 'state': '', 'kind': '', 'confidence': 'unknown'}
