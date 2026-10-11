"""Read JS8 MCForms traffic that NODE (StationWatch) already logged.

NODE tails JS8Call's own logs and writes one JSON line per decoded message
to <Logs>/groups-YYYY-MM-DD.jsonl. This collector reads those files
(read-only, local disk, no network, no radio control), keeps the MCForms
responses (F!nnn ...), decodes them with the operator's JS8Spotter form
files, and returns them as community-sourced, unverified observations.

The same report heard again (same sender, form, answers and datecode) keeps
the same external_id, so SHADE treats it as one report seen again rather
than a new one.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import commstat
from .mcforms import area_tier, decode, datecode_age_hours, load_forms, maidenhead_center, summary
from .model import Observation

STALE_HOURS = 14
DAYS_READ = 3
SEVERITY = {'RED': 'severe', 'YELLOW': 'moderate', 'GREEN': 'minor'}


def _parse_utc(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _log_files(logs: Path, now: datetime):
    """This PC's NODE output, then every remote NODE's forwarded copy under
    <logs>/nodes/<node-id>/ (written by the host's NODE receiver)."""
    folders = [('local', logs)]
    remote = logs / 'nodes'
    if remote.is_dir():
        folders += sorted((child.name, child) for child in remote.iterdir() if child.is_dir())
    for node_id, folder in folders:
        for offset in range(DAYS_READ):
            day = (now - timedelta(days=offset)).strftime('%Y-%m-%d')
            path = folder / f'groups-{day}.jsonl'
            if path.exists():
                yield node_id, path


def collect_node_js8(source, now: datetime | None = None) -> list[Observation]:
    from .collectors import CollectionError
    now = now or datetime.now(timezone.utc)
    logs = Path(source.url.removeprefix('file:///').removeprefix('file://'))
    forms_dir = Path(getattr(source, 'forms_dir', '') or '')
    if not logs.is_dir():
        raise CollectionError(f'NODE log folder not found: {logs}')
    forms = load_forms(forms_dir) if forms_dir.is_dir() else {}
    if str(getattr(source, 'forms_dir', '') or '').strip() and not forms:
        # A configured forms folder that is missing is a setup error; leave
        # forms_dir empty to read only CommStat traffic from the NODE logs.
        raise CollectionError(f'No MCForms definitions found in: {forms_dir}')
    observations, seen = [], set()
    for node_id, path in _log_files(logs, now):
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                is_commstat = '{&%' in line or '{F%' in line or '{%%' in line
                if 'F!' not in line and not is_commstat:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get('mode') != 'JS8' or not record.get('frm'):
                    continue
                text = f"{record.get('to') or ''} {record.get('msg') or ''}".strip()
                if is_commstat:
                    found = commstat.decode_statrep(text) or commstat.decode_alert(text)
                    if found:
                        found['sender'] = found['sender'] or str(record['frm']).upper()
                        found['target'] = found['target'] or str(record.get('to') or '').upper()
                        heard = _parse_utc(record.get('utc')) or now
                        heard_at = 'this station' if node_id == 'local' else f'remote NODE {node_id}'
                        obs = commstat.observation(source, found, heard=heard, origin=(
                            f"heard on JS8 {record.get('dial_mhz') or '?'} MHz, SNR {record.get('snr')} dB, by {heard_at}"),
                            extra={'node': {**{k: record.get(k) for k in ('utc', 'src', 'dial_mhz', 'snr', 'frm', 'to', 'grid')},
                                            'node_id': node_id}})
                        if obs.external_id not in seen:
                            seen.add(obs.external_id)
                            observations.append(obs)
                    continue
                if not forms:
                    continue
                decoded = decode(text, forms)
                if decoded is None:
                    continue
                heard = _parse_utc(record.get('utc')) or now
                sender = str(record['frm']).upper()
                key = '|'.join((sender, decoded.form_id, decoded.responses, decoded.datecode or heard.strftime('%Y%m%d%H%M')))
                if key in seen:
                    continue
                seen.add(key)
                age = datecode_age_hours(decoded.datecode, heard) if decoded.datecode else None
                problems = list(decoded.problems)
                if age is not None and age > STALE_HOURS:
                    problems.append(f'Re-broadcast of an older report: sender stamped it ~{age:.0f} h before it was heard.')
                grid = next((v for k, v in decoded.fields.items() if 'Grid' in k), '') or record.get('grid') or ''
                state = next((v for k, v in decoded.fields.items() if k.startswith('State')), '')
                place = next((v for k, v in decoded.fields.items() if k.startswith('City')), '')
                alerting = decoded.form_id == 'F!104' and any(a['code'] == 'E' or a['code'] == 'P' for a in decoded.answers[1:])
                routine = decoded.status == 'GREEN' and not alerting and not decoded.free_text_clean
                heard_at = 'this station' if node_id == 'local' else f'remote NODE {node_id}'
                lines = [f'{sender} via {record.get("to")} on JS8 {record.get("dial_mhz") or "?"} MHz, SNR {record.get("snr")} dB, heard by {heard_at}.',
                         f'Form {decoded.form_id} {decoded.title}; overall {decoded.status}.',
                         'Self-reported amateur radio traffic. Not independently verified.']
                if problems:
                    lines.append('CAUTION: ' + ' '.join(problems))
                lines += [f"Q{a['number']} {a['question']}: {a['answer']}" for a in decoded.answers]
                lines += [f'{k}: {v}' for k, v in decoded.fields.items()]
                if decoded.free_text_clean:
                    lines.append(f'Operator note: {decoded.free_text_clean}')
                center = maidenhead_center(grid)
                raw = {'_mcforms': {'form': decoded.form_id, 'responses': decoded.responses, 'status': decoded.status,
                                    'datecode': decoded.datecode, 'sender_time': decoded.sent_time,
                                    'corrupted': decoded.corrupted, 'complete': decoded.complete,
                                    'degraded': [{'q': a['number'], 'code': a['code']} for a in decoded.degraded]},
                       'node': {**{k: record.get(k) for k in ('utc', 'src', 'dial_mhz', 'snr', 'offset_hz', 'frm', 'to', 'grid')},
                                'node_id': node_id},
                       '_area': '', 'status': 'actual'}
                tier = area_tier(grid, getattr(source, 'home_grid', ''))
                if tier:
                    raw['_area_tier'] = tier
                if center:
                    raw['geometry'] = {'type': 'Point', 'coordinates': [center[1], center[0]]}
                stamp = heard.isoformat().replace('+00:00', 'Z')
                observations.append(Observation(
                    source.id, source.name, source.source_type, source.source_family, key,
                    summary(decoded, sender), '\n'.join(lines), '',
                    category='chatter' if routine else 'infrastructure',
                    location=' '.join(x for x in (place, state, grid) if x),
                    severity=SEVERITY.get(decoded.status, 'unknown'),
                    published_at=stamp, observed_at=stamp, raw=raw))
    return observations
