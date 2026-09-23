# SHADE source strategy: local chatter, top news, and the 7 p.m. watch

Verified 2026-09-22. “Enabled” means SHADE can poll a public endpoint without a
login. “Watch” means a human should subscribe, bookmark, or check the source;
SHADE must not scrape a login wall, private group, or page that blocks polling.

## What the source stack is for

SHADE needs four different kinds of evidence. They should never be collapsed
into one undifferentiated feed.

1. **Local official signal** establishes what an agency says is happening.
2. **Local media and community chatter** discovers events before they reach a
   large newsroom, but remains reported or unverified until corroborated.
3. **Top-news context** identifies national or global stories for a separate
   local-impact check. It is not automatically radio traffic.
4. **Impact sources** answer the local question: what changes here—fuel cost,
   roads, power, schools, health, supply, communications, or public safety?

## Enabled automatic sources

### Local official

| Source | Endpoint | SHADE treatment | Why it matters |
|---|---|---|---|
| TDOT SmartWay East Tennessee | `spatial.tdot.tn.gov/.../Smartway_Events/FeatureServer` | Official; 5-minute snapshot; incidents, weather, and full closures only | Finds crashes, diversion routes, storm damage, and road closures without flooding the queue with routine paving |
| Knoxville Police Department | `https://www.knoxvilletnpolice.gov/feed/` | Official; 15 minutes | Direct KPD advisories, missing-person notices, and major incident updates |
| Knox County Sheriff | `https://knoxsheriff.org/feed/` | Official; 15 minutes | Direct county public-safety statements and alerts |
| City of Oak Ridge alerts | `https://oakridgetn.gov/RSSFeed.aspx?ModID=63&CID=Alerts-4` | Official; 15 minutes | Oak Ridge/Anderson County alerts, closures, and city service disruption |
| NWS TN/NC/KY/GA | `https://api.weather.gov/alerts/active?area=TN` and companion states | Official; existing NOW lane | Life-safety weather and civil alerts; affected-area and expiration rules apply |

TDOT records disappear when no longer active. SHADE treats the feed as a
snapshot and ends visibility after the record disappears or the snapshot ages
out. A lane closure does not prove the cause reported by a caller or social post.

### Local media and chatter

| Source | Endpoint | Type | Notes |
|---|---|---|---|
| WBIR | `https://www.wbir.com/feeds/syndication/rss/news` | Media | Existing broad East Tennessee feed |
| WATE | `https://www.wate.com/feed/` | Media | Existing broad East Tennessee feed |
| WVLT | `https://www.wvlt.tv/feed/` | Media | Existing broad East Tennessee feed |
| WJHL / WCYB | Station feeds in private config | Media | Tri-Cities and upper East Tennessee |
| WLOS / BPR / Mountain Xpress | Feeds in private config | Media | Western North Carolina |
| Knox TN Today | `https://www.knoxtntoday.com/feed/` | Media | Neighborhood, county, schools, utilities, and civic reporting |
| Knoxville Focus | `https://knoxfocus.com/feed/` | Media | Community newspaper serving Knox County |
| East Tennessee Enlightener | `https://etenlightener.org/feed/` | Media | Center-city and historically under-covered community reporting |
| KnoxViews | `https://knoxviews.com/rss.xml` | Community | Citizen discussion and local-government chatter; unverified by default |
| Reddit Knoxville/Asheville/Tri-Cities | Removed from active configuration after repeated HTTP 429 responses | Community | Human watchlist only; do not bypass Reddit rate limits |

The local-community pack is deliberately keyword-gated. Routine opinion,
events, sports, and lifestyle posts are retained as evidence but do not enter
the operator inbox unless they describe a concrete operational impact.

### Top news

The desktop now has a separate **Top news** lane. It keeps national context out
of the operational inbox. Items age out of this lane after 36 hours.

| Source | Endpoint | Role |
|---|---|---|
| NPR National News | `https://feeds.npr.org/1001/rss.xml` | U.S. headline scan |
| PBS NewsHour Headlines | `https://www.pbs.org/newshour/feeds/rss/headlines` | U.S. and world headline scan |
| BBC News World | `https://feeds.bbci.co.uk/news/world/rss.xml` | Independent international headline scan |

AP and Reuters remain excellent manual confirmation sources, but no dependable
public official RSS endpoint was verified. SHADE should not rely on an
unofficial feed that republishes their work. Google News can be a manual
discovery view, but its links and source mix make it a poor originating family.

### Existing official impact sources

| Source | Use |
|---|---|
| EIA Today in Energy | Energy, refinery, production, storage, and price context; not a real-time disruption feed |
| FAA NAS status | Major airport closure or 2-hour-plus delay context for TYS and regional hubs |
| OpenFEMA regional declarations | Administrative confirmation and affected counties; lagging, not a warning service |
| CISA KEV and advisories | Exploited cyber risk and critical-infrastructure context |
| Cloudflare status | One provider’s communications status; not proof of a general Internet outage |
| USGS / NOAA SWPC / GDACS | Earthquake, space-weather, and exceptional global-disaster context under existing thresholds |

## High-value watchlist: check or subscribe, do not scrape

### Immediate local impact

| Source | What to watch | Use in a bulletin |
|---|---|---|
| KUB Outage Center and outage notifications | Electric, water, and gas interruptions; restoration estimates | Customer counts, area, restoration estimate, safety language |
| LCUB, Appalachian Electric, Fort Loudoun Electric, Sevier County Electric, Oak Ridge Electric | Outage maps and utility social notices | Cross-check community power reports and affected territory |
| TVA Media Relations / TVA News | Grid emergency, generation or transmission issue, conservation request | Regional power-system context; local distributor remains the customer-impact authority |
| TEMA Active Disasters and Emergencies | State activation, incident pages, recovery information | State posture and official resources |
| Knox County Alerts, Blount CivicReady, Sevier CodeRED | Evacuation, wildfire, flooding, missing person, shelter or civil alert | Direct alert text and affected area; preserve timestamps |
| TDH Tennessee Health Alert Network archive | Outbreaks, clinician advisories, border-county health events | Health context; verify public guidance and affected geography |
| Knox County Schools, surrounding districts, UT Alert | Closures, lockdowns, schedule changes | Direct operational impact on families and traffic |
| KAT service alerts and airport/airline notices | Transit suspension, route detour, TYS disruption | Specific routes, duration, and alternatives |

### Hyperlocal listening posts

These sources are valuable because they reveal needs and disruptions that may
never become a WBIR/WATE/Knox News story. They are not confirmation by
themselves.

- City of Knoxville **Neighborly Notice** and neighborhood-organization email
  lists: street closures, service changes, local meetings, resource drives, and
  recurring neighborhood concerns.
- East Tennessee PBS **SNAP Gap**, United Way of Greater Knoxville resource
  pages, Second Harvest East Tennessee, Compassion Coalition, KARM, Volunteer
  Ministry Center, and Salvation Army Knoxville: food, shelter, warming/cooling
  capacity, donation shortages, and service interruptions.
- Church and faith-community newsletters or public pages: prioritize churches
  that operate food pantries, warming centers, recovery teams, or large parking
  and shelter facilities. Track the organization’s own announcement, not a
  screenshot reposted into another group.
- Volunteer fire departments, rescue squads, county EMAs, school districts,
  hospitals, and utilities in Knox, Anderson, Blount, Loudon, Roane, Sevier,
  Jefferson, Hamblen, Cocke, Campbell, Union, Grainger, and the WNC counties in
  the configured coverage area.
- Public Facebook pages, Bluesky accounts, YouTube community posts, and X lists
  for named agencies and reporters. Use a curated list. Do not ingest private
  groups, Nextdoor, personal accounts, or an indiscriminate keyword firehose.

For each listening post, record: owner, URL, county, topic, normal posting
cadence, last verified date, whether alerts/email are available, and the nearest
official confirmation source.

## National story to local-impact checklist

Every Top news item must answer at least one local question before it can move
to review:

- **Fuel:** EIA wholesale/retail trend, Tennessee average, diesel versus
  gasoline, terminal/pipeline/refinery cause, timing, and whether any local
  supply interruption is actually reported.
- **Weather/climate:** NWS Morristown/Greenville-Spartanburg products, local
  timing, terrain/flood/fire implications, school and road effects.
- **War/geopolitics:** fuel and freight exposure, Guard deployment if officially
  announced, cyber posture, scams, protests or events—without speculating.
- **Economy:** Tennessee/local employer exposure, utility bills, food/farm
  inputs, travel, benefits, or government-service changes.
- **Cyber/communications:** named local provider or public agency impact;
  national vendor trouble alone is not local impact.
- **Health:** TDH/local health department guidance, affected counties,
  healthcare capacity, and actionable public advice.
- **Federal/state action:** which service, deadline, payment, road, school,
  hospital, or household cost changes here and when.

Example: “oil rises” is context. “EIA reports Gulf Coast wholesale gasoline up
X; AAA Tennessee retail average moved Y; no local terminal outage is reported”
is a sourced local-impact note. Never infer a pump-price change from crude oil
alone.

## 7–8 p.m. operating rhythm

| Time | Action | Output |
|---|---|---|
| 7:00 | Collect; check source health and failed/cooling feeds | Know what is missing before trusting silence |
| 7:05 | Read NOW, Inbox, then Top news | Three separate piles: urgent, local operational, context |
| 7:15 | Pick no more than five candidates | Candidate list with location, timestamp, and originating family |
| 7:20 | Open the original source and a confirming source | Verified fact set; label single-family chatter UNVERIFIED |
| 7:35 | Run the local-impact checklist | What changes in ETN/WNC, for whom, and when |
| 7:45 | Draft only the items that pass | Short bulletin plus source/time notes |
| 7:55 | Final stale-data, attribution, and “still active?” check | Send, hold, or explicitly declare no traffic |
| 8:00 | Bulletin decision | Transmit manually or continue monitoring |

## Overnight posture

| Posture | Trigger | Conduct |
|---|---|---|
| GREEN — routine watch | No active local operational item | Recheck on the normal interval; no need to manufacture traffic |
| YELLOW — developing | One credible local report or a national event with plausible local impact | Shorter checks; seek the originating agency and one independent family; draft but hold if facts are moving |
| RED — active incident | Official local warning, closure, evacuation, major outage, or rapidly changing life-safety event | Monitor the named official sources continuously enough for the event; timestamp every update; retract superseded details |
| QUIET / GO TO GROUND | Information environment is highly charged, rumor-heavy, operationally sensitive, or could endanger responders/victims | Do not amplify live tactics, unverified identities, graphic detail, crowd locations, or rumor. Pass only necessary, attributable public-safety facts—or monitor without transmitting |

Silence is a valid product. “No bulletin; monitoring continues” is better than
filling airtime with weakly sourced chatter.

## Source-handling rules

- A source family is the origin, not the URL. Mirrors and reposts count once.
- Official means “the named agency said it,” not “the claim is unquestionably
  true.” Corrected official posts must supersede earlier versions.
- Community posts can trigger a check; they cannot confirm themselves through
  repetition.
- Preserve publication time, observation time, link, and exact affected area.
- Never publish a person’s home address, medical status, identity, or live
  responder position merely because it appeared in a public group.
- Respect rate limits, robots rules, logins, and terms. If a page blocks polling,
  move it to the human watchlist.
- Review the automatic list monthly. Remove dead feeds and record the last
  successful verification date.

Telegram public previews are discovery-only community inputs: they are
rate-limited globally by upstream domain, never include downloaded media, and
remain unverified until an official or media family corroborates the claim.
ACLED's registered US Crisis Monitor API is an official civil-unrest dataset;
its event date, location, actors, notes, and fatality fields can independently
clear a community unrest claim. Multiple community families alone do not clear
corroboration because unrest channels commonly mirror the same originating
claim.
