# Regional discovery audit — 2026-09-24

## 1. Architecture and loss points

SHADE loads configured sources and enabled packs, polls due sources concurrently, parses results into observations, drops reports older than the collection window, merges observations into claims, computes confidence and relevance when a view is read, then filters and sorts the inbox. The configured database retains evidence and source poll failures. Sending remains a human action.

Before this change, regional discovery depended mainly on fixed feeds: four working ETN television feeds, selected local/community feeds, official weather/road/earthquake APIs, and national context feeds. The `google_news_search` collector existed but no regional search source was enabled. Its old behavior queried the first terms every time, credited Google News as the originating family, and recorded no query provenance. Thus most sample stories disappeared **before ingestion**, regardless of score. Another distinct miss was the NC Chemours settlement: it was in the database from NC Health News but scored 37, below the 45-point display floor. A small Brevard quake was excluded by the USGS M3.5+ source and magnitude gate.

## 2. Source inventory

Before: 62 configured sources, 59 enabled. Relevant automatic inputs included four working ETN TV feeds (WATE, WBIR, WVLT, WJHL), BPR, Mountain Xpress, NC Health News, Tennessee Lookout, TDOT East Tennessee, Knoxville Police, Knox Sheriff, Oak Ridge alerts, NWS, and USGS M3.5+. WCYB and WLOS RSS URLs were already disabled after 404 responses. No regional search schedule was enabled. Kentucky, Georgia, and much of North Carolina had little direct local reporting.

After: 73 configured sources, 70 enabled. Added six rotating regional news searches, a USGS ETN/WNC M1.5+ feed, and four live-tested news feeds: 3B Media News, Georgia Recorder, Kentucky Lantern, and NC Newsline. The last three share one conservative originating family because State Newsroom sites republish each other's material. A candidate 828 News NOW RSS URL was tested but omitted because it last published in April. The disabled WLOS feed still returns 404.

## 3. Search generation and geography

Each of six search regions (ETN, WNC, rest of TN, KY, GA, rest of NC) has four place groups and six concept families. Three queries run per source every three hours; the deterministic eight-slot rotation covers all 24 place/topic pairs in a day **if the collector runs through all slots**. Concepts cover public safety, transport/fire, outages, surveillance, security/DOE/drones, and geophysical/cyber events. Each query requests the past seven days. Query text, URL, count, errors, and the discovering source are logged locally. Publisher domains are mapped back to existing feed families when possible, so a search hit and RSS hit from WVLT do not count as two outlets.

The ETN/WNC locality vocabulary now includes additional cities, facilities, landmarks, and bare county names; common `Co.` abbreviations and the first 500 characters of an article summary are considered. Georgia spelled out is recognized as regional. Ambiguous county names still need a complete state-aware gazetteer. Search results without evidence of TN/KY/GA/NC location are retained but withheld from the regional inbox.

## 4. Example-story path and baseline

A read-only search of the production database for 11 supplied story descriptions found a plausible record for **1/11**: NC Chemours/PFAS. The other ten had no matching title/body pair. This is a database presence check, not a formal discovery-recall estimate: the sample spans different publication days and several supplied headlines would already exceed the seven-day limit on this audit date. The PFAS record was an **ingested but display-filtered** miss; the others had no ingested match, so a low score cannot explain them. Original source coverage for their dates cannot be reconstructed from the old logs.

The new `shade why-missed "headline or URL"` command reports an ingested claim's match strength, discovery query, area, score components, confidence, and display reason. If no observation exists, it reports `not_observed` with recent search polls and says which earlier stages cannot be distinguished. The `.discovery.jsonl` file beside the database records new query attempts and candidate decisions, including collection-age rejection. It rotates at 10 MB.

## 5. Implemented changes, ranked by expected recall gain

1. **Rotating regional searches** (highest): reaches outlets and counties absent from RSS inventory without requiring a known story headline.
2. **Four tested regional newsroom feeds** (high): adds direct local/state reporting and a radio newsroom; polling is independent of search indexing.
3. **Small locally felt USGS quakes** (targeted high): an M2.3 Brevard quake with 26 felt reports now passes the local quake rule.
4. **Publisher attribution, same-outlet deduplication, and query trace** (high for confidence and diagnosis): repeated search hits no longer look like independent outlets.
5. **Locality, interest, and novelty signals** (medium for display): geographic shorthand is resolved, regional items without a concrete headline signal are withheld, and the already-ingested PFAS record now scores 47 and appears in the regional inbox.

The collection stage still ingests noisy search results by design; filtering happens after ingestion. Unverified community material continues through the existing chatter lane and is never silently promoted to confirmed reporting.

## 6. Live comparison, 2026-09-24

An isolated SHADE run at 21:51 UTC polled six search sources successfully and ingested 202 candidate observations into an evaluation database. This was one three-hour search slot, **not** the promised full-day cycle. Manual search was done separately and the following stories were checked:

| Story | SHADE one-slot result | Manual result | Miss stage / explanation |
|---|---|---|---|
| Anderson County Flock contract dispute | Found, ETN/WNC, score 67 after `Co` normalization | [WVLT](https://www.wvlt.tv/2026/09/24/flock-camera-contract-dispute-anderson-co-heads-budget-committee-with-sheriff-mayor-opposite-sides/) | Found by search; old policy had geolocation trouble with `Anderson Co`. |
| Georgia Power/Google nuclear upgrades | Found | [Georgia Recorder](https://georgiarecorder.com/2026/09/24/georgia-power-google-announce-deal-to-pay-for-nuclear-upgrades/) | Found by search; new direct feed provides another route. |
| Kentucky Flock camera vandals | Found via Kentucky Today | [Herald-Leader crime page](https://www.kentucky.com/news/local/crime/) | Search discovered it; confirm originating story before reporting. |
| McDowell County Helene roads recovery | Found via WLOS search result | Not independently found in the manual sample | SHADE-only discovery despite the disabled WLOS feed; original article still needs operator review. |
| Georgia prison drone contraband | Missed in the current slot; SHADE's generated later-slot drone/prison query returned the [WSB-TV](https://www.wsbtv.com/news/local/baby-bottle-drugs-found-drone-delivery-headed-georgia-prison/M6O5RTPUCFCO7PWZRVB53Z2Z5A/) item in a separate live query check | [WSB-TV](https://www.wsbtv.com/news/local/baby-bottle-drugs-found-drone-delivery-headed-georgia-prison/M6O5RTPUCFCO7PWZRVB53Z2Z5A/) | Search schedule timing, not scoring. A full-day run is needed to prove delivery to the inbox. |
| Kingsport first responders hit on I-26 | Not in the one-slot search database | [WVLT](https://www.wvlt.tv/2026/09/22/4-first-responders-injured-after-being-hit-by-car-while-working-crash-east-tennessee-interstate/) | Discovery miss in snapshot; direct WVLT feed may have carried it earlier, but historical feed delivery is unproven. |
| Kentucky hidden school camera | Not in the one-slot search database | [Herald-Leader state page](https://www.kentucky.com/news/state/kentucky/) | Discovery miss in snapshot; later rotation and state feed need observation. |
| Brevard M2.3 quake | Found in newly added USGS feed with 26 felt reports | [Charlotte Observer state page](https://www.charlotteobserver.com/news/state/north-carolina/) | Old source magnitude floor and scoring gate; fixed and live-verified. |
| NC Chemours/PFAS settlement | Already ingested from NC Health News; now visible at score 47 | Existing SHADE source record | Was a display filter miss, fixed by separate interest points. |

## 7. Remaining work and proof needed

The existing Windows `SHADE Collector` task was reactivated after verifying its action points to this checkout, runs in STANDARD mode, and repeats every 15 minutes. The exact scheduled command then polled 46 due sources: 38 succeeded, 8 failed, and 24 were on cooldown; the failures were RadNet 406 (two), Mastodon 422, an oversized podcast feed, missing EIA credentials (two), CISA 403, and FEMA 404. All 11 newly added sources had succeeded in a separate production run (297 observations). These unrelated failures remain visible in source health and need separate repair. Until collection has run through eight three-hour slots, the 24-hour recall target is **not proven**. The current comparison has only an isolated one-slot live result.

A stronger benchmark needs a dated, URL-backed set of independently selected stories for each day, plus recorded search/ingestion snapshots. Measure (a) discovery recall before scoring, (b) area and event classification accuracy, and (c) ranking positions separately. Full county/city gazetteer coverage, article-level event-time extraction, cross-publisher event clustering, direct DOT/agency feeds for KY/GA/NC, and a complete verified/unverified evidence ledger remain future work. Do not infer 24-hour recall from a single successful query slot.

## 8. Closeout snapshot — 2026-09-26

No new dated, URL-backed benchmark CSV was available to run this pass -
`docs/benchmark_template.csv` is an empty header-only template, and
fabricating benchmark stories would not be an independent measurement. The
`shade benchmark` command (`benchmark.py`/`audit_csv`) remains available and
ready to score a real dated sample whenever the operator supplies one; this
pass could not manufacture that input.

**24-hour recall target: still not proven**, for the same reason section 7
gives - proving it requires observing collection through a full day's worth
of the six-region search rotation, which this session cannot compress into a
single working session. The "SHADE Collector" scheduled task has in fact
been running continuously and unattended since 2026-09-21 (see
`docs/SCHEDULING.md`), so far more than 8 three-hour slots have elapsed in
wall-clock terms; whatever recall conclusion that represents has not been
separately re-measured against a fresh dated story sample in this pass, and
one single-slot comparison (section 6) is still the only slot-level ground
truth on record. Do not treat continuous uptime as recall proof by itself.

**Live source-health snapshot** (read-only `source_polls` check, 2026-09-26):

| source | error | classification |
|---|---|---|
| `s2-podcast` | was `response exceeds 5000000 bytes` | **fixed this pass** - verified the real feed is 7,796,019 bytes (a large but legitimate podcast RSS document, not malformed); raised `max_response_bytes` 5MB→10MB in `config.toml` (a global, bounded config value, not a code change) and confirmed via a direct fetch+parse that it now succeeds (990 items parsed) |
| `eia-crude-spot`, `eia-retail-prices` | missing credential environment variable: `EIA_API_KEY` | **missing credential**, not a defect - needs an operator-supplied API key; nothing to fix in code |
| `cisa-advisories` | HTTP 403 | **external** - the endpoint is refusing the request; needs investigation of whether CISA changed its access requirements, not a guessed fix |
| `fema-regional` | HTTP 404 | **external, likely retired/moved endpoint** - needs the current FEMA URL verified before any code change |
| `epa-radnet-knoxville`, `epa-radnet-nashville` | HTTP 406 (Not Acceptable) | **external, uncertain cause** - could be a stale Accept header or a changed API; not fixed this pass to avoid guessing at EPA's current expectations |
| `mastodon-infosec-cve`, `-cybersecurity`, `-infosec` | HTTP 422 | **external, uncertain cause** - three separate Mastodon query endpoints all rejecting the request; needs direct investigation of the current Mastodon API contract, not attempted this pass |
| `reddit-tricities`, `reddit-knoxville`, `wlos-local`, `wcyb-local` | stale "legacy failure cause unavailable" records (last attempted 2026-09-22/23) | **stale poll records, not necessarily still broken** - `wlos-local`'s HTTP 404 is already documented in section 2 as a known dead feed; the others have not been freshly re-polled with an informative error since the legacy-error-message fix landed and need a fresh attempt to know their current state |

Only the `s2-podcast` fix was made - it was the one failure with a concrete,
verifiable, in-code/in-config root cause. Everything else above is handed to
the next review/optimization agent rather than guessed at.
