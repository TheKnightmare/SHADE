"""Patch-ready SHADE -> FIGHT TX_CANDIDATE snapshot adapter (contract v1).

Designed to be copied into ``src/shade_node/candidate_snapshot.py``. This
module deliberately does not use SHADE's ``connect()``: that helper may create,
migrate, or commit a database. Every export uses a separate read-only SQLite
connection and one explicit read transaction.
"""

from __future__ import annotations

import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import html
import re
import json
from pathlib import Path
import sqlite3

from shade_node.db import SCHEMA_VERSION, claim_detail, _correlated_child_statuses, _wire_warnings
from shade_node.formatters import traffic_message


CONTRACT_VERSION = 1
INBOX_CONTRACT_VERSION = 2


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
            if claim["status"] != "TX_CANDIDATE" or claim["expired"]:
                raise CandidateUnavailable("claim is expired")
            # A suppression rule (routine, weak relevance, ...) is SHADE's opinion;
            # the operator marking it TX_CANDIDATE overrides it. Keep it visible.
            suppressed_reason = claim["reason"] or ""
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
            if suppressed_reason:
                warnings.append(f"SHADE suppression overridden by operator TX_CANDIDATE: {suppressed_reason}")
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
                # Operator write-up seed for the Nomad/BBS read; not part of the
                # stable validation fields, never transmitted as-is.
                "story": {"category": claim.get("category") or "", "location": claim.get("location") or "",
                          "area": claim.get("area") or "",
                          "text": _story_text(claim["title"], observations)},
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


_TAG = re.compile(r"<[^>]+>")
_STORY_RANK = {"official": 0, "media": 1}


def _story_text(title: str, observations: list[dict], limit: int = 1500) -> str:
    """Best plain-text write-up seed for the operator: an attached article's
    description first, then official, then media bodies. RSS link-only bodies
    (just the headline and publisher) are skipped. Never invents text."""
    def rank(row):
        operator = row.get("source_id") == "operator-article"
        return (0 if operator else 1, _STORY_RANK.get(row.get("source_type"), 2), -len(row.get("body") or ""))
    head = re.sub(r"\W+", " ", (title or "").lower()).strip()
    for row in sorted(observations, key=rank):
        text = html.unescape(_TAG.sub(" ", row.get("body") or ""))
        text = re.sub(r"\s+", " ", text).strip()
        plain = re.sub(r"\W+", " ", text.lower()).strip()
        if len(text) < 60 or (head and plain.startswith(head) and len(plain) < len(head) + 60):
            continue
        return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."
    return ""


def _daily_wire_warnings(db_path, claim_id, policy, instant):
    """The Daily Wire triage warnings (non-local area, no location, stale,
    merged reports, a merged report already SENT, weak regional impact) for
    one candidate, so FIGHT can show them on its wire-candidate cards.
    Additive, optional field: older FIGHT builds simply ignore it."""
    try:
        with closing(_read_only(db_path)) as db:
            item, _ = claim_detail(db, claim_id, policy, instant)
            children = _correlated_child_statuses(db).get(claim_id, [])
            return _wire_warnings(item, children)
    except (sqlite3.DatabaseError, KeyError, TypeError, ValueError):
        return []


def _field(reports, snapshot) -> str:
    if not reports:
        return ""
    from .statrep_rollup import describe, field_matches
    story = snapshot.get("story") or {}
    return describe(field_matches(reports, title=snapshot.get("title", ""), location=story.get("location", ""),
                                  area=story.get("area", ""), category=story.get("category", "")))


def list_candidates(db_path: str | Path, *, callsign: str, network: str,
                    max_items: int = 1000, policy: dict | None = None,
                    source_instance: str = "local",
                    now: datetime | None = None) -> dict:
    """Discover current eligible TX candidates for the FIGHT inbox.

    Rows include exact formatter text for operator sorting, but no raw
    observation records. Claim IDs are machine handoff keys, never operator
    input. Selection remains advisory; FIGHT must re-export and validate a
    selected claim again before adding it to a persistent list.
    """
    if not isinstance(max_items, int) or isinstance(max_items, bool) or not 1 <= max_items <= 1000:
        raise ValueError("max_items must be between 1 and 1000")
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
            scan_limit = min(max_items * 5, 1000)
            rows = db.execute(
                "SELECT id FROM claims WHERE status='TX_CANDIDATE' "
                "ORDER BY last_seen DESC, id DESC LIMIT ?", (scan_limit + 1,)).fetchall()
            truncated = len(rows) > scan_limit
            claim_ids = [int(row["id"]) for row in rows[:scan_limit]]
        except sqlite3.DatabaseError as exc:
            raise CandidateUnavailable("SHADE database is unavailable or incompatible") from exc
        finally:
            db.execute("ROLLBACK")

    try:
        from .statrep_rollup import latest_reports
        field_reports = latest_reports(db_path, instant - timedelta(hours=24))
    except (sqlite3.Error, OSError, ValueError):
        field_reports = {}
    candidates = []
    for claim_id in claim_ids:
        try:
            snapshot = export_candidate(db_path, claim_id, callsign=callsign,
                network=network, policy=policy, source_instance=source_instance, now=instant)
        except CandidateUnavailable:
            # Expired, suppressed, hidden, merged, or otherwise stale records
            # never enter the actionable inbox.
            continue
        sources = sorted({row["source_name"] for row in snapshot["provenance"]})
        candidates.append({
            "claim_id": claim_id,
            "claim_identity": snapshot["claim_identity"],
            "revision": snapshot["revision"],
            "status": snapshot["status"],
            "title": snapshot["title"],
            "text": snapshot["text"],
            "text_sha256": snapshot["text_sha256"],
            "basis": snapshot["basis"],
            "confidence_label": snapshot["confidence_label"],
            "expires": snapshot["expires"],
            "source_count": len(snapshot["provenance"]),
            "source_names": sources,
            "field_reports": _field(field_reports, snapshot),
            "wire_warnings": [f"SHADE suppressed: {w.split(': ', 1)[1]}" for w in snapshot["warnings"]
                              if w.startswith("SHADE suppression overridden")]
                             + _daily_wire_warnings(db_path, claim_id, policy, instant),
        })
        if len(candidates) >= max_items:
            truncated = truncated or len(claim_ids) > len(candidates)
            break
    return {"contract": "shade.tx_candidate.inbox", "version": INBOX_CONTRACT_VERSION,
            "source_instance": source_instance,
            "snapshot_at": instant.isoformat().replace("+00:00", "Z"),
            "candidate_count": len(candidates), "truncated": truncated,
            "candidates": candidates}


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
