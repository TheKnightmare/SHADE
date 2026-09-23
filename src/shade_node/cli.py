from __future__ import annotations

import argparse
from importlib import resources
import json
from pathlib import Path
import sys

from .db import claim_detail, connect, queue, transition, housekeeping, backup_database, ALLOWED_TRANSITIONS
from .relevance import age, remaining
from .collectors import PARSERS
import sqlite3
from contextlib import closing
from . import __version__
from .engine import active_sources, load_settings, normalize_mode, run_once
from .formatters import evidence_summary, traffic_message, bulletin_messages
from datetime import datetime, timezone, timedelta


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="shade",
        description="SHADE provenance-first public-source collector for human-reviewed radio traffic",
    )
    command.add_argument("--mode", choices=["standard","exercise","actual"], help="explicit operating profile; use standard for unattended runs")
    command.add_argument("--config", default="config.toml", help="TOML configuration path")
    command.add_argument(
        "--emcomm",
        choices=["exercise", "actual"],
        help="override the configured operating mode for this command",
    )
    sub = command.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create config.toml from the example")
    sub.add_parser("status", help="Show active mode and safety boundaries")
    sub.add_parser("run-once", help="Poll enabled sources once")
    sub.add_parser('doctor',help='Validate setup without displaying private values or polling sources')
    sub.add_parser('migrate',help='Back up legacy database, migrate safely and expire ended claims')
    clean=sub.add_parser('housekeep',help='Preview expiration/suppression; --apply is explicit confirmation')
    clean.add_argument('--apply',action='store_true')
    clean.add_argument('--suppress',action='store_true',help='Also suppress low-value NEW claims')
    for name in ('inbox','queue','now'):
        view=sub.add_parser(name,help='Active local life-safety lane' if name=='now' else 'Durable operator inbox (ID is the claim ID)',
                            epilog='Examples: shade inbox --category cyber; shade now --area REGIONAL --all; shade inbox --status EXPIRED --all --lane all')
        view.add_argument('--min-score',type=int)
        view.add_argument('--limit',type=int,default=50)
        view.add_argument('--category')
        view.add_argument('--area',choices=['ETN/WNC','REGIONAL','NATIONAL','GLOBAL','DISTANT'])
        view.add_argument('--status',choices=list(ALLOWED_TRANSITIONS))
        view.add_argument('--max-age',type=float,help='Maximum published age in hours')
        view.add_argument('--all',action='store_true',help='Include suppressed reports; NOW still excludes ended or unknown-expiration items')
        if name!='now': view.add_argument('--lane',choices=['inbox','context','now','all'],default='inbox')
    show_parser = sub.add_parser("show", help="Show provenance for one claim")
    show_parser.add_argument("claim_id", type=int)
    format_parser = sub.add_parser("format", help="Format one claim for operator review")
    format_parser.add_argument("claim_id", type=int)
    format_parser.add_argument(
        "--confirm-actual",
        action="store_true",
        help="required to format traffic while ACTUAL EmComm mode is active",
    )
    bulletin_parser = sub.add_parser("bulletin", help="Export reviewed queue as a numbered JS8 text train")
    bulletin_parser.add_argument("--window", default="24h", help="lookback duration, e.g. 6h or 2d")
    bulletin_parser.add_argument("--min-score", type=int, default=0)
    bulletin_parser.add_argument("--category", help="comma-separated categories")
    bulletin_parser.add_argument("--output", help="output text path (default: data/exports/bulletin-UTC.txt)")
    mark_parser = sub.add_parser("mark", help="Move a claim through the review workflow")
    mark_parser.add_argument("claim_id", type=int)
    mark_parser.add_argument("status", choices=["correlating", "review", "tx_candidate", "sent", "rejected"])
    return command


def _settings_or_die(path: str):
    try:
        return load_settings(path)
    except FileNotFoundError:
        raise SystemExit(f"config not found: {path}; run `shade init` first")


def _main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "init":
        target = Path(args.config)
        if target.exists():
            print(f"refusing to overwrite {target}", file=sys.stderr)
            return 2
        example = resources.files("shade_node").joinpath("config.example.toml").read_text(encoding="utf-8")
        target.write_text(example, encoding="utf-8")
        print(f"created {target}; set a real contact in collection.user_agent")
        return 0
    settings = _settings_or_die(args.config)
    mode = normalize_mode(args.mode or args.emcomm or settings.operating_mode)
    if args.mode and args.emcomm: raise ValueError("Choose --mode or --emcomm, not both")
    if args.command == "status":
        selected = active_sources(settings, mode)
        emcomm_only = sum(1 for source in selected if source.emcomm_only)
        print(f"SHADE {__version__}")
        print(f"MODE: {mode.upper()}")
        print(f"SOURCES: {len(selected)} active ({emcomm_only} EmComm-only)")
        print("TRANSMIT CONTROL: HUMAN ONLY")
        if mode == "actual":
            print("WARNING: ACTUAL EmComm mode is active")
        elif mode == "exercise":
            print("NOTICE: all formatted traffic is marked EXERCISE")
        return 0
    if args.command == "run-once":
        result = run_once(settings, mode=mode)
        print(json.dumps(result, indent=2))
        return 1 if result["sources_ok"] == 0 and result["sources_failed"] else 0
    if args.command=='doctor':
        issues=[]
        if settings.callsign.upper() in {'','SET-ME','CHANGE-ME'}: issues.append('Set operator callsign')
        if 'example.invalid' in settings.user_agent or not any(t in settings.user_agent for t in ('@','https://')): issues.append('Set identifiable request contact')
        if settings.region_label in {'SET-REGION',''}: issues.append('Set operating region')
        if any(s.kind not in PARSERS for s in settings.sources): issues.append('Unknown collector kind')
        try:
            with closing(sqlite3.connect(Path(settings.database).resolve().as_uri()+'?mode=ro',uri=True)) as connection:
                if connection.execute('PRAGMA quick_check').fetchone()[0]!='ok': issues.append('Database integrity problem')
                for row in connection.execute("SELECT source_id,error FROM source_polls WHERE success=0 AND error<>''"):
                    issues.append(f"Source failure: {row[0]}")
        except (OSError,sqlite3.Error): issues.append('Database inaccessible')
        print('MODE: '+mode.upper()+' | TRANSMISSION: human only')
        print('SOURCES: '+str(len(active_sources(settings,mode))))
        print('SCHEDULE: use --mode standard; ACTUAL is never scheduled')
        for issue in issues: print('CHECK: '+issue)
        print('READY' if not issues else 'NOT READY')
        return 1 if issues else 0
    if (args.command=='migrate' or (args.command=='housekeep' and args.apply)) and Path(settings.database).exists():
        print('Backup: '+backup_database(settings.database))
    with connect(settings.database) as connection:
        if args.command in {'migrate','housekeep'}:
            changes=housekeeping(connection,settings.policy,apply=args.command=='migrate' or args.apply,
                                 suppress=args.command=='housekeep' and args.suppress)
            print(json.dumps({'applied':args.command=='migrate' or args.apply,'changes':changes},indent=2))
        elif args.command in {'queue','inbox','now'}:
            if args.limit<1 or (args.max_age is not None and args.max_age<0): raise ValueError('Limit must be positive and age nonnegative')
            policy=dict(settings.policy)
            if args.min_score is not None: policy['minimum_score']=args.min_score
            elif mode!='standard': policy['minimum_score']=settings.emcomm_min_queue_score
            rows=queue(connection,args.min_score,args.limit,lane='now' if args.command=='now' else args.lane,
                       policy=policy,category=args.category,area=args.area,status=args.status,max_age=args.max_age,include_suppressed=args.all)
            print(f'MODE {mode.upper()} | ID = claim ID | HUMAN REVIEW REQUIRED')
            print('ID   AGE  AREA       TYPE             SIG CONF SRC STATUS       '+('REMAINING    ' if args.command=='now' else '')+'TITLE')
            for row in rows:
                ttl=(remaining(row['expires'])+' ').ljust(13) if args.command=='now' else ''
                print(f"{row['id']:<4} {age(row['published']):<4} {row['area']:<10} {row['category'].upper():<16} {row['score']:<3} {row['confidence_score']:<4} {row['independent_families']:<3} {row['status']:<12} {ttl}{row['title'][:85]}")
            if not rows: print('No matching active reports. Use --all for suppressed evidence or shade now for current weather.')
        elif args.command == "bulletin":
            match = __import__('re').fullmatch(r'(\d+(?:\.\d+)?)([mhd])', args.window.strip().lower())
            if not match: raise ValueError('window must look like 30m, 6h, or 2d')
            amount = float(match.group(1)) * {'m': 60, 'h': 3600, 'd': 86400}[match.group(2)]
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=amount)
            categories = {x.strip() for x in args.category.split(',')} if args.category else None
            selected = []
            seen = set()
            for status in ('REVIEW', 'TX_CANDIDATE'):
                for item in queue(connection, args.min_score, 200, lane='all', policy=settings.policy, status=status):
                    if item['id'] in seen or (categories and item['category'] not in categories): continue
                    if not item['published'] or datetime.fromisoformat(item['published'].replace('Z','+00:00')) < cutoff: continue
                    claim, observations = claim_detail(connection, item['id'], settings.policy)
                    selected.append((claim, observations)); seen.add(item['id'])
            selected.sort(key=lambda pair: (pair[0]['score'], pair[0]['published']), reverse=True)
            network = settings.emcomm_network if mode != 'standard' else settings.network
            region = settings.emcomm_region_label if mode != 'standard' else settings.region_label
            limit = settings.emcomm_max_message_chars if mode != 'standard' else settings.max_message_chars
            lines = bulletin_messages(selected, callsign=settings.callsign, network=network, max_chars=limit, mode=mode, region_label=region)
            target = Path(args.output) if args.output else Path(settings.database).parent / 'exports' / ('bulletin-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.txt')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            print(f'WROTE {target} ({len(lines)} messages; {len(selected)} items)')
        elif args.command == "show":
            claim, observations = claim_detail(connection, args.claim_id,settings.policy)
            print(evidence_summary(claim, observations))
        elif args.command == "format":
            if settings.callsign.strip().upper() in {"", "SET-ME", "CHANGE-ME"}:
                print("operator.callsign must be set before formatting traffic", file=sys.stderr)
                return 2
            if mode == "actual" and not args.confirm_actual:
                print(
                    "ACTUAL EmComm formatting requires --confirm-actual; verify the situation and operator authority first",
                    file=sys.stderr,
                )
                return 2
            claim, observations = claim_detail(connection, args.claim_id,settings.policy)
            if claim['status'] not in {'REVIEW','TX_CANDIDATE'} or claim['expired']:
                raise ValueError('Formatting requires current REVIEW or TX_CANDIDATE evidence; use shade show ID first')
            network = settings.emcomm_network if mode != "standard" else settings.network
            region = settings.emcomm_region_label if mode != "standard" else settings.region_label
            max_chars = settings.emcomm_max_message_chars if mode != "standard" else settings.max_message_chars
            print(
                traffic_message(
                    claim,
                    observations,
                    callsign=settings.callsign,
                    network=network,
                    max_chars=max_chars,
                    mode=mode,
                    region_label=region,
                )
            )
        elif args.command == "mark":
            current,_=claim_detail(connection,args.claim_id,settings.policy)
            if current['expired']: raise ValueError('Expired evidence cannot advance through the active workflow')
            transition(connection, args.claim_id, args.status.upper())
            print(f"claim {args.claim_id} -> {args.status.upper()}")
    return 0


def main(argv=None):
    try:
        return _main(argv)
    except (ValueError, OSError, sqlite3.Error) as exc:
        # No traceback, credentials, or private config contents in routine CLI feedback.
        print('SHADE: '+str(exc),file=sys.stderr)
        return 2
