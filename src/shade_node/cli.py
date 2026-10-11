from __future__ import annotations

import argparse
from importlib import resources
import json
from pathlib import Path
import sys

from .db import (claim_detail, connect, queue, transition, housekeeping, backup_database,
                  suppression_review, ALLOWED_TRANSITIONS, MIGRATION_AUDIT_PREFIX, SCHEMA_VERSION,
                  FEEDBACK_ACTIONS, record_feedback, feedback_report, bulk_transition)
from .relevance import age, remaining
from .collectors import PARSERS
import sqlite3
from contextlib import closing
from . import __version__
from .engine import active_sources, load_settings, normalize_mode, run_once
from .discovery_audit import diagnose
from .benchmark import audit_csv
from .formatters import evidence_summary, traffic_message, bulletin_messages, workflow_status
from .candidate_snapshot import export_candidate, list_candidates, validate_candidate
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
    missed = sub.add_parser('why-missed', help='Trace a missed headline or URL through stored discovery evidence')
    missed.add_argument('headline_or_url')
    benchmark = sub.add_parser('benchmark', help='Score a dated independent URL benchmark against discovery and inbox')
    benchmark.add_argument('csv_path')
    sub.add_parser('doctor',help='Validate setup without displaying private values or polling sources')
    sub.add_parser('migrate',help='Back up legacy database, migrate safely and expire ended claims')
    sub.add_parser('suppression-review', help='Read-only list of old suppressions whose reasons no longer apply')
    clean=sub.add_parser('housekeep',help='Preview expiration/suppression; --apply is explicit confirmation')
    clean.add_argument('--apply',action='store_true')
    clean.add_argument('--suppress',action='store_true',help='Also suppress low-value NEW claims')
    purge_p=sub.add_parser('purge',help='Preview deleting claims not seen for N hours (max 72); --apply deletes after a verified backup')
    purge_p.add_argument('--hours',type=float,default=72)
    purge_p.add_argument('--apply',action='store_true')
    for name in ('inbox','queue','now'):
        view=sub.add_parser(name,help='Active local life-safety lane' if name=='now' else 'Durable operator inbox (ID is the claim ID)',
                            epilog='Examples: shade inbox --category cyber; shade now --area REGIONAL --all; shade inbox --status EXPIRED --all --lane all')
        view.add_argument('--min-score',type=int)
        view.add_argument('--limit',type=int,default=50)
        view.add_argument('--category')
        view.add_argument('--area',choices=['ETN/WNC','REGIONAL','NATIONAL','GLOBAL','DISTANT'])
        view.add_argument('--status',choices=list(ALLOWED_TRANSITIONS))
        view.add_argument('--max-age',type=float,help='Maximum published age in hours')
        view.add_argument('--all',action='store_true',help='Show persisted SUPPRESSED claims and routine-filtered evidence; NOW still excludes ended or unknown-expiration items')
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
    relay_parser = sub.add_parser("relay", help="Explicitly make a reviewed claim an operator-relay TX candidate")
    relay_parser.add_argument("claim_id", type=int)
    bulk_parser = sub.add_parser("bulk-mark", help="Move several claims through the workflow in one call")
    bulk_parser.add_argument("claim_ids", help="comma-separated claim IDs, e.g. 12,45,201")
    bulk_parser.add_argument("status", choices=["correlating", "review", "tx_candidate", "sent", "rejected"])
    bulk_parser.add_argument("--relay", action="store_true", help="use the operator-relay TX_CANDIDATE override for every claim")
    feedback_parser = sub.add_parser("feedback", help="Record an auditable operator decision (accept/reject/downgrade/suppress/promote/mark)")
    feedback_parser.add_argument("claim_id", type=int)
    feedback_parser.add_argument("action", choices=sorted(FEEDBACK_ACTIONS))
    feedback_parser.add_argument("--reason", default="", help="short selectable reason code")
    feedback_parser.add_argument("--note", default="", help="free-text note")
    feedback_parser.add_argument("--relay", action="store_true", help="use the operator-relay TX_CANDIDATE override")
    feedback_report_parser = sub.add_parser("feedback-report", help="Read-only summary of recurring operator decisions")
    feedback_report_parser.add_argument("--group-by", choices=["source", "category", "geography", "rule"], default="category")
    feedback_report_parser.add_argument("--action", choices=sorted(FEEDBACK_ACTIONS))
    feedback_report_parser.add_argument("--limit", type=int, default=50)
    wire_parser = sub.add_parser("daily-wire", help="List/export current TX_CANDIDATE items for the Daily Wire")
    wire_parser.add_argument("--area")
    wire_parser.add_argument("--category")
    wire_parser.add_argument("--min-confidence", choices=["UNVERIFIED", "REPORTED", "CORROBORATED", "OFFICIAL-REPORT", "CONFIRMED"])
    wire_parser.add_argument("--sort", choices=["freshness", "confidence", "area", "corroboration"], default="freshness")
    wire_parser.add_argument("--output", help="write the listing to this path instead of stdout")
    fight_inbox = sub.add_parser(
        "fight-inbox",
        help="Read-only eligible-candidate text and metadata JSON for the FIGHT local workspace",
    )
    fight_inbox.add_argument("--limit", type=int, default=1000)
    fight_inbox.add_argument("--source-instance", default="local")
    fight_inbox.add_argument("--callsign", help="expected operator callsign; must match the selected profile")
    fight_inbox.add_argument("--network", help="expected network label; must match the selected profile")
    fight_export = sub.add_parser(
        "fight-export", help="Read-only current candidate snapshot JSON for the FIGHT workspace")
    fight_export.add_argument("--claim-id", type=int, required=True,
                              help="machine handoff identity selected from fight-inbox; not operator-entered")
    fight_export.add_argument("--callsign", required=True)
    fight_export.add_argument("--network", required=True)
    fight_export.add_argument("--candidate-mode", choices=["standard", "exercise"], default="standard")
    fight_export.add_argument("--source-instance", default="local")
    fight_validate = sub.add_parser(
        "fight-validate", help="Read-only revalidate a FIGHT candidate snapshot from UTF-8 JSON on stdin")
    fight_validate.add_argument("--callsign", required=True)
    fight_validate.add_argument("--network", required=True)
    fight_statreps = sub.add_parser(
        "fight-statreps", help="Read-only CommStat STATREP roll-up JSON for the FIGHT wire")
    fight_statreps.add_argument("--hours", type=float, default=24)
    fight_statreps.add_argument("--scope", choices=["region", "all"], default="region")
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
    if args.command == "fight-statreps":
        from .statrep_rollup import rollup
        print(json.dumps(rollup(settings.database, hours=args.hours, scope=args.scope,
                                area_label=settings.region_label), ensure_ascii=False, sort_keys=True))
        return 0
    if args.command in {"fight-inbox", "fight-export", "fight-validate"}:
        if mode not in {"standard", "exercise"}:
            raise ValueError(f"{args.command} supports standard or exercise mode only")
        if args.callsign and args.callsign.strip().upper() != settings.callsign.strip().upper():
            raise ValueError(f"{args.command} callsign does not match the selected SHADE profile")
        if args.network and args.network.strip() != settings.network.strip():
            raise ValueError(f"{args.command} network does not match the selected SHADE profile")
        if args.command == "fight-inbox":
            if not 1 <= args.limit <= 1000:
                raise ValueError("fight-inbox --limit must be between 1 and 1000")
            result = list_candidates(
                settings.database, callsign=settings.callsign, network=settings.network,
                max_items=args.limit, policy=settings.policy,
                source_instance=args.source_instance,
            )
        elif args.command == "fight-export":
            result = export_candidate(
                settings.database, args.claim_id, callsign=settings.callsign,
                network=settings.network, mode=args.candidate_mode,
                region_label=settings.region_label, max_chars=settings.max_message_chars,
                policy=settings.policy, source_instance=args.source_instance,
            )
        else:
            snapshot = json.load(sys.stdin)
            if not isinstance(snapshot, dict):
                raise ValueError("fight-validate expects a JSON object on stdin")
            fmt = snapshot.get("format")
            candidate_callsign = fmt.get("callsign") if isinstance(fmt, dict) else None
            candidate_network = fmt.get("network") if isinstance(fmt, dict) else None
            if not isinstance(candidate_callsign, str) or candidate_callsign.strip().upper() != settings.callsign.strip().upper():
                raise ValueError("fight-validate candidate callsign does not match the selected SHADE profile")
            if not isinstance(candidate_network, str) or candidate_network != settings.network:
                raise ValueError("fight-validate candidate network does not match the selected SHADE profile")
            if not isinstance(snapshot.get("mode"), str) or snapshot["mode"] not in {"standard", "exercise"}:
                raise ValueError("fight-validate supports standard or exercise snapshots only")
            result = validate_candidate(settings.database, snapshot, policy=settings.policy)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
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
    if args.command == 'benchmark':
        with connect(settings.database) as connection:
            report = audit_csv(connection, settings.database, args.csv_path, settings.policy)
        print(json.dumps(report, indent=2))
        return 0
    if args.command == 'why-missed':
        with connect(settings.database) as connection:
            report = diagnose(connection, settings.database, args.headline_or_url, settings.policy)
        print(json.dumps(report, indent=2))
        return 0
    if args.command == "run-once":
        result = run_once(settings, mode=mode)
        print(json.dumps(result, indent=2))
        return 1 if result["sources_ok"] == 0 and (result["sources_failed"] or result["sources_partial"]) else 0
    if args.command=='doctor':
        issues=[]
        if settings.callsign.upper() in {'','SET-ME','CHANGE-ME'}: issues.append('Set operator callsign')
        if 'example.invalid' in settings.user_agent or not any(t in settings.user_agent for t in ('@','https://')): issues.append('Set identifiable request contact')
        if settings.region_label in {'SET-REGION',''}: issues.append('Set operating region')
        if any(s.kind not in PARSERS for s in settings.sources): issues.append('Unknown collector kind')
        try:
            with closing(sqlite3.connect(Path(settings.database).resolve().as_uri()+'?mode=ro',uri=True)) as connection:
                if connection.execute('PRAGMA quick_check').fetchone()[0]!='ok': issues.append('Database integrity problem')
                if connection.execute('PRAGMA user_version').fetchone()[0] < SCHEMA_VERSION: issues.append('Database migration required; run shade migrate')
                active_ids = {source.id for source in active_sources(settings, mode)}
                configured = {source.id: source for source in settings.sources}
                for row in connection.execute("SELECT source_id,error FROM source_polls WHERE success=0 AND error<>''"):
                    if row[0] in active_ids:
                        issues.append(f"Source failure: {row[0]}: {row[1]}")
                    elif row[0] in configured and not configured[row[0]].enabled:
                        issues.append(f"Disabled source, last failure: {row[0]}: {row[1]}")
        except (OSError,sqlite3.Error): issues.append('Database inaccessible')
        print('MODE: '+mode.upper()+' | TRANSMISSION: human only')
        print('SOURCES: '+str(len(active_sources(settings,mode))))
        print('SCHEDULE: use --mode standard; ACTUAL is never scheduled')
        for issue in issues: print('CHECK: '+issue)
        print('READY' if not issues else 'NOT READY')
        return 1 if issues else 0
    migration_needed = False
    if args.command=='migrate' and Path(settings.database).exists():
        with closing(sqlite3.connect(Path(settings.database).resolve().as_uri()+'?mode=ro',uri=True)) as current:
            migration_needed = current.execute('PRAGMA user_version').fetchone()[0] < SCHEMA_VERSION
    verified_backup = None
    if (args.command=='migrate' or (args.command=='housekeep' and args.apply)) and Path(settings.database).exists():
        verified_backup = backup_database(settings.database)
        print('Backup: '+verified_backup)
    with connect(settings.database, allow_migrate=args.command=='migrate',
                 verified_backup=verified_backup if args.command=='migrate' else None) as connection:
        if args.command == 'purge':
            from .db import purge_plan, purge as purge_claims, compact
            plan = purge_plan(connection, args.hours)
            summary = {'cutoff': plan['cutoff'], 'eligible_claims': len(plan['claim_ids']),
                       'protected_old_claims': plan['protected_old'], 'total_claims': plan['total_claims'], 'applied': False}
            if args.apply and plan['claim_ids']:
                summary['backup'] = backup_database(settings.database)
                summary.update(applied=True, deleted=purge_claims(connection, plan))
            print(json.dumps(summary, indent=2))
        elif args.command == 'suppression-review':
            print(json.dumps({'candidates': suppression_review(connection, settings.policy)}, indent=2))
        elif args.command in {'migrate','housekeep'}:
            changes=housekeeping(connection,settings.policy,apply=args.command=='migrate' or args.apply,
                                 suppress=args.command=='housekeep' and args.suppress)
            migration_rows = connection.execute(
                'SELECT claim_id,tx_candidate_basis,reason FROM workflow_audit WHERE reason LIKE ? ORDER BY id',
                (MIGRATION_AUDIT_PREFIX+'%',),
            ).fetchall() if migration_needed else []
            print(json.dumps({'applied':args.command=='migrate' or args.apply,'changes':changes,
                              'tx_candidate_basis_backfills':[dict(row) for row in migration_rows]},indent=2))
        elif args.command in {'queue','inbox','now'}:
            if args.limit<1 or (args.max_age is not None and args.max_age<0): raise ValueError('Limit must be positive and age nonnegative')
            policy=dict(settings.policy)
            if args.min_score is not None: policy['minimum_score']=args.min_score
            elif mode!='standard': policy['minimum_score']=settings.emcomm_min_queue_score
            rows=queue(connection,args.min_score,args.limit,lane='now' if args.command=='now' else args.lane,
                       policy=policy,category=args.category,area=args.area,status=args.status,max_age=args.max_age,include_suppressed=args.all)
            print(f'MODE {mode.upper()} | ID = claim ID | HUMAN REVIEW REQUIRED')
            print('ID   AGE  AREA       TYPE             SIG CONFIDENCE                       SRC STATUS                         '+('REMAINING    ' if args.command=='now' else '')+'TITLE')
            for row in rows:
                ttl=(remaining(row['expires'])+' ').ljust(13) if args.command=='now' else ''
                print(f"{row['id']:<4} {age(row['published']):<4} {row['area']:<10} {row['category'].upper():<16} {row['score']:<3} {row['confidence_label']:<32} {row['independent_families']:<3} {workflow_status(row):<30} {ttl}{row['title'][:85]}")
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
        elif args.command == 'relay':
            current,_=claim_detail(connection,args.claim_id,settings.policy)
            if current['expired']: raise ValueError('Expired evidence cannot advance through the active workflow')
            transition(connection,args.claim_id,'TX_CANDIDATE',operator_override=True)
            print(f"claim {args.claim_id} -> TX_CANDIDATE (operator relay)")
        elif args.command == 'bulk-mark':
            ids = [int(x.strip()) for x in args.claim_ids.split(',') if x.strip()]
            results = bulk_transition(connection, ids, args.status.upper(), operator_override=args.relay)
            for r in results:
                print(f"claim {r['claim_id']} -> {args.status.upper()}" if r['ok'] else
                      f"claim {r['claim_id']}: FAILED - {r['error']}")
            failed = sum(1 for r in results if not r['ok'])
            print(f"{len(results)-failed}/{len(results)} succeeded")
            return 1 if failed else 0
        elif args.command == 'feedback':
            record_feedback(connection, args.claim_id, args.action, reason_code=args.reason,
                             note=args.note, operator_override=args.relay)
            print(f"claim {args.claim_id}: feedback recorded ({args.action})")
        elif args.command == 'feedback-report':
            rows = feedback_report(connection, group_by=args.group_by, action=args.action, limit=args.limit)
            if not rows:
                print('No operator feedback recorded yet.')
            else:
                print(f"{'GROUP':<28} {'ACTION':<22} COUNT")
                for row in rows:
                    print(f"{(row['group_key'] or '(none)'):<28} {row['action']:<22} {row['n']}")
        elif args.command == 'daily-wire':
            rows = queue(connection, 0, 10000, lane='all', policy=settings.policy, status='TX_CANDIDATE',
                         category=args.category, area=args.area)
            conf_rank = {'UNVERIFIED': 0, 'REPORTED': 1, 'CORROBORATED': 2, 'OFFICIAL-REPORT': 3, 'CONFIRMED': 4}
            if args.min_confidence:
                floor = conf_rank[args.min_confidence]
                rows = [r for r in rows if conf_rank.get(r['confidence_label'].split(' ')[0], 0) >= floor]
            sort_key = {
                'freshness': lambda r: r['published'] or '',
                'confidence': lambda r: conf_rank.get(r['confidence_label'].split(' ')[0], 0),
                'area': lambda r: r['area'],
                'corroboration': lambda r: r['independent_families'],
            }[args.sort]
            rows = sorted(rows, key=sort_key, reverse=True)
            lines = [f"DAILY WIRE - {len(rows)} TX_CANDIDATE item(s), sorted by {args.sort}",
                     f"{'ID':<6} {'AGE':<5} {'AREA':<10} {'CATEGORY':<16} {'CONFIDENCE':<20} {'FAM':<4} {'BASIS':<16} TITLE"]
            for row in rows:
                basis = (row.get('tx_candidate_basis') or 'basis missing')
                lines.append(f"{row['id']:<6} {age(row['published']):<5} {row['area']:<10} "
                              f"{row['category'].upper():<16} {row['confidence_label'].split(' ')[0]:<20} "
                              f"{row['independent_families']:<4} {basis:<16} {row['title'][:70]}")
            text = '\n'.join(lines)
            if args.output:
                Path(args.output).write_text(text + '\n', encoding='utf-8')
                print(f'WROTE {args.output} ({len(rows)} items)')
            else:
                print(text)
    return 0


def main(argv=None):
    # Some evidence titles/bodies carry characters (emoji, non-Latin scripts)
    # a Windows console's default codepage can't represent. Without this,
    # printing one crashed the ENTIRE command with an opaque
    # "'charmap' codec can't encode character" error - not a traceback (the
    # except clause below already catches UnicodeEncodeError, a ValueError
    # subclass), but the operator got no output at all instead of the report
    # they asked for. reconfigure() is a no-op on already-UTF-8 streams and
    # degrades safely (best-effort) on any stream that doesn't support it.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):
            pass
    try:
        return _main(argv)
    except (ValueError, OSError, sqlite3.Error) as exc:
        # No traceback, credentials, or private config contents in routine CLI feedback.
        print('SHADE: '+str(exc),file=sys.stderr)
        return 2
