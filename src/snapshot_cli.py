"""Local, read-only JSON transport for the SHADE candidate snapshot prototype.

Pilot shim only. Install the contract inside SHADE's own CLI before release.
No radio/network operation and no SHADE DB mutation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export or revalidate one SHADE TX_CANDIDATE")
    parser.add_argument("action", choices=("export", "validate"))
    parser.add_argument("--shade-src", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--claim-id", type=int)
    parser.add_argument("--callsign")
    parser.add_argument("--network")
    parser.add_argument("--mode", choices=("standard", "exercise"), default="standard")
    parser.add_argument("--source-instance", default="local")
    args = parser.parse_args(argv)
    if not args.shade_src.is_dir() or not args.db.is_file():
        parser.error("SHADE source directory and database file must exist")
    sys.path.insert(0, str(args.shade_src.resolve()))
    from candidate_snapshot import CandidateUnavailable, export_candidate, validate_candidate

    try:
        if args.action == "export":
            if args.claim_id is None or not args.callsign or not args.network:
                parser.error("export requires --claim-id, --callsign and --network")
            result = export_candidate(args.db, args.claim_id, callsign=args.callsign,
                                      network=args.network, mode=args.mode,
                                      source_instance=args.source_instance)
        else:
            # Passing the envelope on stdin avoids command-line length limits
            # and accidental inclusion of traffic content in process listings.
            result = validate_candidate(args.db, json.load(sys.stdin))
    except (CandidateUnavailable, ValueError, KeyError, TypeError) as exc:
        print(f"candidate unavailable: {exc}", file=sys.stderr)
        return 3
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
