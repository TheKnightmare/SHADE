from __future__ import annotations

import re
import json
from datetime import datetime, timezone
from .relevance import remaining


def compact(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def workflow_status(claim) -> str:
    status = claim['status']
    if status == 'TX_CANDIDATE':
        basis = (claim.get('tx_candidate_basis') if isinstance(claim, dict) else claim['tx_candidate_basis']) or 'basis missing'
        return f"TX_CANDIDATE ({basis.replace('_', ' ')})"
    return status


def bulletin_basis_tag(claim, observations) -> str:
    if claim['status'] == 'TX_CANDIDATE' and claim.get('tx_candidate_basis') == 'operator_relay':
        return '(O)'
    if any(row['source_type'] in {'official','media'} for row in observations):
        return '(C)'
    if any(json.loads(row['raw_json']).get('_high_credibility_source') or
           json.loads(row['raw_json']).get('_trusted_for_relay') for row in observations):
        return '(U-HC)'
    return '(U)'


def traffic_message(
    claim,
    observations,
    *,
    callsign: str,
    network: str,
    max_chars: int = 500,
    mode: str = "standard",
    region_label: str = "",
) -> str:
    mode = mode.lower()
    if mode not in {"standard", "exercise", "actual"}:
        raise ValueError(f"unknown operating mode: {mode}")
    label = claim["confidence_label"]
    location = compact(claim["location"] or region_label or "LOCATION-NOT-STATED").upper()
    title = compact(claim["title"]).upper()
    sources = len({row["source_family"] for row in observations})
    official = len({row["source_family"] for row in observations if row["source_type"] == "official"})
    suffix = f" SRC-FAMILIES:{sources} OFFICIAL:{official} REVIEWED-BY:{callsign}"
    if mode == "exercise":
        prefix = f"@{network} EXERCISE/EMCOMM {label}/{location} - "
        suffix += " EXERCISE"
    elif mode == "actual":
        prefix = f"@{network} ACTUAL/EMCOMM {label}/{location} - "
    else:
        prefix = f"@{network} {label}/{location} - "
    room = max_chars - len(prefix) - len(suffix)
    if room < 20: raise ValueError("Message limit too small for mandatory attribution and mode markings")
    if len(title) > room:
        title = title[: room - 3].rstrip() + "..."
    return prefix + title + suffix


# Compatibility for integrations written against the 0.1 formatter API.
ghostnet_message = traffic_message


def split_message(text: str, max_chars: int) -> list[str]:
    """Split a JS8 message at sentence/clause/word boundaries."""
    if max_chars < 1:
        raise ValueError('max_chars must be positive')
    text = compact(text)
    parts = []
    while len(text) > max_chars:
        cut = max((m.end() for m in re.finditer(r'[.!?;,:]\s+', text[:max_chars])), default=0)
        if cut < max_chars // 2:
            cut = text[:max_chars + 1].rfind(' ')
        if cut <= 0:
            cut = max_chars
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def bulletin_messages(items, *, callsign, network, max_chars=500, mode='standard', region_label=''):
    """Render reviewed queue items as a numbered, manually-transmitted train."""
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
    header = f"@{network} BULLETIN {callsign} GENERATED:{stamp} ITEMS:{len(items)}"
    bodies = []
    for claim, observations in items:
        tagged = bulletin_basis_tag(claim, observations)+' '+traffic_message(claim, observations, callsign=callsign,
            network=network, max_chars=max_chars, mode=mode, region_label=region_label)
        bodies.extend(split_message(tagged, max_chars))
    total = 1 + len(bodies)
    # Re-split once using the final numbering overhead so every transmitted
    # line, including the header, remains within the operator limit.
    payload = [header, *bodies]
    width = len(str(total)) * 2 + 2
    while any(len(text) + width > max_chars for text in payload):
        payload = [header] + [part for body in payload[1:] for part in split_message(body, max_chars - width)]
        total = len(payload)
        width = len(str(total)) * 2 + 2
    return [f"{index}/{total} {text}" for index, text in enumerate(payload, 1)]


def evidence_summary(claim, observations):
    from .db import ALLOWED_TRANSITIONS
    lines=[f"CLAIM {claim['id']} | {claim['confidence_label']} | {workflow_status(claim)}",claim['title'],
           'LOCATION: '+(claim['location'] or 'not stated'),
           f"AREA: {claim['area']} | CATEGORY: {claim['category']}",
           f"PUBLISHED: {claim['published']} | FIRST SEEN: {claim['first_seen']} | LAST SEEN: {claim['last_seen']}",
           f"EXPIRES: {claim['expires'] or 'not supplied'} ({remaining(claim['expires'])}) | ONSET: {claim['onset'] or 'not supplied'}",
           f"EVENT: {claim['event']} | URGENCY: {claim['urgency']} | CERTAINTY: {claim['certainty']}",
           'SIGNIFICANCE: '+str(claim['score'])+' = '+json.dumps(claim['breakdown'],sort_keys=True),
           'CONFIDENCE: '+claim['confidence_explanation'],
           'VISIBILITY: '+(claim['reason'] or claim['lane'])]
    for row in observations:
        lines.append(f"- [{row['source_type']}/{row['source_family']}] {row['source_name']}: {row['url']} (published {row['published_at']}; observed {row['observed_at']})")
    targets=ALLOWED_TRANSITIONS.get(claim['status'],set())
    lines.append('NEXT: '+('; '.join(f"shade mark {claim['id']} {t.lower()}" for t in sorted(targets)) or 'terminal state; evidence retained'))
    if claim['status']=='REVIEW' and not any(row['source_type'] in {'official','media'} for row in observations):
        lines.append(f"RELAY OVERRIDE: shade relay {claim['id']} (explicit operator action; logged as operator_relay)")
    if claim['status'] in {'REVIEW','TX_CANDIDATE'}: lines.append(f"FORMAT: shade format {claim['id']} (ACTUAL also requires --confirm-actual)")
    return '\n'.join(lines)
