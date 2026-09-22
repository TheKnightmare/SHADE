# SHADE 0.4.0 local-signal and top-news beta

- Added a separate Top news/context lane so national and world headlines prompt
  a local-impact check without crowding the operational inbox.
- Added verified NPR National, PBS NewsHour, and BBC World source packs with a
  36-hour context window and operational/civic-impact filtering.
- Added an official TDOT SmartWay collector for live East Tennessee incidents,
  weather damage, diversion routes, and full closures. Routine paving and lane
  maintenance are filtered before ingestion; disappeared snapshot events expire.
- Added official KPD, Knox County Sheriff, and Oak Ridge alert feeds.
- Added Knox TN Today, Knoxville Focus, East Tennessee Enlightener, and KnoxViews
  as provenance-labelled local media/community discovery sources.
- Tightened community-headline gating to prevent incidental words in article
  bodies, navigation, and promotions from manufacturing operational chatter.
- Added the full source strategy, church/community listening-post guidance,
  national-to-local impact checklist, 7–8 p.m. workflow, and overnight posture.
- Repaired the WVLT endpoint, disabled obsolete WCYB/WLOS RSS endpoints after
  verified 404 responses, and slowed Reddit polling in response to rate limits.
- Preserved the human-only transmission boundary, attribution model, immutable
  evidence, operating modes, and ACTUAL confirmation gate.

Upgrade from 0.3.1 requires no database migration. Preserve `config.toml`,
`config.local.toml`, and `data/shade.db`; never overwrite private station files
with the public examples.

# 0.3.1 desktop beta

Adds a native Windows operator console, double-click launcher, source health, filters,
review actions, message preview/copy/save, and backed-up housekeeping previews.
Collection runs in the background on demand. Starts in STANDARD; existing review
and ACTUAL confirmation gates remain enforced. Includes desktop regression tests.

# SHADE 0.3.0 beta release notes

- Added durable `inbox` (`queue` alias) and local life-safety `now` lanes.
- Added age, area, category, confidence, source count, relevance filters and expiration.
- Added auditable component scoring and configurable geography/priority thresholds.
- Suppressed routine weather, expired alerts and remote routine M6.x earthquakes.
- Added CISA KEV/advisories, Cloudflare, EIA, OpenFEMA and FAA source packs.
- Preserved raw originals and added immutable revisions, source cooldown state,
  snapshot freshness and workflow audit records.
- Tightened correlation to source identity or conservative located/time-bounded
  exact titles; generic weather/disaster titles cannot manufacture matches.
- Added safe automatic backup for legacy schema migration and explicit bulk
  housekeeping with default preview. Existing IDs and original rows survive.
- Added `doctor`, clearer errors, next-command guidance and reviewed-only formatting.
- Retained STANDARD / EXERCISE / ACTUAL controls and the ACTUAL formatting gate.
- Added explicit `--mode standard` for safe unattended collection.

Upgrade preserves config.toml. Use docs/UPGRADE.md; do not initialize over an
existing station. Source packs and scoring overrides may be added in the private
config.local.toml companion. No radio-control, automatic transmission, GUI,
account creation or external publication is included.

Known limits: CDC HAN is empty/stale and disabled; EIA is context rather than a
real-time utility/pipeline outage feed; FEMA declarations can lag incidents;
FAA status is not a road/rail monitor; Cloudflare covers its own network only.
Scoring and conservative geographic matching require operator review. Existing
legacy groupings are retained, not destructively re-correlated.

