# SHADE next five: operator workflow

These five fixes are installed in the running SHADE checkout as of September 26, 2026. The separate configuration layering design branch remains unmerged.

## Location before wire copy

The bundled `gazetteer_2026.csv` contains county and place names for AL, GA, KY, NC, TN, and WV from the U.S. Census Bureau's 2026 Gazetteer ZIP files. Regenerate it with `python scripts/generate_gazetteer.py`. The matcher accepts explicit county and place plus state phrases, including `Co.`; a unique place name can be used without a state. Ambiguous names remain unlabeled. The wire formatter uses the resolved label, and the regional area score uses a resolved location when one is clear. It does not use a place name to assert county membership.

## Duplicate coverage

Select two or more reports and choose **Group duplicates…**. Select the primary report. SHADE checks category and headline overlap, retains every observation on its original report, links the reports, and hides the secondary reports from the active inbox. The primary report shows all source evidence and recomputed family count. Future long, identical headlines published within six hours also join automatically. Identical syndicated coverage within six hours counts as one effective originating family for confidence. Manual grouping remains available for headlines that differ. Reports already sent, expired, or merged cannot be grouped.

## Original source links

**Source links…** shows direct publisher observations first and labels Google News RSS links as Google News results. **Add article…** previews the final public HTTPS URL and publisher metadata and asks for confirmation. If its headline shares few terms with the selected report, SHADE asks for an extra event/place/date check. Google News RSS URLs do not reliably redirect to publisher articles, so the user must open the result and paste the publisher article URL when needed.

## Actual wire ledger

**Log wire TX…** records the actual time with timezone, channel, and exact transmitted text, then marks the report SENT. Copying a prepared message does not record a transmission. The report detail shows prior wire records. Existing wire transcripts need the actual date and a matched report ID before backfill; the app must not infer them from the clipboard.

## Independent recall

Run `shade benchmark benchmark.csv` against a CSV with `url,published_at,county,state`. Dates must include a UTC offset or Z. An optional `event_category` column enables event correctness scoring; without it, the report lists SHADE's category and explicitly marks event correctness unknown. The report separates observation ingestion, location correctness, inbox position or filter reason, and includes a `why_missed` diagnosis for every miss. It summarizes by state and concept family. Google News wrapper matching to a direct publisher URL is reported as **possible**, never silently counted as discovered. A place name without a county crosswalk cannot prove county correctness.

Use independent URLs gathered outside SHADE for recall. SHADE source links for already transmitted items are a workflow check, not an independent recall benchmark.

## Release record

Database schema version 6 was installed after a verified backup at `data/release-backups/20260926T063932Z`. The migration preserved 8,815 claims and 9,716 observations and passed SQLite integrity check. A separate verified backup preceded current-inbox duplicate consolidation. All 111 tests pass. The station configuration, source list, and scheduled collector task were not changed. Older code cannot open the migrated database.
