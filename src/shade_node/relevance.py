"""Pure, clock-injectable relevance policy. No network or database side effects."""
from datetime import datetime, timezone, timedelta
import json
import re

DEFAULT_POLICY = {
    'minimum_score': 45, 'max_age_hours': 168, 'global_quake_magnitude': 7.5,
    'regional_quake_magnitude': 3.5, 'domestic_quake_magnitude': 6.5,
    'local_terms': ['east tennessee', 'eastern tennessee', 'western north carolina', 'knoxville', 'asheville', 'tri-cities'],
    'regional_states': ['TN', 'KY', 'GA', 'NC'],
    'local_counties': ['Knox, TN','Blount, TN','Sevier, TN','Anderson, TN','Loudon, TN','Roane, TN','Monroe, TN','McMinn, TN','Bradley, TN','Hamilton, TN','Campbell, TN','Claiborne, TN','Union, TN','Grainger, TN','Jefferson, TN','Hamblen, TN','Cocke, TN','Greene, TN','Washington, TN','Sullivan, TN','Carter, TN','Johnson, TN','Unicoi, TN','Hawkins, TN','Hancock, TN','Scott, TN','Morgan, TN','Cumberland, TN','Bledsoe, TN','Rhea, TN','Meigs, TN','Polk, TN','Sequatchie, TN','Marion, TN','Buncombe, NC','Haywood, NC','Henderson, NC','Madison, NC','Yancey, NC','Mitchell, NC','Avery, NC','Watauga, NC','Ashe, NC','Alleghany, NC','Wilkes, NC','Caldwell, NC','Burke, NC','McDowell, NC','Rutherford, NC','Polk, NC','Transylvania, NC','Jackson, NC','Swain, NC','Macon, NC','Graham, NC','Clay, NC','Cherokee, NC'],
    'category_priorities': {},
}
OPERATIONAL = {'cyber','infrastructure','grid','fuel','communications','transportation','supply-chain','public-health','public-safety','emergency','chatter','top-news','civil-unrest'}
LIFE_SAFETY = {'Tornado Warning','Flash Flood Warning','Civil Emergency Message','Evacuation Immediate','Shelter In Place Warning','Extreme Wind Warning','Tsunami Warning'}


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
            'published':timestamp(row['published_at']) or (timestamp(row['observed_at']) if raw.get('_snapshot') else None), 'event':p.get('event',''),
            'urgency':p.get('urgency',''), 'certainty':p.get('certainty','')}


def geography(row, meta, policy):
    # Affected area, never issuing weather office, determines local relevance.
    area=row['location'] or meta['properties'].get('areaDesc','') or meta['raw'].get('_area','')
    area=area.replace(' (County)','')
    text=area if meta['category']=='weather' else area+' '+row['title']
    if any(re.search(r'(?<!\w)'+re.escape(t)+r'(?!\w)',text,re.I) for t in policy['local_terms']+policy['local_counties']):
        return 'ETN/WNC',30
    states=policy['regional_states']
    if any(re.search(r'\b'+re.escape(t)+r'\b',text) for t in states) or re.search(r'\b(Tennessee|Kentucky|North Carolina)\b',text,re.I):
        return 'REGIONAL',20
    # Structured USGS coordinates establish southeast/domestic context.
    coords=(meta['raw'].get('geometry') or {}).get('coordinates',[])
    if meta['category']=='earthquake' and len(coords)>=2 and isinstance(coords[0],(int,float)):
        lon,lat=coords[:2]
        if -90<=lon<=-75 and 30<=lat<=39: return 'REGIONAL',20
        if -125<=lon<=-66 and 24<=lat<=50: return 'NATIONAL',12
    if meta['category']=='weather': return 'DISTANT',0
    if meta['raw'].get('_area')=='NATIONAL' or re.search(r'\b(United States|USA|Alaska|Hawaii)\b',text): return 'NATIONAL',12
    if meta['category']=='space-weather': return 'NATIONAL',12
    return 'GLOBAL',0


def confidence(rows):
    families={r['source_family'] for r in rows}
    official={r['source_family'] for r in rows if r['source_type']=='official'}
    media={r['source_family'] for r in rows if r['source_type']=='media'}
    if official and len(families)>=2: label,score='CONFIRMED',90
    elif official: label,score='OFFICIAL-REPORT',75
    elif len(media)>=2 or (media and len(families)>=2): label,score='CORROBORATED',60
    elif any(r['source_type']=='community' for r in rows): label,score='UNVERIFIED',20
    else: label,score='REPORTED',35
    explanation=f'{len(families)} configured originating family/families; {len(official)} official. Reposts in one family count once; this label is attribution, not proof of truth.'
    return label,score,len(families),len(official),explanation


def evaluate(row, policy=None, now=None, confidence_score=75, families=1):
    policy={**DEFAULT_POLICY,**(policy or {})}; now=now or datetime.now(timezone.utc)
    meta=metadata(row); raw=meta['raw']; p=meta['properties']; cat=meta['category']
    area,geo=geography(row,meta,policy)
    published=meta['published']; hours=(now-published).total_seconds()/3600 if published else float('inf')
    impact={'unknown':0,'minor':3,'moderate':10,'severe':20,'extreme':30}.get(row['severity'],0)
    text=(row['title']+' '+row['body']).lower()
    operational_pattern=r'\b(outage|disruption|closure|closed|evacuation|exploited|exploitation|ransomware|shutdown|shortage|outbreak|emergency declaration|fire|explosion|crash|collision|shooting|stabbing|missing person|hazmat|hazardous materials|boil water|water main|power line|brush fire|wildfire|rescue|fatal|deadly|bridge|road|interstate|shelter|hospital|school lockdown)\b'
    consequential=bool(re.search(operational_pattern,text))
    if cat=='cyber' and row['source_family']=='cisa' and 'cveID' not in raw:
        # Advisory templates often say there is NO known public exploitation.
        positive=re.sub(r'no (?:known )?(?:public )?exploits?[^.]*|no (?:known )?exploitation[^.]*','',text)
        consequential=bool(re.search(r'known exploited|actively exploited|active exploitation|ransomware campaign|critical infrastructure (?:attack|disruption)',positive))
    if consequential and cat in OPERATIONAL and cat!='chatter': impact=max(impact,20)
    parts={'geography':geo,'category':15 if cat in OPERATIONAL else 5,
           'impact':impact,'freshness':15 if 0<=hours<=6 else 10 if 0<=hours<=24 else 5 if 0<=hours<=72 else 0,
           'source_confidence':round(confidence_score/15),'corroboration':min(5,max(0,families-1)*5),
           'time_sensitivity':5 if meta['urgency']=='Immediate' else 0,
           'operator_priority':int(policy['category_priorities'].get(cat,0))}
    reason=''; lane='context' if cat=='top-news' else 'inbox'; expired=bool(meta['expires'] and meta['expires']<=now)
    if timestamp(raw.get('_superseded_at')) and timestamp(raw['_superseded_at'])<=now: expired=True
    if raw.get('status') in {'resolved','postmortem'} or raw.get('_snapshot_active') is False: expired=True
    if cat=='weather':
        lane='now'
        vtec=' '.join(p.get('parameters',{}).get('VTEC',[]))
        if p.get('messageType')=='Cancel' or '/O.CAN.' in vtec or '/O.EXP.' in vtec: expired=True
        if expired: reason='Warning ended or cancelled'
        elif not meta['expires']: reason='No verified weather expiration'
        elif meta['onset'] and meta['onset']>now: reason='Not yet active'
        elif p.get('status','Actual')!='Actual': reason='Test/exercise weather bulletin'
        elif meta['event'] not in LIFE_SAFETY: reason='Routine weather; explicit broad NOW view only'
        elif area!='ETN/WNC': reason='Outside primary local weather area'
    elif cat=='earthquake':
        mag=p.get('mag',0) or 0; sig=p.get('sig',0) or 0; felt=p.get('felt',0) or 0
        exceptional=mag>=policy['global_quake_magnitude'] or p.get('alert') in {'orange','red'}
        # USGS tsunami flag means a tsunami product was issued, not a confirmed threat.
        exceptional=exceptional or (p.get('tsunami')==1 and mag>=7 and hours<=12)
        regional=area in {'ETN/WNC','REGIONAL'} and mag>=policy['regional_quake_magnitude']
        domestic=area=='NATIONAL' and mag>=policy['domestic_quake_magnitude'] and (felt>=100 or sig>=1000)
        if not (exceptional or regional or domestic): reason='Remote/routine earthquake without substantial operational impact'
        else: parts['impact']=30; parts['category']=15
    elif cat=='space-weather':
        if not re.search(r'\b[GSR][3-5]\b|\bK-index of [7-9]\b',row['body'],re.I): reason='Below consequential space-weather threshold'
        else: parts['impact']=25;parts['category']=15
    elif row['source_family']=='gdacs':
        if not row['title'].lower().startswith('red '): reason='Non-exceptional global disaster notice'
        else: parts['impact']=30
    elif cat=='top-news':
        # These are deliberately isolated from the operational inbox. Their job
        # is to prompt a local-impact check, not to become transmit-ready facts.
        parts['impact']=5
        context_pattern=(r'\b(white house|congress|supreme court|federal government|president|election|government shutdown|'
                         r'tariff|sanction|inflation|interest rate|recession|market crash|bank failure|war|attack|airstrike|'
                         r'ceasefire|invasion|missile|military|nuclear|cyberattack|outage|airport|flight|rail|port|shipping|'
                         r'supply chain|shortage|outbreak|pandemic|epidemic|recall|hurricane|tornado|flood|wildfire|earthquake|breach|hacked|hack|ransomware|fbi|federal bureau|data leak)\b')
        fuel_pattern=r'\b(?:gas(?:oline)?|diesel|oil|fuel)\b.{0,50}\b(?:price|cost|rise|surge|spike|increase|shortage|supply)\b|\b(?:price|cost)\b.{0,50}\b(?:gas(?:oline)?|diesel|oil|fuel)\b'
        if not re.search(context_pattern+'|'+fuel_pattern,text,re.I): reason='No clear national operational or civic impact trigger'
        elif hours>36: reason='Top-news context is older than 36 hours'
    elif cat=='chatter':
        # Community/body text often contains navigation, promos, or incidental
        # words such as “shelter.” Require the actual headline to carry the
        # event signal before it enters the operator inbox.
        chatter_pattern=r'\b(outage|disruption|closure|closed|evacuation|shortage|outbreak|emergency declaration|fire|explosion|crash|collision|shooting|stabbing|missing person|hazmat|hazardous materials|boil water|water main|power line|brush fire|wildfire|fatal|deadly|lockdown|shelter in place)\b'
        if not re.search(chatter_pattern,row['title'],re.I): reason='No concrete event signal in the community headline'
    elif cat == 'civil-unrest':
        if not re.search(r'\b(protest|riot|clash|riot|unrest|demonstration|strike|violence|civilian|killed|fatalit)', text, re.I):
            reason='No civil-unrest event signal'
    elif cat in OPERATIONAL:
        if not consequential and impact<20: reason='No concrete operational impact identified'
        if area=='GLOBAL' and not raw.get('_us_impact'): reason='No established domestic/regional impact'
    else: reason='Outside operational categories'
    if expired and not reason: reason='Incident ended'
    if hours<-.25: reason='Future-dated report'
    if hours>policy['max_age_hours']: reason='Outside freshness window'
    score=max(0,min(100,sum(parts.values())))
    if not reason and score<policy['minimum_score']: reason='Below configured relevance threshold'
    return {'area':area,'category':cat,'score':score,'breakdown':parts,'published':iso(published),
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
    result.update(title=row['title'],location=row['location'],confidence_label=label,confidence_score=conf,
                  independent_families=families,official_families=official,confidence_explanation=explanation,
                  significance_score=chosen['score'])
    if all(x[0]['expired'] for x in items) and result['status'] not in {'SENT','REJECTED'}: result['status']='EXPIRED'
    return result
