# STANDARD, EXERCISE and ACTUAL

No profile transmits, controls radio software, keys PTT or declares an emergency.
The licensed human remains responsible for review and any subsequent traffic.

```powershell
shade --mode standard status
shade --mode standard run-once
shade inbox
shade now
shade --mode exercise inbox
shade --mode exercise format 214
shade --mode actual format 214 --confirm-actual
```

`--emcomm exercise` and `--emcomm actual` remain compatible aliases. Do not
combine --mode and --emcomm. Command-line mode overrides end with that command.
STANDARD excludes emcomm_only sources; EXERCISE/ACTUAL include approved ones.
Lower EmComm score thresholds do not bypass expiration or routine-content gates.
Use explicit --all filters for a wider view. Formatting requires REVIEW or
TX_CANDIDATE. Replace 214 with the actual claim ID.

EXERCISE copy has EXERCISE at both ends, even after title truncation. ACTUAL
formatting refuses to proceed without --confirm-actual. Confirmation only permits
text formatting; it does not establish emergency authority or perform RF actions.
Mandatory mode/attribution markings are never truncated to fit an undersized limit.

Review the source URLs, confidence explanation, dates and area with `show`.
Uncorroborated community reporting remains UNVERIFIED. A CONFIRMED label describes
configured independent origins; it is not proof of truth. Sources that relay a
common report must use the same family.

Workflow: NEW -> CORRELATING/REVIEW -> TX_CANDIDATE -> SENT; rejection is available
where shown by `show`. SENT is only a manual historical annotation. Expired
reports cannot advance manually. A new source validity extension can reopen an expired report as NEW, with an audit entry. Suppressed NEW reports may be returned to REVIEW.

Scheduled operations must explicitly use `--mode standard`; this wins over a
persistent ACTUAL setting. The project creates no tasks automatically. See
SCHEDULING.md for manual update instructions.
