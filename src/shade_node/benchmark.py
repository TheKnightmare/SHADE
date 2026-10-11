"""Independent, dated benchmark audit. Input rows are never used to train ranking."""
import csv
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .db import claim_detail, queue
from .discovery_audit import diagnose
from .gazetteer import resolve
from .model import token_set
from .relevance import timestamp

REQUIRED = {'url', 'published_at', 'county', 'state'}

def _family(title):
    text = title.casefold()
    groups = {
        'privacy/surveillance': ('flock', 'camera', 'license plate', 'surveillance'),
        'infrastructure/comms': ('tower', 'e-911', 'outage', 'power grid', 'nuclear'),
        'environment': ('pfas', 'chemical', 'pollution', 'contamination'),
        'crime/public safety': ('shot', 'arrest', 'arson', 'threat', 'homicide', 'machete'),
        'weather': ('flood', 'tornado', 'storm', 'warning'),
        'transport': ('crash', 'highway', 'road', 'reopens'),
    }
    return next((family for family, terms in groups.items() if any(term in text for term in terms)), 'other/unknown')

def audit_csv(connection, database, csv_path, policy):
    with Path(csv_path).open(newline='', encoding='utf-8-sig') as stream:
        reader = csv.DictReader(stream)
        if not REQUIRED <= set(reader.fieldnames or []):
            raise ValueError('Benchmark CSV requires url,published_at,county,state columns.')
        benchmark = list(reader)
    if not benchmark:
        raise ValueError('Benchmark CSV is empty.')
    inbox = queue(connection, None, 100000, policy=policy)
    ranks = {item['id']: rank for rank, item in enumerate(inbox, 1)}
    observations = [dict(row) for row in connection.execute(
        'SELECT claim_id,url,title,published_at,raw_json FROM observations')]
    reports = []
    for number, row in enumerate(benchmark, 2):
        url = row['url'].strip()
        expected_state = row['state'].strip().upper()
        expected_county = row['county'].strip().casefold().removesuffix(' county')
        published = timestamp(row['published_at'])
        if urlsplit(url).scheme not in {'http', 'https'} or not published or not re.search(r'(?:Z|[+-]\d{2}:\d{2})$', row['published_at']) or expected_state not in {'AL','GA','KY','NC','TN','WV'}:
            raise ValueError(f'Invalid benchmark row {number}; provide URL, timezone-aware date, county, state.')
        exact = [item for item in observations if item['url'] == url]
        match = exact[0] if exact else None
        match_basis = 'exact_url' if match else 'none'
        # Google News wrappers can hide a direct publisher URL. Use origin host,
        # date and headline path only as a *possible* match, never as proof.
        possible = []
        if not match:
            path_tokens = token_set(urlsplit(url).path.replace('-', ' '))
            host = (urlsplit(url).hostname or '').removeprefix('www.').casefold()
            for item in observations:
                raw = json.loads(item['raw_json'])
                item_host = (urlsplit(item['url']).hostname or '').removeprefix('www.').casefold()
                if host not in {item_host, raw.get('_origin_host', '').removeprefix('www.').casefold()}:
                    continue
                when = timestamp(item['published_at'])
                if not when or abs((when-published).total_seconds()) > 48*3600:
                    continue
                overlap = len(path_tokens & token_set(item['title'])) / max(1, len(path_tokens | token_set(item['title'])))
                if overlap >= 0.5:
                    possible.append((overlap, item))
            if possible:
                possible.sort(key=lambda pair: pair[0], reverse=True)
                match_basis = 'possible_title_host_date'
        candidate = match or (possible[0][1] if possible else None)
        claim = claim_detail(connection, candidate['claim_id'], policy)[0] if candidate else None
        location = resolve(candidate['title']) if candidate else None
        state_correct = location['state'] == expected_state if location else None
        if not location:
            area_correct = None
        elif expected_county in {'', 'statewide'}:
            area_correct = state_correct
        elif location['kind'] == 'county':
            found_county = location['location'].split(' County,')[0].casefold()
            area_correct = state_correct and found_county == expected_county
        elif location['kind'] == 'place':
            area_correct = None  # Gazetteer place rows do not prove county membership.
        else:
            area_correct = False
        discovery = 'yes' if match else 'possible' if possible else 'no'
        if claim:
            position = ranks.get(claim['id'])
            if position:
                filter_reason = ''
            elif claim['status'] in {'SENT','REJECTED','EXPIRED','SUPPRESSED','MERGED'}:
                filter_reason = f'status={claim["status"]}'
            elif claim['reason']:
                filter_reason = claim['reason']
            elif claim['lane'] != 'inbox':
                filter_reason = f'lane={claim["lane"]}'
            else:
                filter_reason = f'score={claim["score"]}; threshold={policy["minimum_score"]}'
        else:
            position = None
            filter_reason = 'No matched observation'
        expected_event = (row.get('event_category') or '').strip().casefold()
        event_correct = claim['category'].casefold() == expected_event if claim and expected_event else None
        missed = discovery != 'yes' or area_correct is not True or event_correct is False or position is None
        reports.append({
            'url': url, 'published_at': row['published_at'], 'county': row['county'], 'state': expected_state,
            'discovery': discovery, 'match_basis': match_basis, 'claim_id': claim['id'] if claim else None,
            'area': claim['area'] if claim else None, 'location': location['location'] if location else '',
            'state_correct': state_correct, 'area_correct': area_correct,
            'event_category': claim['category'] if claim else None,
            'event_correct': event_correct, 'event_note': '' if expected_event else 'CSV has no expected event_category column',
            'inbox_rank': position, 'display_filter': filter_reason,
            'concept_family': _family(candidate['title'] if candidate else urlsplit(url).path.replace('-', ' ')),
            'why_missed': diagnose(connection, database, url, policy) if missed else None,
        })
    def summary(key):
        result = {}
        for item in reports:
            group = result.setdefault(item[key], {'stories':0,'discovered':0,'state_correct':0,'area_correct':0,'area_unknown':0,'event_correct':0,'event_unknown':0,'inbox':0})
            group['stories'] += 1
            group['discovered'] += item['discovery'] == 'yes'
            group['state_correct'] += item['state_correct'] is True
            group['area_correct'] += item['area_correct'] is True
            group['area_unknown'] += item['area_correct'] is None
            group['event_correct'] += item['event_correct'] is True
            group['event_unknown'] += item['event_correct'] is None
            group['inbox'] += item['inbox_rank'] is not None
        return result
    return {'stories': reports, 'by_state': summary('state'), 'by_concept_family': summary('concept_family')}
