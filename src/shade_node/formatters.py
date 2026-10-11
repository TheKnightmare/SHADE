from __future__ import annotations

import re
import json
from datetime import datetime, timezone
from .relevance import remaining


def compact(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# Radio copy is plain text.  Remove emoji code points and their joiner/
# presentation machinery before applying the message length limit.  The
# keycap sequence is matched first and reduced to its plain digit, #, or *.
_KEYCAP_EMOJI = re.compile(r"[0-9#*]\ufe0f?\u20e3")
_EMOJI_SINGLETONS = {
    0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x2328, 0x23CF,
    0x24C2, 0x25B6, 0x25C0, 0x2934, 0x2935, 0x3030, 0x303D, 0x3297, 0x3299,
}
_EMOJI_RANGES = (
    (0x2194, 0x21AA), (0x231A, 0x231B), (0x23E9, 0x23F3),
    (0x23F8, 0x23FA), (0x25AA, 0x25AB), (0x25FB, 0x25FE),
    (0x2600, 0x27BF), (0x2B05, 0x2B55), (0x1F000, 0x1FAFF),
    (0xE0020, 0xE007F),
)
_EMOJI_PARTS = {0x200D, 0x20E3, 0xFE0E, 0xFE0F}


def _emoji_codepoint(character: str) -> bool:
    codepoint = ord(character)
    return (codepoint in _EMOJI_PARTS or codepoint in _EMOJI_SINGLETONS
            or any(start <= codepoint <= end for start, end in _EMOJI_RANGES))


def strip_emoji(text: str) -> str:
    """Remove radio-incompatible emoji while preserving surrounding copy."""
    without_keycaps = _KEYCAP_EMOJI.sub(lambda match: match.group()[0], text)
    cleaned = "".join(char for char in without_keycaps if not _emoji_codepoint(char))
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def workflow_status(claim) -> str:
    status = claim['status']
    # Operator disposition vocabulary is intentionally simpler than stored
    # lifecycle mechanics. Preserve the raw status/reason in DB and audit;
    # this is presentation-only and never mutates either record.
    if status in {'REJECTED', 'EXPIRED'}:
        return 'REJECT'
    if status == 'TX_CANDIDATE':
        basis = (claim.get('tx_candidate_basis') if isinstance(claim, dict) else claim['tx_candidate_basis']) or 'basis missing'
        return f"TX_CANDIDATE ({basis.replace('_', ' ')})"
    if status == 'SENT':
        # Transmission logging (wire_transmissions/record_wire_transmission)
        # was added after some SENT claims already existed - those are valid
        # historical evidence, not a data-quality problem, so this never
        # infers or backfills a channel/message; it only distinguishes two
        # real, different situations for the operator.
        if claim.get('wire_transmissions'):
            return 'SENT — transmission details available'
        return 'SENT — no transmission record; predates recording'
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
    label = strip_emoji(claim["confidence_label"])
    # A station's home-region label does not locate every regional story.
    area = claim.get('area', '') if isinstance(claim, dict) else claim['area']
    location = strip_emoji(compact(claim["location"] or (region_label if area == 'ETN/WNC' else area) or "LOCATION-NOT-STATED")).upper()
    title = strip_emoji(compact(claim["title"])).upper()
    suffix = ''
    if mode == "exercise":
        prefix = f"@{strip_emoji(network)} EXERCISE/EMCOMM {label}/{location} - "
        suffix += " EXERCISE"
    elif mode == "actual":
        prefix = f"@{strip_emoji(network)} ACTUAL/EMCOMM {label}/{location} - "
    else:
        prefix = f"@{strip_emoji(network)} {label}/{location} - "
    room = max_chars - len(prefix) - len(suffix)
    if room < 20: raise ValueError("Message limit too small for the report and mode markings")
    if len(title) > room:
        title = title[: room - 3].rstrip() + "..."
    return strip_emoji(prefix + title + suffix)


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
        for footnote, row in enumerate((row for row in observations if row.get('display_role') == 'supporting'), 1):
            detail = f"(F{footnote}) SUPPORT [{row['source_type']}/{row['source_family']}] {row['source_name']}: {row['url']}"
            bodies.extend(split_message(detail, max_chars))
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
           f"CHATTER: {claim['chatter_mentions']} mentions, {claim['chatter_sources']} sources, {claim['chatter_window']}",
           'VISIBILITY: '+(claim['reason'] or claim['lane'])]
    for wire in claim.get('wire_transmissions', []):
        lines.append(f"WIRE TX: {wire['sent_at']} on {wire['channel']} • {wire['message_text']}")
    for row in observations:
        marker = 'LEAD' if row.get('display_role') == 'lead' else 'FOOTNOTE'
        lines.append(f"- {marker} [{row['source_type']}/{row['source_family']}] {row['source_name']}: {row['url']} (published {row['published_at']}; observed {row['observed_at']})")
    targets=ALLOWED_TRANSITIONS.get(claim['status'],set())
    lines.append('NEXT: '+('; '.join(f"shade mark {claim['id']} {t.lower()}" for t in sorted(targets)) or 'terminal state; evidence retained'))
    if claim['status']=='REVIEW' and not any(row['source_type'] in {'official','media'} for row in observations):
        lines.append(f"RELAY OVERRIDE: shade relay {claim['id']} (explicit operator action; logged as operator_relay)")
    if claim['status'] in {'REVIEW','TX_CANDIDATE'}: lines.append(f"FORMAT: shade format {claim['id']} (ACTUAL also requires --confirm-actual)")
    return '\n'.join(lines)
