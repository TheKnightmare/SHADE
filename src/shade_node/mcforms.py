"""Decode JS8Call MCForms traffic (JS8Spotter / @SITREP multiple-choice forms).

A form response on the air looks like:
    @SITREP MSG F!104 100 ST[OK] GR[EM15DJ] #JGSE
    @SITREP  F!505 3233333333X3333X3332233X33323233 GR[FM05OW] WATER RESTRICTIONS #JGRD

Only the answer codes travel over the air; both ends hold the same form file
(JS8Spotter's forms/MCFnnn.txt). This module parses those form files and
expands a received response into labelled questions and answers. It never
transmits and never treats a self-report as verified fact.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Same structure JS8Spotter itself matches (js8spotter.py, MCF subsystem):
# <to> [optional words] F!<id> <responses> <free text> #<datecode>
MESSAGE = re.compile(
    r"(?P<to>@?[A-Z0-9]+)(?:/[A-Z0-9]+)?\s+(?:(?P<cmd>[A-Z?]+)\s+)?(?P<form>F![A-Z0-9]+)\s+"
    r"(?P<responses>[A-Z0-9]+)\s*(?P<text>.*?)\s*(?P<datecode>#[A-Z0-9]+)?\s*(?:[●♢â].*)?$")
FIELD = re.compile(r"\b([A-Z]{2})\[(.*?)\]")
CORRUPT = '<NACK>'


@dataclass
class Question:
    number: int
    text: str
    options: dict[str, str]
    default: str | None = None


@dataclass
class Form:
    form_id: str
    title: str
    questions: list[Question] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)
    version: str = ''


def parse_form(text: str) -> Form:
    lines = text.splitlines()
    title, _, form_id = lines[0].partition('|')
    form = Form(form_id.strip(), title.strip())
    current = None
    for line in lines[1:]:
        if line.startswith('? '):
            current = Question(len(form.questions) + 1, line[2:].strip(), {})
            form.questions.append(current)
        elif line.startswith('@') and current is not None and len(line) >= 2:
            code, label = line[1], line[2:].strip()
            if label.startswith('*'):
                label = label[1:].strip(); current.default = code
            current.options[code.upper()] = label
        elif line.startswith('[') and ']' in line:
            code, _, prompt = line[1:].partition(']')
            form.fields[code.strip().upper()] = prompt.strip().rstrip(':')
        elif line.startswith('. Version:'):
            form.version = line[len('. Version:'):].strip()
    return form


def load_forms(directory) -> dict[str, Form]:
    forms = {}
    for path in sorted(Path(directory).glob('MCF*.txt')):
        try:
            form = parse_form(path.read_text(encoding='utf-8', errors='replace'))
        except (IndexError, ValueError):
            continue
        if form.form_id.startswith('F!'):
            forms[form.form_id.upper()] = form
    return forms


def decode_datecode(code: str, received: datetime | None = None) -> str:
    """JS8Spotter short timestamp '#MDHm': month A-L, day A-Z/0-4, hour A-X,
    minute in 2-minute steps A-Z/0-3. Year is not sent. The protocol does not
    state a time zone (the sender's clock), so this returns text, not a UTC
    datetime."""
    code = (code or '').lstrip('#')
    if len(code) < 3:
        return ''
    month = ord(code[0]) - 64 if 'A' <= code[0] <= 'L' else None
    d = code[1]
    day = ord(d) - 64 if 'A' <= d <= 'Z' else (ord(d) - 47 + 26 if '0' <= d <= '4' else None)
    hour = ord(code[2]) - 65 if 'A' <= code[2] <= 'X' else None
    minute = 0
    if len(code) > 3:
        m = code[3]
        minute = (ord(m) - 65) * 2 if 'A' <= m <= 'Z' else ((ord(m) - 48) + 26) * 2 if '0' <= m <= '3' else None
    if None in (month, day, hour, minute):
        return ''
    return f'{month:02d}/{day:02d} {hour:02d}:{minute:02d} (sender clock)'


@dataclass
class DecodedForm:
    form_id: str
    title: str
    to: str
    responses: str
    answers: list[dict]
    fields: dict[str, str]
    free_text: str
    datecode: str
    sent_time: str
    complete: bool
    corrupted: bool
    problems: list[str]
    status: str  # GREEN / YELLOW / RED / PARTIAL / UNKNOWN
    degraded: list[dict]
    free_text_clean: str = ''


def parse_message(text: str) -> dict | None:
    """Split one JS8 directed-message text into MCForms parts, or None."""
    text = (text or '').strip()
    text = re.sub(r'^[A-Z0-9/]+:\s+', '', text)  # older JS8Call: 'CALL: @GROUP ...'
    # End-of-message markers: JS8Call's '●'/'♢', mojibake 'â', or '?' after
    # NODE's ASCII-safe logging.
    text = re.sub(r'(?:\s+[\u25cf\u2662\u00e2?]+)+\s*$', '', text)
    match = MESSAGE.search(text)
    if not match:
        return None
    body = match.group('text') or ''
    return {'to': match.group('to'), 'form': match.group('form').upper(), 'responses': match.group('responses').upper(),
            'text': body, 'datecode': match.group('datecode') or '', 'corrupted': CORRUPT in text,
            'fields': {k: v for k, v in FIELD.findall(body)}}


def _severity(form: Form, question: Question, code: str) -> int | None:
    """0 = worst .. 3 = normal for the 0-3 scale used by SITREP assessment
    forms; None when the form doesn't use that scale or the answer is unknown."""
    numeric = {c for c in question.options if c.isdigit()}
    if code in numeric and numeric <= {'0', '1', '2', '3'} and len(numeric) >= 3:
        return int(code)
    return None


def decode(text: str, forms: dict[str, Form]) -> DecodedForm | None:
    parts = parse_message(text)
    if not parts:
        return None
    form = forms.get(parts['form'])
    problems = []
    if parts['corrupted']:
        problems.append('Message partly lost in reception (<NACK>); answers may be incomplete.')
    if form is None:
        return DecodedForm(parts['form'], 'Unknown form (no local definition)', parts['to'], parts['responses'], [],
                           parts['fields'], parts['text'], parts['datecode'], decode_datecode(parts['datecode']),
                           False, parts['corrupted'], problems + ['No local form file for this ID.'], 'UNKNOWN', [])
    answers, degraded = [], []
    for question, code in zip(form.questions, parts['responses']):
        label = question.options.get(code)
        if label is None:
            problems.append(f'Q{question.number}: code {code!r} is not a valid answer.')
        sev = _severity(form, question, code)
        entry = {'number': question.number, 'question': question.text, 'code': code,
                 'answer': label or f'(invalid code {code})', 'severity': sev,
                 'is_default': code == question.default}
        answers.append(entry)
        if sev is not None and sev < 3:
            degraded.append(entry)
    complete = len(parts['responses']) == len(form.questions)
    if not complete:
        problems.append(f'{len(parts["responses"])} answers for {len(form.questions)} questions.')
    fields = {form.fields.get(k, k): v for k, v in parts['fields'].items()}
    status = 'UNKNOWN'
    if not complete or any(a['answer'].startswith('(invalid') for a in answers):
        status = 'PARTIAL'
    elif form.form_id == 'F!104' and answers:
        status = {'1': 'GREEN', '2': 'YELLOW', '3': 'RED'}.get(answers[0]['code'], 'UNKNOWN')
    elif degraded or any(a['severity'] == 3 for a in answers):
        worst = min((a['severity'] for a in degraded), default=3)
        status = 'RED' if worst <= 1 else 'YELLOW' if worst == 2 else 'GREEN'
    # Operator free text only; drop field markers and reception debris
    # (bracket fragments, 1-2 character scraps next to <NACK>).
    clean = FIELD.sub('', parts['text']).replace(CORRUPT, ' ')
    clean = ' '.join(word for word in clean.split()
                     if word not in form.fields and '[' not in word and ']' not in word and not word.startswith('#') and len(word) > 2)
    if parts['corrupted'] and clean:
        problems.append(f'Unreadable fragment: {clean}'); clean = ''
    return DecodedForm(form.form_id, form.title, parts['to'], parts['responses'], answers, fields, parts['text'],
                       parts['datecode'], decode_datecode(parts['datecode']), complete, parts['corrupted'],
                       problems, status, degraded, clean)


def summary(decoded: DecodedForm, sender: str = '') -> str:
    """One-line operator summary, e.g. for a SHADE evidence row title."""
    where = decoded.fields.get('Maidenhead Grid Square (REQUIRED - 4, 6, 8, or 10 characters)') \
        or next((v for k, v in decoded.fields.items() if 'Grid' in k), '')
    state = next((v for k, v in decoded.fields.items() if k.startswith('State')), '')
    loc = ' '.join(x for x in (state, where) if x)
    head = f'{sender + " " if sender else ""}{decoded.title} [{decoded.status}]' + (f' {loc}' if loc else '')
    if decoded.free_text_clean:
        head += f' "{decoded.free_text_clean}"'
    if decoded.form_id == 'F!104':
        extras = [a['answer'] for a in decoded.answers[1:] if not a['is_default']]
        return head + (': ' + '; '.join(extras) if extras else '')
    if decoded.degraded:
        items = ', '.join(f"{a['question'].split(') ',1)[-1]}={a['code']}" for a in decoded.degraded[:6])
        return head + f': degraded - {items}' + ('…' if len(decoded.degraded) > 6 else '')
    if decoded.status == 'PARTIAL':
        return head + ': partly received; answers incomplete'
    return head + ': all reported items normal'


def datecode_age_hours(code: str, received: datetime) -> float | None:
    """Approximate hours between the sender's datecode and reception. The
    sender clock's time zone is unknown, so treat anything under ~14 h as
    'current'; larger gaps mean a stored/re-broadcast report."""
    text = decode_datecode(code)
    if not text:
        return None
    month, rest = text.split('/', 1)
    day, clock = rest.split(' ', 1)
    hour, minute = clock[:5].split(':')
    try:
        sent = received.replace(month=int(month), day=int(day), hour=int(hour), minute=int(minute), second=0, microsecond=0)
    except ValueError:
        return None
    if sent > received + (received - received.replace(year=received.year - 1)) / 2:
        sent = sent.replace(year=sent.year - 1)
    return (received - sent).total_seconds() / 3600


def maidenhead_center(grid: str) -> tuple[float, float] | None:
    """(lat, lon) of a 4/6-character Maidenhead locator's centre."""
    g = (grid or '').strip()
    if len(g) < 4 or not (g[0].isalpha() and g[1].isalpha() and g[2:4].isdigit()):
        return None
    lon = (ord(g[0].upper()) - 65) * 20 - 180 + int(g[2]) * 2
    lat = (ord(g[1].upper()) - 65) * 10 - 90 + int(g[3])
    if len(g) >= 6 and g[4:6].isalpha():
        lon += (ord(g[4].lower()) - 97) * (2 / 24) + (1 / 24)
        lat += (ord(g[5].lower()) - 97) * (1 / 24) + (1 / 48)
    else:
        lon += 1; lat += 0.5
    return round(lat, 4), round(lon, 4)


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    from math import asin, cos, radians, sin, sqrt
    (lat1, lon1), (lat2, lon2) = a, b
    h = sin(radians(lat2 - lat1) / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371 * asin(sqrt(h))


def area_tier(grid: str, home_grid: str) -> str:
    """SHADE area tier from distance to the station's own grid square:
    <=160 km local, <=500 km regional, contiguous US national, else global.
    Empty when either grid is missing."""
    here, there = maidenhead_center(home_grid), maidenhead_center(grid)
    if not here or not there:
        return ''
    km = distance_km(here, there)
    if km <= 160:
        return 'ETN/WNC'
    if km <= 500:
        return 'REGIONAL'
    lat, lon = there
    return 'NATIONAL' if 24 <= lat <= 50 and -125 <= lon <= -66 else 'GLOBAL'
