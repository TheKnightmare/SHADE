"""NODE (StationWatch) feed switches, edited from SHADE web's NODE tab.

NODE's settings file is station-watch-config.json beside station_watch.py. Feeds are switched off by
listing them in its "disabled_sources" array ("http_sources:xlx237_eastcoast", "varac_source"); the
feed's own settings stay in the file untouched, so switching it back on restores it exactly.
A rolling backup is written before each change. NODE reads the file when it starts, so a change
takes effect after a NODE restart.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

SECTIONS = {
    'sources': 'Log file',
    'decoders': 'Decoder log',
    'udp_sources': 'UDP feed',
    'http_sources': 'Web dashboard',
    'fldigi_sources': 'fldigi',
}
CONFIG_NAME = 'station-watch-config.json'
TASK = 'OSINT STATION NODE'


def _detail(spec) -> str:
    if isinstance(spec, str):
        return spec
    if not isinstance(spec, dict):
        return ''
    if spec.get('url'):
        return spec['url']
    if spec.get('path'):
        return spec['path']
    if spec.get('db_path'):
        return spec['db_path']
    if spec.get('port'):
        return f"{spec.get('host') or spec.get('bind') or '127.0.0.1'}:{spec['port']}"
    return ''


class NodeSettings:
    def __init__(self, station_watch_dir: str | Path):
        self.dir = Path(station_watch_dir)
        self.path = self.dir / CONFIG_NAME

    def _load(self) -> tuple[dict, bool]:
        if not self.path.is_file():
            raise ValueError(f'NODE settings file not found: {self.path}')
        raw = self.path.read_bytes().decode('utf-8-sig')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('NODE settings file is not a JSON object')
        return data, '\r\n' in raw

    def listing(self) -> dict:
        data, _ = self._load()
        off = set(data.get('disabled_sources') or [])
        rows = []
        if isinstance(data.get('varac_source'), dict):
            v = data['varac_source']
            rows.append({'key': 'varac_source', 'section': 'VarAC database', 'name': 'varac',
                         'detail': _detail(v), 'on': 'varac_source' not in off and v.get('enabled', True) is not False})
        for section, label in SECTIONS.items():
            for name, spec in (data.get(section) or {}).items():
                key = f'{section}:{name}'
                rows.append({'key': key, 'section': label, 'name': name, 'detail': _detail(spec), 'on': key not in off})
        return {'path': str(self.path), 'rows': rows,
                'note': 'Switches save to station-watch-config.json (backup kept beside it). '
                        'NODE reads it when it starts: press Restart NODE to apply.'}

    def set(self, key: str, on: bool) -> dict:
        data, crlf = self._load()
        known = {r['key'] for r in self.listing()['rows']}
        if key not in known:
            raise ValueError('Unknown NODE feed.')
        off = [k for k in (data.get('disabled_sources') or []) if k != key]
        if not on:
            off.append(key)
        if key == 'varac_source' and on and isinstance(data.get('varac_source'), dict):
            data['varac_source']['enabled'] = True       # an old enabled:false must not keep it off
        data['disabled_sources'] = sorted(off)
        text = json.dumps(data, indent=2, ensure_ascii=False) + '\n'
        json.loads(text)
        shutil.copy2(self.path, self.path.with_name(CONFIG_NAME + '.web-backup'))
        tmp = self.path.with_name(CONFIG_NAME + '.tmp')
        tmp.write_bytes((text.replace('\n', '\r\n') if crlf else text).encode('utf-8'))
        os.replace(tmp, self.path)
        return {'key': key, 'on': on}

    def restart(self) -> dict:
        """Restart NODE's background task with the station's own control script (no RF involved)."""
        script = self.dir.parent.parent / 'Launch' / 'node_service_control.ps1'
        if os.name != 'nt':
            raise ValueError('Restart NODE is only available on the station PC.')
        if not script.is_file():
            raise ValueError(f'NODE control script not found: {script}')
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        done = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script),
                               '-Action', 'restart'], capture_output=True, text=True, timeout=90, creationflags=flags)
        if done.returncode != 0:
            raise ValueError('NODE restart failed: ' + (done.stderr or done.stdout).strip()[-300:])
        return {'restarted': True, 'output': done.stdout.strip()[-300:]}
