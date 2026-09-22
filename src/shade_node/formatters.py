from __future__ import annotations

import re
import json
from .relevance import remaining


def compact(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


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


def evidence_summary(claim, observations):
    from .db import ALLOWED_TRANSITIONS
    lines=[f"CLAIM {claim['id']} | {claim['confidence_label']} | {claim['status']}",claim['title'],
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
    if claim['status'] in {'REVIEW','TX_CANDIDATE'}: lines.append(f"FORMAT: shade format {claim['id']} (ACTUAL also requires --confirm-actual)")
    return '\n'.join(lines)
