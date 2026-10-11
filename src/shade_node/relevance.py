"""Pure, clock-injectable relevance policy. No network or database side effects."""
from datetime import datetime, timezone, timedelta
from .model import normalize_text, token_set
import json
import re

DEFAULT_POLICY = {
    'minimum_score': 45, 'max_age_hours': 168, 'global_quake_magnitude': 7.5,
    'regional_quake_magnitude': 3.5, 'domestic_quake_magnitude': 6.5,
    'local_terms': ['east tennessee', 'eastern tennessee', 'western north carolina', 'knoxville', 'asheville', 'tri-cities', 'oak ridge', 'y-12', 'ornl', 'brevard', 'blue ridge parkway', 'great smoky mountains', 'johnson city', 'kingsport', 'gatlinburg', 'sevierville', 'maryville', 'pigeon forge', 'hendersonville', 'waynesville', 'oliver springs', 'rockwood', 'crossville', 'white pine', 'greeneville', 'morristown', 'lenoir city', 'farragut', 'elizabethton', 'dandridge', 'sweetwater', 'boone', 'sylva'],
    'regional_states': ['TN', 'KY', 'GA', 'NC'],
    'local_counties': ['Knox, TN','Blount, TN','Sevier, TN','Anderson, TN','Loudon, TN','Roane, TN','Monroe, TN','McMinn, TN','Bradley, TN','Hamilton, TN','Campbell, TN','Claiborne, TN','Union, TN','Grainger, TN','Jefferson, TN','Hamblen, TN','Cocke, TN','Greene, TN','Washington, TN','Sullivan, TN','Carter, TN','Johnson, TN','Unicoi, TN','Hawkins, TN','Hancock, TN','Fentress, TN','Scott, TN','Morgan, TN','Cumberland, TN','Bledsoe, TN','Rhea, TN','Meigs, TN','Polk, TN','Sequatchie, TN','Marion, TN','Buncombe, NC','Haywood, NC','Henderson, NC','Madison, NC','Yancey, NC','Mitchell, NC','Avery, NC','Watauga, NC','Ashe, NC','Alleghany, NC','Wilkes, NC','Caldwell, NC','Burke, NC','McDowell, NC','Rutherford, NC','Polk, NC','Transylvania, NC','Jackson, NC','Swain, NC','Macon, NC','Graham, NC','Clay, NC','Cherokee, NC'],
    'category_priorities': {},
    # Configurable, reversible noise controls keyed by source_family OR category
    # (source_family checked first). Empty by default - operators add entries
    # under [relevance.noise_policies.<name>] in config.toml rather than this
    # code hard-coding an exclusion for e.g. ICE-instant-report or temporary-
    # airport-notice sources. 'suppress' hides the item outright (with a
    # visible, auditable reason); 'max_age_hours' applies a tighter staleness
    # cutoff than the global one; 'min_score' raises the bar an item from that
    # source/category must clear before it can reach the operator queue.
    'noise_policies': {},
}
OPERATIONAL = {'cyber','infrastructure','grid','fuel','commodity','communications','transportation','supply-chain','public-health','public-safety','emergency','chatter','top-news','civil-unrest','civil-liberties','politics','regional','radiological','fire-weather','notam'}

# NOTAMs get their own category rather than being forced into public-safety.
# Only a NOTAM that plausibly affects the public, emergency operations,
# wildfire response, a major event, a regional airport's passenger operations,
# transportation, or public access is surfaced for operator review by
# default; routine aviation-maintenance/pilot-operational notices (a runway
# light out, an obstruction lighting outage, a taxiway closure) stay out of
# the general Daily Wire, same as any other routine/no-impact item elsewhere
# in this file - visible in evidence, not hidden, just not queue-eligible.
NOTAM_PUBLIC_IMPACT_PATTERN = re.compile(
    r'\b(?:tfr|temporary flight restriction|wildfire|fire[- ]?fighting|air ?show|stadium|'
    r'sporting event|VIP movement|presidential|secret service|closed to (?:the )?public|'
    r'public access|drone|unmanned aircraft|UAS operations?|military exercise|'
    r'runway closed|airport closed|major event|evacuation|disaster relief)\b', re.I)
LIFE_SAFETY = {'Tornado Warning','Flash Flood Warning','Civil Emergency Message','Evacuation Immediate','Shelter In Place Warning','Extreme Wind Warning','Tsunami Warning'}
FIRE_WEATHER = {'Red Flag Warning', 'Fire Weather Watch'}


# Shared, auditable event vocabulary. Highest matching tier wins; structured
# source severity can raise it further. Patterns use word boundaries to avoid
# incidental matches such as 'fire' inside 'firewall'.
SEVERITY_TIERS = (
    ('fatality', 30, r'\b(?:mass casualties|mass casualty|fatal(?:ity|ities)?|deadly|killed|deaths?|died|bod(?:y|ies).{0,80}(?:recovered|found)|remains recovered)\b'),
    ('self_harm', 25, r'\b(?:suicid(?:e|al)|self[- ]harm|attempted suicide|suicide attempts?|mental health crisis)\b'),
    ('structural', 22, r'\b(?:bridge collapse|building collapse|collapsed? (?:bridge|building|structure)|structural failure|dam failure|levee breach|infrastructure failure)\b'),
    ('injury', 20, r'\b(?:injur(?:y|ies|ed)|wounded|shooting|stabbing|assault|explosion|crash|collision|rescue)\b'),
    ('disruption', 15, r'\b(?:outage|disruption|closure|closed|evacuation|exploited|exploitation|ransomware|shutdown|shortage|outbreak|emergency declaration|fire|hazmat|hazardous materials|boil water|water main|power line|wildfire|bridge|road|interstate|shelter|hospital|school lockdown|warrantless|surveillance|privacy breach|press freedom|polling place closure|election disruption|voting access|protest rights|protest|riot|clash|unrest|demonstration|strike)\b'),
)
SEVERITY_PATTERNS = tuple((name, points, re.compile(pattern, re.I)) for name, points, pattern in SEVERITY_TIERS)
EVENT_SIGNAL = re.compile('|'.join('(?:'+pattern+')' for _, _, pattern in SEVERITY_TIERS), re.I)
ROUTINE_CONTENT = re.compile(r'\b(?:weekly|monthly|daily) (?:newsletter|digest|roundup)\b|\b(?:newsletter|community calendar|upcoming events|event announcement|announces? (?:a |the )?(?:meeting|webinar|open house))\b', re.I)

# Routine sports-update language (score updates, matchup recaps, game-clock
# terms). A sports item can still reach the operator inbox - just not on
# sports language alone; EVENT_SIGNAL (deaths, injuries, evacuation, etc.)
# still applies on top of this exactly as it does for every other category,
# so a stadium collapse or crowd crush during a game is unaffected.
SPORTS_PATTERN = re.compile(
    r'\b(?:touchdown|quarterback|halftime|scoreboard|box score|final score|kickoff|'
    r'tip[- ]off|face[- ]off|first pitch|overtime|extra innings|shootout|walk-off|'
    r'home run|three[- ]pointer|field goal)\b'
    r'|\b(?:leads?|trails?|edges?|tops?|defeats?|beats?|upsets?|routs?)\b.{0,40}\b(?:matchup|game|series|clash|opener|rival(?:ry)?)\b'
    r'|\bmatchup\b.{0,40}\bvs\.?\b'
    r'|\b(?:1st|2nd|3rd|4th|first|second|third|fourth)\s+(?:quarter|inning|period|half)\b'
    r'|\bfinal:?\s*\d{1,3}[-–]\d{1,3}\b',
    re.I)



# Independent of conventional impact: terms denote the kind of regional event
# GhostNet watches. A search result still needs a concrete headline signal.
GHOSTNET_FAMILIES = {
    'infrastructure': r'\b(?:power|grid|utility|water|sewer|communications?|cellular|tower|road|highway|bridge|rail|airport|pipeline)\b.{0,70}\b(?:outage|failure|disruption|closure|shut|crash|damag|leak|restor|repair)',
    'surveillance': r'\b(?:flock|alpr|license plate reader|surveillance|privacy|recording device|hidden camera)\b',
    'hazard': r'\b(?:wildfire|major fire|house fire|arson|explosion|hazmat|contamination|pfas|earthquake|landslide|flood|tornado)\b',
    'security': r'\b(?:cyberattack|ransomware|drone|contraband|prison|nuclear|doe|y-12|ornl)\b',
    'public_safety': r'\b(?:homicide|shoot(?:ing)?|shot|arrest(?:ed)?|charged|vandalism|theft|stolen|steal(?:ing)?|threaten|missing|remains found|rescue|crash|collision|jackknif\w*|injur\w*|911|e-911)\b',
    'health': r'\b(?:outbreak|disease|public health|contaminat|settlement)\b',
}
GHOSTNET_PATTERNS = {name: re.compile(pattern, re.I) for name, pattern in GHOSTNET_FAMILIES.items()}
NOVELTY_PATTERN = re.compile(r'\b(?:unexpected|unusual|mysterious|disappeared|intercepted|posed as|impersonat|hidden|secret|vandaliz|sabotag|identified by dna|without food|extreme heat)\b', re.I)


def ghostnet_interest(text):
    matched = [name for name, pattern in GHOSTNET_PATTERNS.items() if pattern.search(text)]
    return min(100, 25 * len(matched)), matched


def novelty_score(text):
    return 25 if NOVELTY_PATTERN.search(text) else 0


def severity_points(text):
    return max((points for _, points, pattern in SEVERITY_PATTERNS if pattern.search(text)), default=0)


def routine_content(title):
    return bool(ROUTINE_CONTENT.search(title))


def timestamp(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (TypeError,ValueError):
        return None


def iso(value):
    return value.isoformat().replace('+00:00','Z') if value else ''


def age(value, now=None):
    when = timestamp(value); now = now or datetime.now(timezone.utc)
    if when is None: return '?'
    seconds = max(0, int((now-when).total_seconds()))
    if seconds < 60: return '<1m'
    if seconds < 3600: return f'{seconds//60}m'
    if seconds < 86400: return f'{seconds//3600}h'
    return f'{seconds//86400}d'


def remaining(value, now=None):
    when=timestamp(value); now=now or datetime.now(timezone.utc)
    if when is None: return 'unknown'
    if when <= now: return 'ended'
    return age(iso(now-(when-now)),now)+' left'


def metadata(row):
    raw=json.loads(row['raw_json'])
    p=raw.get('properties',{})
    category=row['category']
    if row['source_family']=='gdacs' and 'earthquake' in row['title'].lower(): category='earthquake'
    end_values=[timestamp(p.get(k)) for k in ('ends','expires')]
    expiry=min((v for v in end_values if v),default=timestamp(raw.get('expires')))
    if raw.get('_snapshot_asof'):
        expiry=timestamp(raw['_snapshot_asof'])+timedelta(minutes=30)
    return {'raw':raw, 'properties':p, 'category':category, 'expires':expiry,
            'onset':timestamp(p.get('onset') or p.get('effective')),
            'published':timestamp(row['published_at']) or (timestamp(row['observed_at']) if raw.get('_snapshot') else None),
            'event_at':timestamp(raw.get('_event_at')), 'event':p.get('event',''),
            'urgency':p.get('urgency',''), 'certainty':p.get('certainty','')}


from functools import lru_cache


@lru_cache(maxsize=8)
def _local_terms_lower(terms):
    """Lowercased copy of policy['local_terms']+['local_counties'], cached by
    the (hashable) tuple of terms - the same handful of dozens of strings on
    every call within one queue()/evaluate() pass.

    A single big regex alternation of ~140 terms turned out NOT to be faster
    than the original any()-of-separate-searches loop it replaced: CPython's
    `re` engine still does roughly O(terms) work per position for a large
    non-literal-prefix alternation, so combining calls didn't reduce work,
    just call count (measured: no improvement, occasionally worse). What
    actually wins here is a cheap substring pre-filter first - Python's `in`
    operator on strings is a highly optimized C search - so the far more
    expensive regex (needed for the exact word-boundary semantics the
    original code requires) only ever runs against the small number of terms
    that are already known to appear as a raw substring, instead of against
    all ~140 on every claim regardless of whether any of them are even
    present in the text."""
    return tuple(t.casefold() for t in terms)


@lru_cache(maxsize=8)
def _local_county_names(local_counties):
    """policy['local_counties'] validated against the gazetteer's own county
    index - same result every time for the same policy, previously
    recomputed (including two gazetteer.data() lookups) on every single
    geography() call rather than once per distinct policy."""
    from .gazetteer import data
    county_index = data()[0]
    return tuple(sorted(
        value.split(',')[0] for value in local_counties
        if county_index.get(('county', value.split(',')[0].casefold())) == {value.split(', ')[1]}))




def geography(row, meta, policy):
    from .gazetteer import resolve
    # Sources that already know their distance tier (NODE radio reports placed
    # by Maidenhead grid) state it explicitly; nothing else sets this key.
    tier = meta['raw'].get('_area_tier')
    if tier in {'ETN/WNC': 30, 'REGIONAL': 20, 'NATIONAL': 12, 'GLOBAL': 0}:
        return tier, {'ETN/WNC': 30, 'REGIONAL': 20, 'NATIONAL': 12, 'GLOBAL': 0}[tier]
    found = resolve(row['title']) if 'source_type' in row.keys() and row['source_type'] == 'media' else None
    if found and found['confidence'] in {'explicit', 'unique_place'}:
        location = found['location']
        if location.replace(' County,', ',') in policy['local_counties'] or any(t.casefold() in location.casefold() for t in policy['local_terms']):
            return 'ETN/WNC', 30
        return 'REGIONAL', 20
    # Affected area, never issuing weather office, determines local relevance.
    area=row['location'] or meta['properties'].get('areaDesc','') or meta['raw'].get('_area','')
    area=area.replace(' (County)','')
    text=area if meta['category'] in {'weather','fire-weather'} else area+' '+row['title']+' '+row['body'][:500]
    text=re.sub(r'\bCo\.?\b', 'County', text, flags=re.I)
    text_lower=text.casefold()
    all_terms=tuple(policy['local_terms']+policy['local_counties'])
    term_candidates=[t for t,tl in zip(all_terms,_local_terms_lower(all_terms)) if tl in text_lower]
    if any(re.search(r'(?<!\w)'+re.escape(t)+r'(?!\w)',text,re.I) for t in term_candidates):
        return 'ETN/WNC',30
    local_county_names = _local_county_names(tuple(policy['local_counties']))
    county_candidates=[name for name in local_county_names if name.casefold() in text_lower]
    if any(re.search(r'\b'+re.escape(name)+r' County\b', text, re.I) for name in county_candidates):
        return 'ETN/WNC',30
    states=policy['regional_states']
    # Same substring-prefilter-before-regex shape as local_terms/local_counties
    # above: `t in text`/`name in text_lower` is a necessary condition for the
    # corresponding \b-wrapped regex to match, so skipping the regex whenever
    # the substring itself is absent can never change which rows match.
    state_candidates=[t for t in states if t in text]
    full_name_candidate=any(name in text_lower for name in ('tennessee','kentucky','georgia','north carolina'))
    if any(re.search(r'\b'+re.escape(t)+r'\b',text) for t in state_candidates) or \
       (full_name_candidate and re.search(r'\b(Tennessee|Kentucky|Georgia|North Carolina)\b',text,re.I)):
        return 'REGIONAL',20
    # Structured USGS coordinates establish southeast/domestic context.
    coords=(meta['raw'].get('geometry') or {}).get('coordinates',[])
    if meta['category']=='earthquake' and len(coords)>=2 and isinstance(coords[0],(int,float)):
        lon,lat=coords[:2]
        if -90<=lon<=-75 and 30<=lat<=39: return 'REGIONAL',20
        if -125<=lon<=-66 and 24<=lat<=50: return 'NATIONAL',12
    if meta['category'] in {'weather','fire-weather'}: return 'DISTANT',0
    if meta['raw'].get('_area')=='NATIONAL' or re.search(r'\b(United States|USA|Alaska|Hawaii)\b',text): return 'NATIONAL',12
    if meta['category']=='space-weather': return 'NATIONAL',12
    return 'GLOBAL',0


def confidence(rows):
    families = set()
    media = set()
    seen_syndicated = {}
    for row in rows:
        family = row['source_family']
        if row['source_type'] == 'media' and len(token_set(row['title'])) >= 7:
            title = row['title'].removesuffix(' - ' + row['source_name'])
            headline = normalize_text(title)
            published = timestamp(row['published_at'])
            prior = seen_syndicated.setdefault(headline, [])
            if published and any(abs((published - earlier).total_seconds()) <= 6 * 3600 for earlier in prior):
                continue
            if published:
                prior.append(published)
        families.add(family)
        if row['source_type'] == 'media':
            media.add(family)
    official={r['source_family'] for r in rows if r['source_type']=='official'}
    high_credibility=any(
        ('_high_credibility_source' in r['raw_json'] or '_trusted_for_relay' in r['raw_json']) and (
            json.loads(r['raw_json']).get('_high_credibility_source')
            or json.loads(r['raw_json']).get('_trusted_for_relay'))  # legacy rows: display only
        for r in rows
    )
    if official and len(families)>=2: label,score='CONFIRMED',90
    elif official: label,score='OFFICIAL-REPORT',75
    elif len(media)>=2: label,score='CORROBORATED',60
    elif media: label,score='REPORTED',35
    elif any(r['source_type']=='community' for r in rows):
        label,score=('UNVERIFIED (HIGH-CRED SOURCE)',20) if high_credibility else ('UNVERIFIED',20)
    else: label,score='REPORTED',35
    credibility=' At least one source is operator-designated high-credibility; this is informational and does not permit workflow advancement.' if high_credibility else ''
    explanation=f'{len(families)} effective originating family/families; {len(official)} official. Identical syndicated headlines published within six hours count once. This label is attribution, not proof of truth.{credibility}'
    return label,score,len(families),len(official),explanation


# Time-independent signals memoized across refreshes. geography(),
# severity_points(), ghostnet_interest() and novelty_score() are pure
# functions of the observation's text/metadata and the policy's geography
# lists; recomputing them for ~30k observations on every desktop refresh
# was ~75% of refresh time. The key hashes every input those functions read,
# so any change to the row or the geography policy is a cache miss, never a
# stale hit. Time-dependent scoring (freshness, expiry) is still recomputed.
_STATIC_CACHE = {}
_STATIC_CACHE_LIMIT = 200_000


def _static_signals(row, meta, policy):
    import hashlib
    raw = meta['raw']
    source_type = row['source_type'] if 'source_type' in row.keys() else None
    material = '\x1f'.join((
        str(source_type), row['title'], row['body'], str(row['location']), str(meta['category']),
        str(meta['properties'].get('areaDesc', '')), str(raw.get('_area', '')), str(raw.get('_area_tier', '')),
        json.dumps(raw.get('geometry'), sort_keys=True, default=str)))
    policy_key = (tuple(policy['local_terms']), tuple(policy['local_counties']), tuple(policy['regional_states']))
    key = (hashlib.blake2b(material.encode('utf-8', 'surrogatepass'), digest_size=16).digest(), policy_key)
    hit = _STATIC_CACHE.get(key)
    if hit is not None:
        return hit
    area, geo = geography(row, meta, policy)
    combined = row['title'] + ' ' + row['body']
    interest, interest_families = ghostnet_interest(combined)
    value = (area, geo, severity_points(combined.lower()), interest, tuple(interest_families), novelty_score(combined))
    if len(_STATIC_CACHE) >= _STATIC_CACHE_LIMIT:
        _STATIC_CACHE.clear()
    _STATIC_CACHE[key] = value
    return value


def evaluate(row, policy=None, now=None, confidence_score=75, families=1):
    policy={**DEFAULT_POLICY,**(policy or {})}; now=now or datetime.now(timezone.utc)
    meta=metadata(row); raw=meta['raw']; p=meta['properties']; cat=meta['category']
    area,geo,static_impact,interest,interest_families,novelty=_static_signals(row,meta,policy)
    published=meta['published']; effective=min((value for value in (published, meta['event_at']) if value), default=None)
    hours=(now-effective).total_seconds()/3600 if effective else float('inf')
    impact={'unknown':0,'minor':3,'moderate':10,'severe':20,'extreme':30}.get(row['severity'],0)
    text=(row['title']+' '+row['body']).lower()
    impact=max(impact, static_impact)
    if cat=='cyber' and row['source_family']=='cisa' and 'cveID' not in raw:
        # Advisory templates often say there is NO known public exploitation.
        positive=re.sub(r'no (?:known )?(?:public )?exploits?[^.]*|no (?:known )?exploitation[^.]*','',text)
        consequential=bool(re.search(r'known exploited|actively exploited|active exploitation|ransomware campaign|critical infrastructure (?:attack|disruption)',positive))
        impact={'unknown':0,'minor':3,'moderate':10,'severe':20,'extreme':30}.get(row['severity'],0)
        if consequential: impact=max(impact,severity_points(positive))
    if cat not in OPERATIONAL:
        impact={'unknown':0,'minor':3,'moderate':10,'severe':20,'extreme':30}.get(row['severity'],0)
    parts={'geography':geo,'category':15 if cat in OPERATIONAL else 5,
           'impact':impact,'freshness':15 if 0<=hours<=6 else 10 if 0<=hours<=24 else 5 if 0<=hours<=72 else 0,
           'source_confidence':round(confidence_score/15),'corroboration':min(5,max(0,families-1)*5),
           'time_sensitivity':5 if meta['urgency']=='Immediate' else 0,
           'operator_priority':int(policy['category_priorities'].get(cat,0)) + min(10, max(0, int(raw.get('_chokepoint_boost', 0))))}
    reason=''; lane='context' if cat=='top-news' else 'inbox'; expired=bool(meta['expires'] and meta['expires']<=now)
    if timestamp(raw.get('_superseded_at')) and timestamp(raw['_superseded_at'])<=now: expired=True
    if raw.get('status') in {'resolved','postmortem'} or raw.get('_snapshot_active') is False: expired=True
    if cat in {'weather','fire-weather'}:
        lane='now'
        vtec=' '.join(p.get('parameters',{}).get('VTEC',[]))
        if p.get('messageType')=='Cancel' or '/O.CAN.' in vtec or '/O.EXP.' in vtec: expired=True
        if expired: reason='Warning ended or cancelled'
        elif not meta['expires']: reason='No verified weather expiration'
        elif meta['onset'] and meta['onset']>now: reason='Not yet active'
        elif p.get('status','Actual')!='Actual': reason='Test/exercise weather bulletin'
        elif meta['event'] not in LIFE_SAFETY | FIRE_WEATHER: reason='Routine weather; explicit broad NOW view only'
        elif cat == 'fire-weather':
            parts['impact'] = max(parts['impact'], 20 if meta['event']=='Red Flag Warning' else 10)
            parts['category'] = 15
    elif cat=='earthquake':
        mag=p.get('mag',0) or 0
        felt=p.get('felt') or 0
        consequential_quake=(mag>=policy['regional_quake_magnitude'] or p.get('alert') in {'orange','red'}
                             or (area=='ETN/WNC' and mag>=1.5 and isinstance(felt,(int,float)) and felt>=5))
        # Magnitude thresholds are area-neutral. Geography only changes points.
        if not consequential_quake: reason='Routine earthquake below magnitude threshold'
        else: parts['impact']=30; parts['category']=15
    elif cat=='space-weather':
        if not re.search(r'\b[GSR][3-5]\b|\bK-index of [7-9]\b',row['body'],re.I): reason='Below consequential space-weather threshold'
        else: parts['impact']=25;parts['category']=15
    elif row['source_family']=='gdacs':
        if not row['title'].lower().startswith('red '): reason='Non-exceptional global disaster notice'
        else: parts['impact']=30
    elif cat in {'fuel','commodity'}:
        fuel_signal = r'\b(?:gas(?:oline)?|diesel|oil|fuel|crude|barrel)\b.{0,80}\b(?:price|cost|rise|surge|spike|increase|record|shortage|supply|disruption|shipping)\b|\b(?:price|cost|record|shortage|supply|disruption)\b.{0,80}\b(?:gas(?:oline)?|diesel|oil|fuel|crude|barrel)\b'
        if not re.search(fuel_signal, text, re.I): reason='No clear fuel-price or supply-impact signal'
        else: parts['impact']=20
    elif cat=='top-news':
        # These are deliberately isolated from the operational inbox. Their job
        # is to prompt a local-impact check, not to become transmit-ready facts.
        parts['impact']=5
        context_pattern=(r'\b(white house|congress|supreme court|federal government|president|election|government shutdown|'
                         r'tariff|sanction|inflation|interest rate|recession|market crash|bank failure|war|attack|airstrike|'
                         r'ceasefire|invasion|missile|military|nuclear|cyberattack|outage|airport|flight|rail|port|shipping|'
                         r'supply chain|shortage|outbreak|pandemic|epidemic|recall|hurricane|tornado|flood|wildfire|earthquake|breach|hacked|hack|ransomware|fbi|federal bureau|data leak|immigration|ice|deportation|national guard|federal agents?|civil rights|shooting|protest|unrest|hormuz|bab al[- ]mandab|refinery|diesel|gasoline|fuel crisis|beef|restaurant closures?|fast food|w[ée]ndy)\b')
        fuel_pattern=r'\b(?:gas(?:oline)?|diesel|oil|fuel)\b.{0,50}\b(?:price|cost|rise|surge|spike|increase|shortage|supply)\b|\b(?:price|cost)\b.{0,50}\b(?:gas(?:oline)?|diesel|oil|fuel)\b'
        if not re.search(context_pattern+'|'+fuel_pattern,text,re.I): reason='No clear national operational or civic impact trigger'
        durable = bool(re.search(r'\b(?:fuel|diesel|gasoline|oil|hormuz|bab al[- ]mandab|immigration|ice|deportation|national guard|civil rights|unrest|protest)\b', text, re.I))
        if durable:
            parts['impact'] = 15
        if hours > (168 if durable else 36): reason='Top-news context is older than its freshness window'
    elif cat=='chatter':
        # Community/body text often contains navigation, promos, or incidental
        # words such as “shelter.” Require the actual headline to carry the
        # event signal before it enters the operator inbox.
        seasonal_pattern=r'\b(leaf season|fall color|autumn color|park conditions?|visitor access|visitation|tourism impact|trail closure|park road closure|campground closure)\b'
        if re.search(seasonal_pattern, text, re.I):
            parts['impact'] = max(parts['impact'], 10)
        elif not EVENT_SIGNAL.search(row['title']): reason='No concrete event signal in the community headline'
    elif cat == 'notam':
        if NOTAM_PUBLIC_IMPACT_PATTERN.search(text):
            parts['impact'] = max(parts['impact'], 15)
        else:
            reason = 'Routine aviation NOTAM (operational/maintenance) - no public-impact signal'
    elif cat == 'regional' and 'source_type' in row.keys() and row['source_type'] == 'media' and not any(pattern.search(row['title']) for pattern in GHOSTNET_PATTERNS.values()):
        reason='No regional operational or unusual event signal in headline'
    elif cat == 'transportation' and row['source_family'] == 'tdot-smartway' and \
            str(raw.get('EVENT_SUBTYPE', '')).lower() == 'congestion' and \
            'no lanes blocked' in str(raw.get('VEHICLE_IMPACT', '')).lower():
        reason='Routine congestion with no lanes blocked'
    elif cat == 'civil-unrest':
        pass  # Classification is the event signal; no narrower second gate.
    elif cat == 'public-safety':
        # This category previously fell through to the unconditional
        # 'geography and severity rank reports' pass-through below, which had
        # no content gate at all - a routine sports score update or a
        # no-local-impact national announcement mis-tagged public-safety
        # would sail through on geography/freshness points alone. Require the
        # same kind of concrete event signal 'chatter' already requires.
        if not (EVENT_SIGNAL.search(row['title']) or GHOSTNET_PATTERNS['public_safety'].search(row['title'])):
            reason='No concrete public-safety event signal in headline'
    elif cat in OPERATIONAL:
        pass  # Geography and severity rank reports; neither hides them.
    else: reason='Outside operational categories'
    if cat == 'regional' and 'source_type' in row.keys() and row['source_type'] == 'media' and area not in {'ETN/WNC', 'REGIONAL'}:
        reason='No verified TN/KY/GA/NC location in search result'
    # Applies regardless of source_type: a 'regional' item with no stated or
    # inferable location/regional connection is downgraded/rejected either way.
    if cat == 'regional' and area not in {'ETN/WNC', 'REGIONAL'} and not reason:
        reason='No location or regional connection stated'
    if not reason and SPORTS_PATTERN.search(row['title']) and not EVENT_SIGNAL.search(row['title']):
        reason='Routine sports update with no documented abnormal local impact'
    if not reason and area == 'GLOBAL' and cat in {'public-safety','regional','civil-liberties','emergency'} \
            and not (EVENT_SIGNAL.search(row['title']) or GHOSTNET_PATTERNS['public_safety'].search(row['title'])):
        reason='Global item with no documented local impact'
    noise = policy['noise_policies'].get(row['source_family']) or policy['noise_policies'].get(cat)
    if noise:
        if noise.get('suppress') and not reason:
            reason = noise.get('reason') or f"Suppressed by noise policy for {row['source_family'] or cat}"
        noise_max_age = noise.get('max_age_hours')
        if noise_max_age is not None and hours > noise_max_age:
            reason = f"Stale under noise policy for {row['source_family'] or cat} (> {noise_max_age}h)"
        noise_min_score = noise.get('min_score')
        if noise_min_score is not None and not reason:
            parts['noise_policy_floor'] = -abs(int(noise_min_score))  # visible in the breakdown, not a silent exclusion
    if expired and not reason: reason='Incident ended'
    if hours<-.25: reason='Future-dated report'
    if hours>policy['max_age_hours']: reason='Outside freshness window'
    if cat in {'regional', 'public-health', 'public-safety', 'civil-liberties', 'transportation'} and area in {'ETN/WNC', 'REGIONAL'}:
        parts['ghostnet_interest'] = min(15, interest // 5)
        parts['novelty'] = min(5, novelty // 5)
    score=max(0,min(100,sum(parts.values())))
    if routine_content(row['title']) and not expired and reason not in {'Future-dated report','Outside freshness window'}:
        reason='Routine newsletter or announcement'
    return {'area':area,'category':cat,'score':score,'breakdown':parts,
            'locality_score': geo, 'ghostnet_interest_score': interest, 'ghostnet_interest_families': list(interest_families), 'novelty_score': novelty,
            'published':iso(published),'event_at':iso(meta['event_at']),
            'expires':iso(meta['expires']),'onset':iso(meta['onset']),'event':meta['event'],
            'urgency':meta['urgency'],'certainty':meta['certainty'],'lane':lane,'reason':reason,'expired':expired}


def assess(claim, rows, policy=None, now=None):
    label,conf,families,official,explanation=confidence(rows)
    # Preserve all evidence; evaluate latest revision of each originating event.
    latest={}
    for row in rows:
        key=(row['source_family'],row['external_id'])
        if key not in latest or row['observed_at']>=latest[key]['observed_at']: latest[key]=row
    items=[(evaluate(r,policy,now,conf,families),r) for r in latest.values()]
    active=[x for x in items if not x[0]['reason']]
    chosen,row=max(active or items,key=lambda x:(not x[0]['expired'],x[0]['score'],x[0]['published']))
    result=dict(claim);result.update(chosen)
    observed = [timestamp(item['observed_at']) for item in rows if timestamp(item['observed_at'])]
    window_seconds = int((max(observed)-min(observed)).total_seconds()) if len(observed) > 1 else 0
    chatter_window = '<1h' if window_seconds < 3600 else f'{window_seconds//3600}h' if window_seconds < 86400 else f'{window_seconds//86400}d'
    from .gazetteer import resolve
    found = resolve(row['title']) if 'source_type' in row.keys() and row['source_type'] == 'media' else None
    display_location = row['location'] or (found['location'] if found and found['confidence'] in {'explicit', 'unique_place'} else '')
    result.update(title=row['title'],location=display_location,confidence_label=label,confidence_score=conf,
                  independent_families=families,official_families=official,confidence_explanation=explanation,
                  significance_score=chosen['score'], chatter_mentions=len(rows),
                  chatter_sources=len({item['source_id'] for item in rows}), chatter_window=chatter_window)
    if all(x[0]['expired'] for x in items) and result['status'] not in {'SENT','REJECTED'}: result['status']='EXPIRED'
    return result
