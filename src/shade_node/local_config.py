"""Operator source switches, saved in config.local.toml (the station's own tuning file).

Two settings, both changed from the web console's Sources & health tab:
  * per-source on/off  -> [source_overrides] disabled / enabled lists (applied last, so they also
                          work for pack sources whose definitions live inside SHADE)
  * source packs on/off -> [source_packs] enabled

config.toml is never touched. config.local.toml keeps every other line and comment; a rolling
backup is written beside it before each change, and the new text must parse before it is saved.
"""
from __future__ import annotations

import re
import shutil
import tomllib
from pathlib import Path

BEGIN = '# --- SHADE web: source switches (managed; change them in Sources & health) ---'
END = '# --- end SHADE web source switches ---'


def _toml_list(values) -> str:
    return '[' + ', '.join('"' + v.replace('\\', '\\\\').replace('"', '\\"') + '"' for v in sorted(values)) + ']'


def local_path(config_path: str | Path) -> Path:
    return Path(config_path).with_name('config.local.toml')


def read(config_path) -> tuple[str, dict]:
    path = local_path(config_path)
    text = path.read_bytes().decode('utf-8') if path.exists() else ''   # keep CRLF as-is
    return text, (tomllib.loads(text) if text.strip() else {})


def _strip_block(text: str) -> str:
    return re.sub(re.escape(BEGIN) + r'.*?' + re.escape(END) + r'\n?', '', text, flags=re.S).rstrip('\n') + '\n'


def write(config_path, *, disabled: set[str], enabled: set[str], packs: list[str] | None = None) -> Path:
    """Save the switches. `packs=None` leaves the pack list alone."""
    path = local_path(config_path)
    text, current = read(config_path)
    crlf = '\r\n' in text
    body = _strip_block(text.replace('\r\n', '\n')) if text else ''
    block = [BEGIN, '[source_overrides]', f'disabled = {_toml_list(disabled)}', f'enabled = {_toml_list(enabled)}']
    if packs is not None:
        pattern = re.compile(r'(^\[source_packs\][^\[]*?^enabled\s*=\s*)\[[^\]]*\]', re.M | re.S)
        if pattern.search(body):
            body = pattern.sub(lambda m: m.group(1) + _toml_list(packs), body, count=1)
        elif re.search(r'^\[source_packs\]', body, re.M):
            body = re.sub(r'^\[source_packs\][^\n]*\n', lambda m: m.group(0) + f'enabled = {_toml_list(packs)}\n',
                          body, count=1, flags=re.M)
        else:
            block += ['[source_packs]', f'enabled = {_toml_list(packs)}']
    new = (body if body.strip() else '') + ('\n' if body.strip() else '') + '\n'.join(block + [END]) + '\n'
    tomllib.loads(new)                                   # refuse to save anything that does not parse
    if path.exists():
        shutil.copy2(path, path.with_name('config.local.toml.web-backup'))
    if crlf:
        new = new.replace('\n', '\r\n')
    path.write_bytes(new.encode('utf-8'))
    return path


def overrides(config_path) -> tuple[set[str], set[str]]:
    _, data = read(config_path)
    ov = data.get('source_overrides', {}) if isinstance(data.get('source_overrides'), dict) else {}
    return set(ov.get('disabled') or []), set(ov.get('enabled') or [])


# --------------------------------------------------------------------------- operator's own RSS feeds
FEEDS_BEGIN = '# --- SHADE web: my RSS feeds (managed; add or remove them in Sources & health) ---'
FEEDS_END = '# --- end SHADE web RSS feeds ---'
MY_PACK = 'my-feeds'
FEED_FIELDS = ('id', 'kind', 'name', 'url', 'source_type', 'source_family', 'category', 'area', 'pack',
               'min_poll_seconds')


def _toml_str(value) -> str:
    if isinstance(value, int):
        return str(value)
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def my_feeds(config_path) -> list[dict]:
    text, _ = read(config_path)
    text = text.replace('\r\n', '\n')
    m = re.search(re.escape(FEEDS_BEGIN) + r'\n(.*?)' + re.escape(FEEDS_END), text, flags=re.S)
    return tomllib.loads(m[1]).get('sources', []) if m else []


def write_feeds(config_path, feeds: list[dict]) -> Path:
    path = local_path(config_path)
    text, _ = read(config_path)
    crlf = '\r\n' in text
    body = re.sub(re.escape(FEEDS_BEGIN) + r'.*?' + re.escape(FEEDS_END) + r'\n?', '',
                  text.replace('\r\n', '\n'), flags=re.S).rstrip('\n')
    lines = [FEEDS_BEGIN]
    for feed in feeds:
        lines.append('[[sources]]')
        lines += [f'{k} = {_toml_str(feed[k])}' for k in FEED_FIELDS if feed.get(k) not in (None, '')]
        lines.append('')
    lines.append(FEEDS_END)
    new = (body + '\n\n' if body.strip() else '') + '\n'.join(lines) + '\n'
    tomllib.loads(new)
    if path.exists():
        shutil.copy2(path, path.with_name('config.local.toml.web-backup'))
    if crlf:
        new = new.replace('\n', '\r\n')
    path.write_bytes(new.encode('utf-8'))
    return path
