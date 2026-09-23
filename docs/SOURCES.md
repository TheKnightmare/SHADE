# Official source inventory and limitations

Endpoints were fetched and parsed locally on 2026-09-21 (Eastern time).
A responding endpoint is not a guarantee of complete coverage or future uptime.
No credentials, login scraping or access-control bypass is used.

The expanded operating inventory, listening-post policy, top-news lane, and
7–8 p.m. workflow are in [SOURCE_STRATEGY.md](SOURCE_STRATEGY.md). New local
official, local-community, and top-news endpoints were verified on 2026-09-22.

| Pack/source | Native endpoint | Origin/frequency | Operational limitations |
|---|---|---|---|
| Existing NWS | https://api.weather.gov/alerts/active?area=TN (also NC/KY/GA) | nws / 15 min | Broad state collection, local affected-area filtering; expiry enforced. Identify client contact. |
| Existing USGS | https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_week.geojson | usgs / 15 min | Significant global feed supplemented by the regional USGS pack. Tsunami flag is not confirmation. |
| Existing SWPC | https://services.swpc.noaa.gov/products/alerts.json | noaa-swpc / 15 min | Default consequential threshold G/S/R3+ or K7+. Minor watches retained but hidden. |
| Existing GDACS | https://www.gdacs.org/xml/rss.xml | gdacs / 15 min | Aggregator; do not treat relayed reports as independent. Only exceptional notices qualify by default. |
| cyber / KEV | https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json | cisa / hourly | Catalog addition date is not exploit onset. Patch relevance requires checking local assets. |
| cyber / advisories | https://www.cisa.gov/cybersecurity-advisories/all.xml | cisa / hourly | Same family as KEV; generic vulnerability templates do not establish active exploitation. |
| communications | https://www.cloudflarestatus.com/api/v2/summary.json | cloudflare / 15 min | Operator-origin self-report, not all Internet or cellular outages. Domestic component evidence required. Resolved/disappeared snapshots hidden. |
| energy | https://www.eia.gov/rss/todayinenergy.xml | eia / hourly | Official energy context, not a real-time grid/pipeline/refinery disruption monitor. Routine prices/production stories hidden. |
| regional | https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries | fema / hourly | Pack filters TN/NC/KY/GA, orders declarationDate descending, caps 100 records. County rows and declaration dates; lagging administrative evidence, not warning dispatch. |
| transportation | https://nasstatus.faa.gov/api/airport-status-information | faa / 15 min | Full closures and 2h+ delays; transient/general-aviation restrictions suppressed. Snapshot absence/30-minute freshness ends active visibility. First observation is used when origin time is unavailable; not a flight-planning service. |
| health (disabled) | https://tools.cdc.gov/api/v2/resources/media/413690.rss | cdc / hourly | HTTP 200 but zero items and March 2025 build date. Do not claim working public-health coverage. |
| top-news / CNBC | https://www.cnbc.com/id/100003114/device/rss/rss.html | cnbc / 15 min | National market and energy context; context prompt, not local confirmation. |
| top-news / Al Jazeera | https://www.aljazeera.com/xml/rss/all.xml | al-jazeera / 30 min | International conflict and shipping context; corroborate operational effects locally. |

Source documentation and attribution:

- [NWS API](https://www.weather.gov/documentation/services-web-api)
- [USGS GeoJSON](https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php)
- [CISA's official KEV data repository](https://github.com/cisagov/kev-data): describes JSON/CSV, update cadence and CC0 terms. If using its mirror, retain family `cisa`.
- [Cloudflare status API](https://www.cloudflarestatus.com/api): explicitly supports automated JSON polling and asks for an identifiable User-Agent; do not scrape its HTML pages.
- [EIA native RSS subscriptions](https://www.eia.gov/tools/rssfeeds/): intended for periodic reader polling.
- [OpenFEMA dataset](https://www.fema.gov/openfema-data-page/disaster-declarations-summaries-v2): official JSON API; the documentation page returned 403 to the research browser but the data API responded with its documented schema.
- [FAA developer FAQ](https://www.fly.faa.gov/fly/FAQ/faq): links the public machine-readable airport status endpoint.
- [CDC HAN](https://www.cdc.gov/han/php/about/): use official public notices manually until a maintained feed is verified.
- [TEMA current status](https://www.tn.gov/tema/current-status.html) and [NC emergency management](https://www.ncdps.gov/our-organization/emergency-management): no dependable native incident feed verified in this work. No scraper was added.
- [DOE CESER](https://www.energy.gov/ceser/office-cybersecurity-energy-security-and-emergency-response): situation reports cover consequential energy disruptions, but no dependable native incident feed verified. No invented API or utility outage-map scraper was added.

Coverage gaps remain for real-time local utility, fuel/pipeline/refinery, road,
rail, public-health and county emergency dispatch incidents. Additional approved
RSS/Atom feeds can be added privately. Do not label community reporting official.
The disabled community template uses `source_type="community"`, and one family
remains UNVERIFIED. Mirrors/reposts must retain the origin's family.

## Local signal layer

The private local configuration adds RSS/Atom feeds from WATE, WBIR, WVLT, WJHL,
Blue Ridge Public Radio, Mountain Xpress, Tennessee Lookout, and
North Carolina Health News, plus operator-approved community sources. These
are intentionally treated as media or community discovery sources. A local
headline can surface a consequential event, but it does not become confirmed
unless an official source or an independent originating family corroborates it.
The former WCYB and WLOS feed URLs returned 404 on 2026-09-22 and are retained
disabled as manual-watch sources rather than replaced with invented or
unofficial feeds. Reddit's public RSS endpoints repeatedly returned HTTP 429,
so the Reddit feeds were removed from the active configuration rather than
retrying around the limit. Use the local media, official feeds, and approved
Telegram previews for discovery instead.

X is best used as a watchlist/discovery layer for named local agencies, stations,
reporters, utilities, road authorities, and eyewitnesses. It should not be
scraped as an undifferentiated firehose or counted as independent evidence when
posts repeat the same claim. The recommended flow is: detect a burst, extract
the specific claim and location, then confirm against an originating report,
agency notice, dispatch, utility status, or a second independent local outlet.

Request policy: timeout 20 seconds and 5 MB limit by default, HTTPS redirects
only, no rapid retries, persistent 15-minute/hourly cooldowns. Failures back off
at least 30 minutes and honor longer Retry-After headers. Do not bypass a source
block; disable it and review its terms. Partial failures are reported by source.

This source list is expected to be tuned. SHADE separates discovery from
eligibility: adding a feed can improve recall without making every headline
transmit-ready. For slow-building conditions such as fuel-price shocks or a
sustained enforcement/protest pattern, the relevance policy keeps strong
national context visible for up to seven days while still requiring current,
attributable evidence before transmission.


The regional pack also uses the [official USGS FDSN query API](https://earthquake.usgs.gov/fdsnws/event/1/)
with GeoJSON, latitude 30..39, longitude -90..-75, magnitude >=3.5, newest-first,
and limit 100. HTTP 200/schema verified locally. Its default catalog window is
30 days; SHADE applies the station's seven-day collection cutoff. It polls hourly
and shares originating family `usgs` with the significant feed. Identical origin
event IDs correlate even when they arrive through different USGS feed URLs.

## Telegram and civil-unrest sources

`telegram_preview` reads only the public `t.me/s/<channel>` HTML preview. SHADE
stores text, message metadata, and outbound links; it never downloads media.
Telegram channels share a global upstream rate-limit budget, so configure each
channel at a conservative 30–60 minute interval and disable a channel after
repeated 403/404/timeout failures rather than retrying indefinitely.

`acled_api` is an official ACLED US Crisis Monitor dataset. Set its API key via
the configured environment variable (`ACLED_API_KEY` in the example) and never
place credentials in a TOML file. ACLED observations are normalized as
`civil-unrest` events with event date, location, actors, notes, and fatalities.

Community corroboration does not become transmit-eligible merely because more
community families repeat it. Fast-moving unrest channels frequently mirror one
another, so repeated community claims can be one rumor propagating. A claim
requires at least one official or media family before it may advance to
`TX_CANDIDATE`; ACLED counts as official.

The operator may designate a named superior relay such as S2 Underground Wire
with `trusted_for_relay = true`. SHADE preserves that source's family and text
attribution and labels the claim `TRUSTED-RELAY`; it does not relabel the source
as official or silently merge it with another family.
The full S2 watcher map is documented in [S2_WATCHER.md](S2_WATCHER.md).

## Water and radiological monitoring

`usgs_waterservices` reads configured USGS Instantaneous Values gauge sites for
discharge (`00060`) and gage height (`00065`) by default. Site numbers are
operator configuration, not guessed by SHADE. A configured threshold raises
the normal significance through the existing severity/scoring path.

`epa_radnet` uses EPA's documented near-real-time CSV downloads. RadNet
stations provide official background and exposure monitoring; EPA notes that
elevated readings are reviewed by trained scientists. `safecast` uses the
public, keyless volunteer-sensor API and remains community evidence. A single
Safecast reading crossing a threshold is not sufficient: sensor drift and
hardware faults are real failure modes. Treat it seriously only when a RadNet
station agrees or multiple independent Safecast sensors agree. This is a
hardware-reliability safeguard, distinct from the general community rule.

## Mastodon / Fediverse

`mastodon_hashtag` polls each configured instance's public hashtag timeline,
follows the instance's `Link` pagination, stores text/metadata/links only, and
never captures media. Source families include both instance and hashtag (for
example `mastodon-mastodon.social-Iran`). Coverage from one instance is
partial by design: federation does not provide a universal firehose, and this
is not a bug to work around. Rate limits are budgeted per instance, not
globally.

## IRC

SHADE does not currently poll IRC. IRC is a persistent, server/channel
transport rather than an HTTPS feed, so a safe read-only collector would need
the exact network, TLS server/port, channel names, nick/registration policy,
and an explicit retention/rate-limit decision. No IRC credentials or chat
transcripts are collected implicitly.
