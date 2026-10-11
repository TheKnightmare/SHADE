# SHADE quick user guide

## Starting SHADE

Double-click **Open SHADE.vbs** in the SHADE folder (or the **SHADE** shortcut in
`C:\Station\Launch`). No terminal is needed.

A splash screen (SHADE logo and tagline) shows for at least 5 seconds while the
console loads, then the main window opens. If no configuration is found yet,
you'll be asked to select an existing `config.toml`.

The window has two tabs: **Inbox** (day-to-day review, described below) and
**Daily Wire** (reviewing, ordering, and exporting the current TX_CANDIDATE
queue — see [DAILY_WIRE.md](DAILY_WIRE.md) for the full picture). Nothing in
either tab transmits anything by itself.

## The main window

| Area | What it does |
|---|---|
| **Toolbar** | Collect now (poll public sources once), Refresh view, Sources & health, Housekeeping, Open station, Help |
| **Filters** | Lane (Inbox / Top news / NOW / All evidence), category, area, review state, "Show suppressed and routine" |
| **Search bar** | Free-text search, minimum score, max age in hours |
| **Report list** | Sortable columns — click a heading to sort, click again to reverse |
| **Detail pane** | Full evidence, scoring breakdown, and workflow status for the selected report |
| **Action row** | Start review, Correlate, TX candidate, Reject, Relay anyway, plus Bulk edit, Group duplicates, Add article, Source links, Log wire TX, Prepare message |
| **Mode selector** | STANDARD / EXERCISE / ACTUAL (top right) |

Lanes:
- **Inbox** — durable operational reports awaiting review.
- **Top news** — fresh national/world context, for checking local impact.
- **NOW** — current local life-safety weather only.
- **All evidence** — everything, including suppressed/routine when that box is checked.

## Reviewing a report

1. Select a report in the list. The detail pane shows its sources, score
   breakdown, confidence, and current state.
2. **Start review** moves it into active review.
3. **Correlate** marks it as still gathering corroborating sources — a
   holding state for "this is probably the same story as other incoming
   reports, wait before treating it as reviewed." From CORRELATING you can
   still move on to Start review, TX candidate, or Reject once it's settled;
   nothing is lost by using it, it's just a waypoint before REVIEW.
4. **TX candidate** marks it as a candidate for the wire. This requires at
   least one official or media source — a community-only report can't reach
   TX candidate without an explicit override.
5. **Relay anyway…** deliberately bypasses that corroboration requirement for
   a single report. It asks for confirmation and is permanently logged as an
   operator override (`operator_relay`) — never silent.
6. **Reject** removes a report from active consideration; it stays retained,
   not deleted.
7. **Prepare message…** (enabled once a report is REVIEW or TX_CANDIDATE)
   shows the exact text SHADE would transmit. This never sends anything —
   it's a preview you copy or save yourself, before you transmit it manually
   over radio/JS8/etc.
8. **Log wire TX…** is the *after-the-fact record* of step 7 — once you've
   actually transmitted a prepared message yourself, use this to tell SHADE
   what you sent. Enter the exact text, the real time, and the channel; after
   you confirm, it marks the report SENT and stores the transmission (visible
   later as "WIRE TX" in that report's evidence). SHADE never transmits
   anything itself — this button only records that you already did.

Marking something **TX_CANDIDATE** is a review decision, not a transmission —
every actual send stays a deliberate, separate, human action.

## Bulk actions

Select multiple reports (Ctrl/Shift-click) to enable:
- **Bulk edit…** — move all selected reports to one state (review,
  correlation, rejection, or TX_CANDIDATE). If any one report can't make the
  change (e.g. no corroboration for TX_CANDIDATE), **none** of them change —
  each report is still checked individually.
- **Group duplicates…** — merge reports describing the same event under one
  primary report. All source evidence stays attached; the others are hidden
  from the inbox but not deleted.

## Attaching outside evidence

**Add article…** lets you paste a news article URL, preview its headline and
publisher, and attach it as media evidence after you confirm it describes the
same event.

## Housekeeping

**Housekeeping…** previews which reports would expire or be suppressed.
Nothing changes until you click **Apply** — which backs up the database first
and never deletes any original evidence. If the preview changes before you
apply it, you'll be asked to reopen it.

## Modes

- **STANDARD** — normal operation.
- **EXERCISE** — marks every prepared message at both ends as an exercise.
- **ACTUAL** — requires an explicit confirmation dialog and a second
  confirmation before every individual prepared message. Never auto-enabled.

The app always opens in STANDARD.

## Automatic collection

A Windows Scheduled Task ("SHADE Collector") polls public sources every 15
minutes in the background — no console window appears, and it runs whether or
not the desktop console is open. **Collect now** in the toolbar polls
on-demand and shares the same source cooldowns, so it won't double-poll a
source that was just checked.

## The Daily Wire tab

Shows every current TX_CANDIDATE report in one place, with a **Warnings**
column called out separately — a GLOBAL/NATIONAL item, a missing location, a
stale item, or a correlated duplicate all get flagged there, visibly, rather
than being silently dropped or silently included.

- **Move up / Move down** reorders the list for your own transmission
  priority. **Operator note** attaches a free-text note. **Remove from Daily
  Wire** hides an item from this view without rejecting or changing its
  review state — "Show removed items" brings it back.
- **Back to review** / **Reject…** are the same review actions as the Inbox
  tab, just reachable from here too.
- **Preview & export…** shows the exact text that would go out, with the
  same Copy/Save options as everywhere else in SHADE. It never transmits
  anything by itself.

`shade daily-wire` (from a terminal, in the SHADE folder) does the same thing
as a quick command-line listing, sorted for transmission priority, if you
just want a fast text view without opening the GUI.

## Good to know

- The view refreshes automatically once a minute while idle; collection is
  never scheduled automatically from inside this window.
- Closing and reopening SHADE is always safe once the current operation
  finishes — configuration and evidence stay local.
- Nothing in this console ever transmits, posts, or sends anything by itself.
  Every transmission is something you did yourself, outside SHADE, that you
  then optionally record.
