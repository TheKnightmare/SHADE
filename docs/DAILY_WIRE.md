# Daily Wire

## What it is

Daily Wire is a SHADE-side review, assembly, and export view over the
existing `TX_CANDIDATE` queue. It is not a new workflow state, not a second
formatter, and not a transmission mechanism. Everything it shows is a claim
SHADE has already marked `TX_CANDIDATE` through the normal review workflow
(Start review → TX candidate, or an audited operator relay override).

## The STATION → SHADE → FIGHT boundary

```
STATION → SHADE → FIGHT → radio/BBS/digital channels
```

- **SHADE** collects, attributes, deduplicates/correlates, scores, and
  reviews claims, producing `TX_CANDIDATE` material. Daily Wire lives here.
- **FIGHT** (in the separate StationWatch application) separately reads
  `claims.status = 'TX_CANDIDATE'` directly from SHADE's database (read-only,
  `PRAGMA query_only=1`) and stages it for manual transmission. FIGHT does
  not decide what is true or worthy - it only formats and stages what SHADE
  has already approved.

**`TX_CANDIDATE` is the one membership signal both Daily Wire and FIGHT read.**
Daily Wire was deliberately *not* given a second status (no `WIRE_APPROVED`,
no `DEFERRED`) that a claim would leave `TX_CANDIDATE` to enter, because
FIGHT's query only ever matches `TX_CANDIDATE` - anything moved to a new
status would silently vanish from FIGHT's view. Ordering, operator notes, and
"remove from Daily Wire view" are stored in a separate table
(`wire_annotations`: claim ID, position, note, hidden flag, updated
timestamp) that FIGHT never reads and that never touches `claims.status`.

This repo does not modify StationWatch or FIGHT, and none of the tests below
depend on it - a comparison test in `tests/test_daily_wire.py` re-implements
FIGHT's exact `SELECT id FROM claims WHERE status='TX_CANDIDATE'` query as a
standalone check and asserts Daily Wire's membership matches it exactly, on
the same fixture.

## Using it

Open the **Daily Wire** tab next to Inbox. It shows every current
`TX_CANDIDATE` claim with:

- Filters: area, category, a free-text search (title/location), and a
  "Warnings only" toggle.
- A **Warnings** column, shown per item, never hidden:
  - `GLOBAL`/`NATIONAL`/`DISTANT` item in a regional Daily Wire
  - No location stated (only raised when `area != 'ETN/WNC'` - a genuine
    local item is often classified `ETN/WNC` from title/body text alone,
    with no separately populated `location` field, and that's normal, not a
    defect; `area` is the primary geography signal here, `location` is
    secondary)
  - Stale or expired
  - N correlated/merged report(s) folded into this claim
  - A correlated child claim was previously marked SENT
  - Weak or unclear regional impact (any non-`ETN/WNC` area)
- **Move up / Move down** - reorders the visible list; persisted to
  `wire_annotations.position`.
- **Operator note** - free text per claim, persisted to
  `wire_annotations.note`.
- **Remove from Daily Wire** - toggles `wire_annotations.hidden`. This only
  hides an item from this view; it does not reject, suppress, or otherwise
  change the claim's workflow state. "Show removed items" brings it back.
- **Back to review / Reject…** - the same `transition()`-backed workflow
  actions the Inbox tab already exposes, applied to the selected Daily Wire
  item. These are the *only* things in this tab that ever change
  `claims.status`, and they require the same confirmation dialogs as
  elsewhere.
- **Preview & export…** - renders the currently filtered/ordered list as one
  bulletin using the existing `formatters.bulletin_messages()` (the same
  formatter the Inbox's reviewed-batch export already uses), with Copy/Save
  actions identical to the existing message preview dialog. **This never
  transmits anything and never calls FIGHT.** FIGHT's own staging
  (`shade format <id>` via its CLI) is a separate, unrelated code path in
  StationWatch that this feature does not touch, duplicate, or invoke.

Nothing in Daily Wire - viewing, filtering, sorting, reordering, annotating,
hiding, previewing, or exporting - changes a claim's `status` unless the
operator explicitly clicks an existing workflow action button and confirms
it.

## Historical SENT semantics

Transmission logging (`wire_transmissions`, `Log wire TX…`) was added after
some claims had already been marked `SENT`. Those older `SENT` claims are
valid historical evidence, not a data problem, and nothing in SHADE infers,
backfills, or fabricates a channel or message for them. `workflow_status()`
now shows one of two distinct labels wherever a claim's status is displayed
(Inbox list, claim detail, Daily Wire):

- `SENT — transmission details available` (a real `wire_transmissions` row exists)
- `SENT — no transmission record; predates recording` (it does not)

## Database changes

One new, purely additive table (`wire_annotations`) was added to
`AUXILIARY_SCHEMA`. It has no `CHECK`-constrained enum column and no
relationship FIGHT reads, so it needed no `SCHEMA_VERSION` bump and no
rename/rebuild migration - `connect()`'s existing `REQUIRED_OBJECTS`
mechanism backs up and adds it automatically the first time it's missing,
exactly as designed for additive schema changes. No existing table, column,
or `CHECK` constraint was altered.

## Performance

`daily_wire()` queries `claims` directly by `status='TX_CANDIDATE'` (matching
FIGHT's own raw-column read) and reuses the same bulk-prefetch,
transaction-wrapped evidence loading `queue()` already uses
(`_bulk_evidence_context`/`_claim_detail_bulk`) - one claims query, one bulk
evidence pass, regardless of candidate count. It deliberately does not call
`queue(status='TX_CANDIDATE')`, because `queue()`'s row loop filters on
`assess()`'s *recomputed* display status (which overrides to `EXPIRED` once a
claim's evidence has expired) rather than the raw `claims.status` column -
that would have silently dropped exactly the stale-but-still-`TX_CANDIDATE`
claims this view needs to flag, and diverged from what FIGHT actually sees.
Filtering/sorting in the GUI (area, category, search, warnings-only) happens
entirely client-side over the already-loaded list - no additional queries.
Measured against the live production database (9 current `TX_CANDIDATE`
claims, ~9,850 total claims): 165-215ms per full `daily_wire()` call.
