"""Auditable matching of configured chokepoints to collected observations."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path


def load_chokepoints(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open('rb') as handle:
        entries = tomllib.load(handle).get('chokepoints', [])
    if not isinstance(entries, list) or any(not isinstance(e, dict) or not e.get('id') or not e.get('name') for e in entries):
        raise ValueError(f'Invalid chokepoints registry: {path}')
    ids = [e['id'] for e in entries]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate chokepoint IDs')
    return entries


def _phrase(text: str, phrase: str) -> bool:
    if not phrase:
        return False
    # Permit I-40 / I 40 / Interstate 40 spelling without matching I-400.
    escaped = re.escape(phrase).replace(r'\-', r'[-\s]?')
    if re.fullmatch(r'I-\d+', phrase, re.I):
        escaped = r'(?:I[-\s]?|Interstate\s+)' + phrase.split('-')[1]
    return bool(re.search(r'(?<!\w)' + escaped + r'(?!\w)', text, re.I))


def _mile_markers(text: str) -> list[float]:
    return [float(value) for value in re.findall(
        r'\b(?:mile\s*(?:marker|post)|mm)\s*#?\s*(\d{1,3}(?:\.\d+)?)\b', text, re.I)]


def tag_chokepoints(observation, chokepoints: list[dict]) -> None:
    """Attach match evidence and a bounded boost to the normal scoring path."""
    if observation.source_family not in {'tdot-smartway', 'usgs-waterservices'}:
        return
    text = ' '.join(str(part) for part in (
        observation.title, observation.body, observation.location,
        *observation.raw.values()) if part is not None)
    matches = []
    for entry in chokepoints:
        matched_by = ''
        if observation.source_family == 'usgs-waterservices':
            site = str(observation.raw.get('site') or '')
            if site and site in [str(s) for s in entry.get('watch_usgs_sites', [])]:
                matched_by = 'USGS site ' + site
        elif 'TDOT SmartWay' in entry.get('incident_sources', []):
            for alias in [entry['name'], *entry.get('aliases', [])]:
                if _phrase(text, alias):
                    matched_by = 'name/alias: ' + alias
                    break
            if not matched_by and entry.get('route') and _phrase(text, entry['route']):
                for county in entry.get('counties', []):
                    if _phrase(text, county):
                        matched_by = f'{entry["route"]} + {county} County'
                        break
                if not matched_by and 'mile_marker_min' in entry and 'mile_marker_max' in entry:
                    for marker in _mile_markers(text):
                        if entry['mile_marker_min'] <= marker <= entry['mile_marker_max']:
                            matched_by = f'{entry["route"]} + mile marker {marker:g}'
                            break
        if matched_by:
            matches.append({'id': entry['id'], 'name': entry['name'], 'matched_by': matched_by})
    if matches:
        observation.raw['_chokepoints'] = matches
        observation.raw['_chokepoint_boost'] = 10
