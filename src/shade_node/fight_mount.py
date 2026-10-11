"""Host FIGHT inside the SHADE browser console (one window, one launcher).

FIGHT's own code is loaded from StationWatch unchanged (pilot_fight_local's
FightPilotSession, its snapshot transport to SHADE, its bulletin store and
theme). SHADE only routes requests to it, so every FIGHT rule - read-only
SHADE access, selection binding, operator-written BLUF, no transmission -
stays exactly as FIGHT implements it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

LIST_FIELDS = {'bluf_item_id', 'bluf_choice', 'issue_item'}
MAX_POST_BYTES = 262_144


def default_fight_dir(config_path: str) -> Path:
    env = os.environ.get('SHADE_FIGHT_DIR')
    if env:
        return Path(env)
    # C:\Station\SHADE\config.toml -> C:\Station\Apps\StationWatch
    return Path(config_path).resolve().parent.parent / 'Apps' / 'StationWatch'


def windowless_python() -> str:
    exe = Path(sys.executable)
    if os.name == 'nt' and exe.name.lower() == 'python.exe' and exe.with_name('pythonw.exe').exists():
        return str(exe.with_name('pythonw.exe'))
    return str(exe)


class FightMount:
    """Lazily loads FIGHT; `error` explains why the tab is unavailable."""

    def __init__(self, service, config_path: str, fight_dir: Path | None = None, session=None, theme=None):
        self.service, self.config_path = service, str(Path(config_path).resolve())
        self.dir = Path(fight_dir) if fight_dir else default_fight_dir(config_path)
        self.session, self.theme, self.error = session, theme, None
        self.errors = ()

    def load(self):
        if self.session is not None or self.error:
            return self.session
        try:
            if not (self.dir / 'pilot_fight_local.py').exists():
                raise FileNotFoundError(f'FIGHT not found in {self.dir}')
            if str(self.dir) not in sys.path:
                sys.path.insert(0, str(self.dir))
            from pilot_fight_local import FightPilotSession
            from snapshot_transport import ShadeSnapshotTransport, SnapshotTransportError
            from station_core.fight import FightError
            from fight_theme import apply_theme
            settings = self.service.settings
            shade_root = Path(self.config_path).parent
            station = self.dir.parent.parent
            transport = ShadeSnapshotTransport(windowless_python(), shade_root / 'src' / 'snapshot_cli.py',
                                               shade_root / 'src', settings.database,
                                               cli_config=self.config_path, native_contract=True)
            self.session = FightPilotSession(transport, station / 'Logs' / 'fight-pilot',
                                             callsign=settings.callsign, network=settings.network,
                                             bulletin_dir=station / 'Logs' / 'fight-bulletins',
                                             wire_area=settings.region_label,
                                             nomad_storage=station / 'Nomad' / 'nomadnetwork' / 'storage')
            self.theme = apply_theme
            self.errors = (FightError, SnapshotTransportError, ValueError, KeyError, OSError, UnicodeError)
        except Exception as exc:   # tab shows the reason; SHADE keeps working
            self.error = f'{type(exc).__name__}: {exc}'
        return self.session

    @staticmethod
    def parse_form(raw: bytes) -> dict:
        from urllib.parse import parse_qs
        if not 0 < len(raw) <= MAX_POST_BYTES:
            raise ValueError('request body size invalid')
        fields = parse_qs(raw.decode('utf-8'), keep_blank_values=True, strict_parsing=True)
        if any(len(v) != 1 for k, v in fields.items() if k not in LIST_FIELDS):
            raise ValueError('duplicate form fields refused')
        return {k: v if k in LIST_FIELDS else v[0] for k, v in fields.items()}
