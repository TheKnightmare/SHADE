"""Patch-ready SHADE -> FIGHT TX_CANDIDATE snapshot adapter (contract v1).

Designed to be copied into ``src/shade_node/candidate_snapshot.py``. This
module deliberately does not use SHADE's ``connect()``: that helper may create,
migrate, or commit a database. Every export uses a separate read-only SQLite
connection and one explicit read transaction.
"""

from __future__ import annotations

import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from shade_node.db import SCHEMA_VERSION, claim_detail
from shade_node.formatters import traffic_message


CONTRACT_VERSION = 1


class CandidateUnavailable(ValueError):
    """The selected claim cannot safely be exported or is no longer current."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _utc(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _read_only(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path).resolve(strict=True)
    connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True,
                                 timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def export_candidate(db_path: str | Path, claim_id: int, *, callsign: str,
                     network: str, mode: str = "standard", region_label: str = "",
                     max_chars: int = 500, policy: dict | None = None,
                     source_instance: str = "local",
                     now: datetime | None = None) -> dict:
    """Return one immutable-by-value, JSON-safe candidate snapshot.

    No DB mutations and no queue-wide prefetch. ``now`` is fixed for the whole
    read and formatting pass. ``standard`` and ``exercise`` are supported;
    ACTUAL needs a separate operator-confirmation design before integration.
    A hidden Daily Wire item is denied in this pilot even though SHADE's
    existing FIGHT adapter currently treats that flag as view-only.
    """
    if not isinstance(claim_id, int) or isinstance(claim_id, bool) or claim_id < 1:
        raise ValueError("claim_id must be a positive integer")
    if mode not in {"standard", "exercise"}:
        raise ValueError("only standard/exercise snapshots are supported")
    if not callsign.strip() or not network.strip() or not source_instance.strip():
        raise ValueError("callsign, network, and source_instance are required")
    instant = _utc(now)
    try:
        connection = _read_only(db_path)
    except (OSError, sqlite3.DatabaseError) as exc:
        raise CandidateUnavailable("SHADE database is unavailable") from exc
    with closing(connection) as db:
        db.execute("BEGIN")
        try:
            schema = db.execute("PRAGMA user_version").fetchone()[0]
            if schema != SCHEMA_VERSION:
                raise CandidateUnavailable(f"SHADE schema {schema} is not supported")
            stored = db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            if stored is None or stored["status"] != "TX_CANDIDATE":
                raise CandidateUnavailable("claim is missing or not TX_CANDIDATE")
            if stored["tx_candidate_basis"] not in {"corroborated", "operator_relay"}:
                raise CandidateUnavailable("TX_CANDIDATE basis is missing or unknown")
            if db.execute("SELECT 1 FROM claim_merges WHERE child_id=?", (claim_id,)).fetchone():
                raise CandidateUnavailable("merged child claims cannot be exported")
            annotation = db.execute("SELECT * FROM wire_annotations WHERE claim_id=?",
                                    (claim_id,)).fetchone()
            if annotation and annotation["hidden"]:
                raise CandidateUnavailable("claim is hidden from the Daily Wire view")
            claim, observations = claim_detail(db, claim_id, policy, instant)
            if claim["status"] != "TX_CANDIDATE" or claim["expired"] or claim["reason"]:
                raise CandidateUnavailable("claim is expired or currently suppressed")
            if not observations:
                raise CandidateUnavailable("claim has no evidence")
            if stored["tx_candidate_basis"] == "corroborated" and not any(
                    row["source_type"] in {"official", "media"} for row in observations):
                raise CandidateUnavailable("corroborated basis has no official/media evidence")

            message = traffic_message(claim, observations, callsign=callsign,
                                      network=network, max_chars=max_chars,
                                      mode=mode, region_label=region_label)
            payload = message.encode("utf-8")
            provenance = [{"observation_id": row["id"],
                           "revision_id": row.get("revision_id"),
                           "source_id": row["source_id"],
                           "source_family": row["source_family"],
                           "source_type": row["source_type"],
                           "source_name": row["source_name"],
                           "external_id": row["external_id"],
                           "url": row["url"],
                           "content_hash": row["content_hash"],
                           "published_at": row["published_at"],
                           "observed_at": row["observed_at"],
                           "display_role": row["display_role"]}
                          for row in observations]
            warnings = []
            if stored["tx_candidate_basis"] == "corroborated":
                warnings.append("Corroborated basis may mean one official/media observation; it does not prove independent confirmation.")
            else:
                warnings.append("Operator-relay override; no independent confirmation is implied.")
            if claim.get("confidence_explanation"):
                warnings.append("SHADE confidence: " + claim["confidence_explanation"])
            # Include the complete stored claim, evidence (including raw
            # metadata/revisions), and latest workflow identity. This token is
            # an optimistic guard, not a lock on future operator actions.
            audit = db.execute("SELECT * FROM workflow_audit WHERE claim_id=? ORDER BY id DESC LIMIT 1",
                               (claim_id,)).fetchone()
            revision = _digest({"claim": dict(stored), "observations": observations,
                                "workflow_audit": dict(audit) if audit else None,
                                "wire_annotation": dict(annotation) if annotation else None})
            result = {
                "contract": "shade.tx_candidate.snapshot", "version": CONTRACT_VERSION,
                "claim_id": claim_id, "claim_identity": f"shade:{source_instance}:claim:{claim_id}",
                "source": "SHADE", "source_instance": source_instance,
                "snapshot_at": instant.isoformat().replace("+00:00", "Z"),
                "revision": revision, "status": "TX_CANDIDATE",
                "basis": stored["tx_candidate_basis"], "mode": mode,
                "policy_sha256": _digest(policy or {}),
                "format": {"callsign": callsign, "network": network,
                           "region_label": region_label, "max_chars": max_chars},
                "title": claim["title"], "expires": claim["expires"],
                "confidence_label": claim["confidence_label"],
                "provenance": provenance, "warnings": warnings,
                "text": message, "text_utf8_b64": base64.b64encode(payload).decode("ascii"),
                "text_sha256": hashlib.sha256(payload).hexdigest(),
            }
            return result
        except CandidateUnavailable:
            # Policy/eligibility failures are not database failures. Preserve
            # their actionable reason for the operator and FIGHT caller.
            raise
        except (sqlite3.DatabaseError, KeyError, TypeError, IndexError, ValueError) as exc:
            raise CandidateUnavailable("SHADE database is unavailable or incompatible") from exc
        finally:
            db.execute("ROLLBACK")


def validate_candidate(db_path: str | Path, snapshot: dict, *,
                       policy: dict | None = None, now: datetime | None = None) -> dict:
    """Re-read immediately before staging; deny changed or ineligible claims.

    FIGHT must stage only after this succeeds and must bind its artifact to
    ``text_sha256``. A later state change still requires another validation.
    """
    if snapshot.get("contract") != "shade.tx_candidate.snapshot" or snapshot.get("version") != CONTRACT_VERSION:
        raise CandidateUnavailable("unknown candidate contract")
    try:
        payload = base64.b64decode(snapshot["text_utf8_b64"], validate=True)
        if (payload != snapshot["text"].encode("utf-8") or
                hashlib.sha256(payload).hexdigest() != snapshot["text_sha256"]):
            raise CandidateUnavailable("candidate text and byte hash disagree")
    except (KeyError, TypeError, ValueError) as exc:
        raise CandidateUnavailable("candidate text is malformed") from exc
    fmt = snapshot.get("format") or {}
    try:
        current = export_candidate(db_path, snapshot["claim_id"], callsign=fmt["callsign"],
                                   network=fmt["network"], region_label=fmt["region_label"],
                                   max_chars=fmt["max_chars"], mode=snapshot["mode"],
                                   policy=policy, source_instance=snapshot["source_instance"], now=now)
    except (KeyError, TypeError, ValueError) as exc:
        raise CandidateUnavailable("candidate is malformed or no longer eligible") from exc
    stable_fields = ("claim_id", "claim_identity", "source", "source_instance", "revision",
                     "status", "basis", "mode", "policy_sha256", "format", "title", "expires",
                     "confidence_label", "provenance", "warnings", "text", "text_utf8_b64",
                     "text_sha256")
    if any(current[field] != snapshot.get(field) for field in stable_fields):
        raise CandidateUnavailable("candidate changed since snapshot")
    return current
