from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
import urllib.error
import urllib.request
import os
from html import unescape
import xml.etree.ElementTree as ET

from .model import Observation, SourceConfig, iso_utc
from .relevance import timestamp
from hashlib import sha256


class CollectionError(RuntimeError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after=retry_after


def fetch_bytes(url: str, *, user_agent: str, timeout: int, max_bytes: int, headers: dict | None = None) -> bytes:
    if not url.lower().startswith("https://"):
        raise CollectionError(f"refusing non-HTTPS source: {url}")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "application/json, application/geo+json, application/rss+xml, application/xml, text/xml;q=0.9", **(headers or {})},
    )
    class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not newurl.lower().startswith('https://'): raise CollectionError('Refusing non-HTTPS redirect')
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    try:
        with urllib.request.build_opener(HTTPSRedirect).open(request, timeout=timeout) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise CollectionError(f"response exceeds {max_bytes} bytes")
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        retry=exc.headers.get('Retry-After','')
        try: seconds=int(retry)
        except ValueError:
            try: seconds=max(0,int((parsedate_to_datetime(retry)-datetime.now(timezone.utc)).total_seconds()))
            except (TypeError,ValueError): seconds=0
        raise CollectionError('HTTP '+str(exc.code),retry_after=seconds) from exc
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise CollectionError(str(exc)) from exc
    if len(data) > max_bytes:
        raise CollectionError(f"response exceeds {max_bytes} bytes")
    return data


def _json(data: bytes):
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectionError(f"invalid JSON: {exc}") from exc


def _ms_epoch(value) -> str:
    try:
        return iso_utc(datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc))
    except (TypeError, ValueError, OSError):
        return iso_utc(None)


def parse_nws(source: SourceConfig, data: bytes) -> list[Observation]:
    payload = _json(data)
    observations = []
    for feature in payload.get("features", []):
        props = feature.get("properties", {})
        external_id = props.get("id") or feature.get("id") or props.get("@id") or props.get("sent") or props.get("headline")
        observations.append(
            Observation(
                source_id=source.id,
                source_name=source.name,
                source_type=source.source_type,
                source_family=source.source_family,
                external_id=str(external_id),
                title=props.get("headline") or props.get("event") or "NWS alert",
                body=props.get("description") or props.get("instruction") or "",
                url=props.get("@id") or feature.get("id") or source.url,
                category="weather",
                location=props.get("areaDesc") or "",
                severity=(props.get("severity") or "unknown").lower(),
                published_at=iso_utc(props.get("sent") or props.get("effective")) if timestamp(props.get("sent") or props.get("effective")) else "",
                raw=feature,
            )
        )
    return observations


def parse_usgs(source: SourceConfig, data: bytes) -> list[Observation]:
    payload = _json(data)
    observations = []
    for feature in payload.get("features", []):
        props = feature.get("properties", {})
        magnitude = props.get("mag")
        severity = "extreme" if isinstance(magnitude, (int, float)) and magnitude >= 7 else "severe" if isinstance(magnitude, (int, float)) and magnitude >= 6 else "moderate"
        observations.append(
            Observation(
                source_id=source.id,
                source_name=source.name,
                source_type=source.source_type,
                source_family=source.source_family,
                external_id=str(feature.get("id") or props.get("code") or props.get("time")),
                title=props.get("title") or f"Magnitude {magnitude} earthquake",
                body=f"Magnitude {magnitude}; tsunami flag {props.get('tsunami', 0)}; felt reports {props.get('felt', 0)}.",
                url=props.get("url") or source.url,
                category="earthquake",
                location=props.get("place") or "",
                severity=severity,
                published_at=_ms_epoch(props.get("time")),
                raw=feature,
            )
        )
    return observations


def parse_swpc(source: SourceConfig, data: bytes) -> list[Observation]:
    payload = _json(data)
    observations = []
    for item in payload if isinstance(payload, list) else []:
        product_code = item.get("product_id") or item.get("message_id") or "SWPC"
        product_id = f"{product_code}|{item.get('issue_datetime') or item.get('message', '')[:80]}"
        message = (item.get("message") or "").strip()
        first_line = next((line.strip() for line in message.splitlines() if line.strip().startswith(("WATCH:","WARNING:","ALERT:"))), "NOAA SWPC alert")
        observations.append(
            Observation(
                source_id=source.id,
                source_name=source.name,
                source_type=source.source_type,
                source_family=source.source_family,
                external_id=product_id,
                title=first_line[:240],
                body=message,
                url=source.url,
                category="space-weather",
                location="GLOBAL",
                severity="moderate",
                published_at=iso_utc(item.get("issue_datetime")),
                raw=item,
            )
        )
    return observations


def _node_text(node: ET.Element, local_name: str) -> str:
    for child in node.iter():
        if child.tag.rsplit("}", 1)[-1].lower() == local_name.lower():
            return (child.text or "").strip()
    return ""


def parse_rss(source: SourceConfig, data: bytes) -> list[Observation]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise CollectionError(f"invalid XML: {exc}") from exc
    items = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}]
    observations = []
    for item in items:
        title = _node_text(item, "title") or "Untitled report"
        body = _node_text(item, "description") or _node_text(item, "summary") or _node_text(item, "content")
        link = _node_text(item, "link")
        if not link:
            for child in item:
                if child.tag.rsplit("}", 1)[-1].lower() == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break
        external_id = _node_text(item, "guid") or _node_text(item, "id") or link or title
        date_text = _node_text(item, "pubDate") or _node_text(item, "published") or _node_text(item, "updated")
        try:
            published = iso_utc(parsedate_to_datetime(date_text)) if date_text else ""
        except (TypeError, ValueError, OverflowError):
            published = iso_utc(date_text) if timestamp(date_text) else ""
        observations.append(
            Observation(
                source_id=source.id,
                source_name=source.name,
                source_type=source.source_type,
                source_family=source.source_family,
                external_id=external_id,
                title=title,
                body=body,
                url=link or source.url,
                category=source.category,
                published_at=published,
                raw={"title": title, "body": body, "link": link, "published": date_text, "xml": ET.tostring(item, encoding="unicode"), "_area": source.area},
            )
        )
    return observations


def parse_kev(source, data):
    payload=_json(data)
    if not isinstance(payload,dict) or not isinstance(payload.get('vulnerabilities'),list):
        raise CollectionError('Invalid CISA KEV schema')
    return [Observation(source.id,source.name,source.source_type,source.source_family,
                        item['cveID'],item['cveID']+' - '+item['vulnerabilityName'],
                        'Known exploited vulnerability. '+item['shortDescription']+' Required action: '+item['requiredAction'],
                        'https://www.cisa.gov/known-exploited-vulnerabilities-catalog',
                        category='cyber',location='United States',severity='severe',
                        published_at=iso_utc(item['dateAdded']),raw={**item,'_area':'NATIONAL'})
            for item in payload['vulnerabilities']]


def parse_status(source, data):
    payload=_json(data)
    if not isinstance(payload,dict) or not isinstance(payload.get('incidents'),list):
        raise CollectionError('Invalid incident API schema')
    result=[]
    for item in payload['incidents']:
        components=' '.join(c.get('name','') for c in item.get('components',[]))
        # Global vendor reporting alone does not establish US impact.
        us_impact=any(t in components for t in ('United States','North America','Atlanta','Ashburn','Chicago','Dallas','Los Angeles'))
        result.append(Observation(source.id,source.name,source.source_type,source.source_family,item['id'],item['name'],
                      '\n'.join(u.get('body','') for u in item.get('incident_updates',[])),item.get('shortlink') or source.url,
                      category='communications',location=components,severity='severe' if item.get('impact') in {'major','critical'} else 'minor',
                      published_at=item.get('created_at',''),raw={**item,'_area':'NATIONAL' if us_impact else 'GLOBAL','_us_impact':us_impact,'_snapshot':True}))
    return result


def parse_fema(source, data):
    payload=_json(data)
    if not isinstance(payload,dict) or not isinstance(payload.get('DisasterDeclarationsSummaries'),list):
        raise CollectionError('Invalid OpenFEMA schema')
    result=[]
    for item in payload['DisasterDeclarationsSummaries']:
        result.append(Observation(source.id,source.name,source.source_type,source.source_family,item['id'],
                      'Disaster declaration: '+item['declarationTitle'],
                      'Emergency declaration and assistance eligibility; not a real-time incident warning.',
                      'https://www.fema.gov/disaster/'+str(item['disasterNumber']),category='public-safety',
                      location=item['designatedArea']+', '+item['state'],severity='severe',
                      published_at=item['declarationDate'],raw=item))
    return result


def parse_faa(source, data):
    try: root=ET.fromstring(data)
    except ET.ParseError as exc: raise CollectionError('Invalid FAA XML') from exc
    if root.tag!='AIRPORT_STATUS_INFORMATION': raise CollectionError('Invalid FAA status schema')
    result=[]
    places={'TYS':'Knoxville TN','TRI':'Tri-cities TN','AVL':'Asheville NC','CHA':'Chattanooga TN',
            'BNA':'Nashville TN','MEM':'Memphis TN','ATL':'Atlanta GA','CLT':'Charlotte NC','RDU':'Raleigh NC','SDF':'Louisville KY','LEX':'Lexington KY'}
    for group in root.findall('Delay_type'):
        kind=group.findtext('Name','')
        for item in group.iter():
            airport=item.findtext('ARPT')
            if not airport: continue
            reason=item.findtext('Reason','');average=item.findtext('Avg','')
            hours=re.search(r'(\d+) hour',average)
            major_delay=bool(hours and int(hours[1])>=2)
            # General aviation restrictions are not a full passenger-airport shutdown.
            closure=kind=='Airport Closures' and not re.search(r'TRANSIENT|NON SKED|PPR',reason,re.I)
            severe=closure or major_delay
            identity=sha256((airport+'|'+kind+'|'+reason+'|'+item.findtext('Start','')).encode()).hexdigest()
            raw={'xml':ET.tostring(item,encoding='unicode'),'_snapshot':True,'_area':'NATIONAL',
                 'event_type':kind,'airport':airport,'average_delay':average}
            result.append(Observation(source.id,source.name,source.source_type,source.source_family,identity,
                          ('Major transportation disruption: ' if severe else 'Routine airport status: ')+airport+' '+kind,
                          reason+' Average delay: '+average,source.url,category='transportation',
                          location=places.get(airport,'United States: '+airport),severity='severe' if severe else 'minor',
                          published_at='',raw=raw))
    return result


def parse_tdot(source, data):
    """Parse the public SmartWay ArcGIS event layer without surfacing routine work."""
    payload=_json(data)
    if not isinstance(payload,dict) or not isinstance(payload.get('features'),list):
        raise CollectionError('Invalid TDOT SmartWay schema')
    result=[]
    for feature in payload['features']:
        item=feature.get('attributes') or {}
        kind=item.get('EVENT_TYPE') or ''
        subtype=item.get('EVENT_SUBTYPE') or kind or 'Traffic event'
        impact=' '.join(filter(None,(item.get('VEHICLE_IMPACT'),item.get('OPPOSITE_VEHICLE_IMPACT'))))
        # SmartWay contains a large volume of routine paving and maintenance.
        # Keep incidents/weather plus genuinely full closures; retain the raw
        # record so the operator can inspect exactly what TDOT published.
        major=kind in {'Incident','Weather'} or bool(re.search(r'\ball lanes blocked\b|\b(?:road|ramp|direction) closed\b',impact,re.I))
        if not major:
            continue
        county=(item.get('COUNTY_NAME') or '').strip()
        start=_ms_epoch(item.get('START_DATE')) if item.get('START_DATE') is not None else ''
        closure=bool(re.search(r'\bclosed\b|\ball lanes blocked\b',impact,re.I))
        raw={**item,'geometry':feature.get('geometry') or {},'_snapshot':True,'_area':'ETN/WNC'}
        result.append(Observation(source.id,source.name,source.source_type,source.source_family,
                      str(item.get('ID') or item.get('OBJECTID')),subtype+(' — '+impact if impact else ''),
                      item.get('DESCRIPTION') or impact,source.url,category='transportation',
                      location=(county+', TN') if county else 'East Tennessee',
                      severity='severe' if closure or kind=='Weather' else 'moderate',
                      published_at=start,raw=raw))
    return result


def parse_telegram_preview(source, data):
    """Parse Telegram's public HTML preview without fetching media."""
    html = data.decode('utf-8', 'replace')
    blocks = re.findall(r'<div class="tgme_widget_message_wrap".*?</div>\s*</div>', html, re.S)
    result = []
    for block in blocks[:source.max_posts_per_poll]:
        mid = re.search(r'data-post="([^"]+)"', block)
        if not mid:
            continue
        post = mid.group(1)
        text_match = re.search(r'<div class="tgme_widget_message_text[^>]*>(.*?)</div>', block, re.S)
        text = unescape(re.sub(r'<br\s*/?>', '\n', text_match.group(1) if text_match else ''))
        text = re.sub(r'<[^>]+>', '', text).strip()
        date = re.search(r'<time[^>]+datetime="([^"]+)"', block)
        forwarded = re.search(r'tgme_widget_message_forwarded_from_name[^>]*>(.*?)</', block, re.S)
        views = re.search(r'tgme_widget_message_views[^>]*>(.*?)</', block, re.S)
        link = f"https://t.me/{post}"
        result.append(Observation(source.id, source.name, source.source_type, source.source_family,
            post, text[:240] or 'Telegram update', text, link, category=source.category,
            location=source.area, published_at=iso_utc(date.group(1)) if date else '',
            raw={'message_id': post.rsplit('/', 1)[-1], 'telegram_permalink': link,
                 'forwarded_from': unescape(re.sub(r'<[^>]+>', '', forwarded.group(1))).strip() if forwarded else '',
                 'view_count': unescape(re.sub(r'<[^>]+>', '', views.group(1))).strip() if views else '', '_area': source.area}))
    return result


def parse_acled(source, data):
    payload = _json(data)
    rows = payload.get('data') if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise CollectionError('Invalid ACLED API schema')
    result = []
    for item in rows:
        event_id = str(item.get('data_id') or item.get('event_id') or item.get('id') or sha256(json.dumps(item, sort_keys=True).encode()).hexdigest())
        location = ', '.join(x for x in (item.get('admin1'), item.get('admin2'), item.get('country')) if x)
        event = item.get('event_type') or item.get('sub_event_type') or 'ACLED event'
        notes = item.get('notes') or item.get('source_scale') or ''
        result.append(Observation(source.id, source.name, 'official', source.source_family, event_id,
            f"{event}: {item.get('location') or item.get('admin2') or 'unlocated'}",
            f"Actors: {item.get('actor1','')} / {item.get('actor2','')}. {notes}", source.url,
            category='civil-unrest', location=location, severity='severe' if (item.get('fatalities') or 0) else 'moderate',
            published_at=iso_utc(item.get('event_date') or item.get('timestamp')),
            raw={**item, '_area': source.area or 'NATIONAL', 'fatalities': item.get('fatalities', 0)}))
    return result


PARSERS = {
    "nws_alerts": parse_nws,
    "usgs_geojson": parse_usgs,
    "swpc_alerts": parse_swpc,
    "rss": parse_rss,
    "cisa_kev": parse_kev,
    "fema_declarations": parse_fema,
    "faa_status": parse_faa,
    "status_api": parse_status,
    "tdot_events": parse_tdot,
    "telegram_preview": parse_telegram_preview,
    "acled_api": parse_acled,
}


def collect(source: SourceConfig, *, user_agent: str, timeout: int, max_bytes: int) -> list[Observation]:
    parser = PARSERS.get(source.kind)
    if not parser:
        raise CollectionError(f"unknown collector kind: {source.kind}")
    headers = {}
    if source.api_key_env:
        key = os.environ.get(source.api_key_env, '')
        if not key:
            raise CollectionError(f"missing credential environment variable: {source.api_key_env}")
        headers['Authorization'] = f'Bearer {key}'
    data=fetch_bytes(source.url,user_agent=user_agent,timeout=timeout,max_bytes=max_bytes,headers=headers)
    try:
        return parser(source,data)
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError) as exc:
        raise CollectionError('Feed schema does not match '+source.kind) from exc
