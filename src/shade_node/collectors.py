from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from email.utils import parsedate_to_datetime
import json
import re
from math import radians, sin, cos, asin, sqrt
import urllib.error
import urllib.request
import os
import csv
import io
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse
from html import unescape
import xml.etree.ElementTree as ET

from .model import Observation, SourceConfig, iso_utc
from .relevance import timestamp
from .keywords import hashtags
from .regional_discovery import search_queries
from hashlib import sha256


class NotModified(RuntimeError):
    pass


class PartialCollectionError(RuntimeError):
    def __init__(self, message, observations):
        super().__init__(message)
        self.observations = observations
        self.retry_after = 0


class CollectionError(RuntimeError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after=retry_after


def fetch_bytes(url: str, *, user_agent: str, timeout: int, max_bytes: int, headers: dict | None = None, return_headers: bool = False):
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
    response_headers = {}
    try:
        with urllib.request.build_opener(HTTPSRedirect).open(request, timeout=timeout) as response:
            response_headers = dict(response.headers.items())
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise CollectionError(f"response exceeds {max_bytes} bytes")
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            raise NotModified() from exc
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
    return (data, response_headers) if return_headers else data


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
        event = props.get("event") or ""
        category = "fire-weather" if event in {"Red Flag Warning", "Fire Weather Watch"} else "weather"
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
                category=category,
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
        publisher = next((child for child in item if child.tag.rsplit('}', 1)[-1].lower() == 'source'), None)
        publisher_name = (publisher.text or '').strip() if publisher is not None else ''
        publisher_host = urlparse(publisher.attrib.get('url', '')).hostname if publisher is not None else None
        is_search = source.kind == 'google_news_search'
        family = publisher_host.removeprefix('www.').lower() if publisher_host else 'google-news-unattributed'
        if is_search and publisher_name and title.casefold().endswith((' - ' + publisher_name).casefold()):
            title = title[:-(len(publisher_name) + 3)].strip()
        date_text = _node_text(item, "pubDate") or _node_text(item, "published") or _node_text(item, "updated")
        try:
            published = iso_utc(parsedate_to_datetime(date_text)) if date_text else ""
        except (TypeError, ValueError, OverflowError):
            published = iso_utc(date_text) if timestamp(date_text) else ""
        raw = {"title": title, "body": body, "link": link, "published": date_text,
               "xml": ET.tostring(item, encoding="unicode"), "_area": "" if is_search else source.area}
        if is_search:
            raw.update(_discovery_source=source.id, _origin_host=family)
        observations.append(
            Observation(
                source_id=source.id,
                source_name=(publisher_name or source.name) if is_search else source.name,
                source_type=source.source_type,
                source_family=source.origin_families.get(family, family) if is_search else source.source_family,
                external_id=external_id,
                title=title,
                body=body,
                url=link or source.url,
                category=source.category,
                published_at=published,
                raw=raw,
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


def parse_notams(source, data):
    """FAA NOTAM Search API (external-api.faa.gov/notamapi/v1/notams).

    UNVERIFIED against a live response: this station has no FAA NOTAM API
    credential configured, so this parser is written defensively against the
    publicly documented response shape (a top-level 'items' list, each with
    an 'icaoLocation'/nested 'properties'/'coreNOTAMData' structure per FAA's
    published schema) and tries a small number of reasonable field-name
    fallbacks rather than assuming one exact shape. Verify field names
    against a real response before relying on this in production - see the
    completion report.

    Captures the notice issue time, effective start, expiration, affected
    location/airspace, altitude limits, source, and the raw notice text, and
    tags every result category='notam' (never 'public-safety') so
    evaluate()'s dedicated NOTAM triage in relevance.py decides whether it is
    public-impact-worthy - this parser does not decide that itself."""
    payload = _json(data)
    items = payload.get('items') if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise CollectionError('Invalid FAA NOTAM API schema')
    result = []
    for item in items:
        core = (item.get('properties') or {}).get('coreNOTAMData', {}).get('notam', {}) if 'properties' in item else item
        notam_id = core.get('id') or core.get('notamNumber') or item.get('notamNumber') or ''
        icao = core.get('icaoLocation') or core.get('location') or item.get('icaoId') or ''
        text = core.get('text') or item.get('text') or item.get('traditionalMessage') or ''
        issued = core.get('issued') or item.get('issued') or ''
        effective_start = core.get('effectiveStart') or item.get('startDate') or issued
        effective_end = core.get('effectiveEnd') or item.get('endDate') or ''
        min_alt = core.get('minimumFL') or item.get('minimumAlt') or ''
        max_alt = core.get('maximumFL') or item.get('maximumAlt') or ''
        if not notam_id or not text:
            continue
        result.append(Observation(
            source.id, source.name, source.source_type, source.source_family, str(notam_id),
            f'NOTAM {icao or "?"}: {text[:120]}', text,
            source.url, category='notam', location=icao,
            severity='unknown', published_at=issued,
            # 'properties.onset'/'expires' feed relevance.metadata()'s existing,
            # generic onset/expiration logic for free - same convention every
            # other structured source (NWS, etc.) already uses - while the
            # '_notam_*' keys preserve the original field names/values for
            # evidence display exactly as the source reported them.
            raw={**item, '_area': source.area or 'REGIONAL',
                 'properties': {**(item.get('properties') or {}), 'onset': effective_start, 'expires': effective_end},
                 '_notam_effective_start': effective_start, '_notam_effective_end': effective_end,
                 '_notam_min_alt': min_alt, '_notam_max_alt': max_alt, '_notam_issued': issued}))
    return result


def parse_faa(source, data):
    try: root=ET.fromstring(data)
    except ET.ParseError as exc: raise CollectionError('Invalid FAA XML') from exc
    if root.tag!='AIRPORT_STATUS_INFORMATION': raise CollectionError('Invalid FAA status schema')
    result=[]
    places={'DCA':'Reagan Washington National Airport','IAD':'Washington Dulles International Airport','TYS':'Knoxville TN','TRI':'Tri-cities TN','AVL':'Asheville NC','CHA':'Chattanooga TN',
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
            ground_stop=bool(re.search(r'ground\s+stop', kind, re.I))
            severe=closure or major_delay or ground_stop
            identity=sha256((airport+'|'+kind+'|'+reason+'|'+item.findtext('Start','')).encode()).hexdigest()
            raw={'xml':ET.tostring(item,encoding='unicode'),'_snapshot':True,'_area':'NATIONAL',
                 'event_type':kind,'airport':airport,'average_delay':average}
            result.append(Observation(source.id,source.name,source.source_type,source.source_family,identity,
                          ('Major transportation disruption: ' if severe else 'Routine airport status: ')+airport+' '+kind,
                          reason+' Average delay: '+average,source.url,category='transportation',
                          location=places.get(airport,'United States: '+airport),severity='severe' if severe else 'minor',
                          published_at='',raw=raw))
    return result


def parse_eia_prices(source, data):
    payload = _json(data)
    rows = payload.get('response', {}).get('data', []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        raise CollectionError('Invalid EIA API schema')
    result = []
    for item in rows:
        series = str(item.get('series') or item.get('seriesId') or item.get('product') or '')
        period = str(item.get('period') or '')
        value = item.get('value')
        if not series or not period or value in (None, ''):
            continue
        description = item.get('series-description') or item.get('seriesDescription') or series
        units = item.get('units') or 'dollars per gallon'
        padd = {'R10':'East Coast / PADD 1', 'R20':'Midwest / PADD 2', 'R30':'Gulf Coast / PADD 3'}
        area = item.get('area-name') or item.get('area') or next((name for code,name in padd.items() if code in series), 'United States')
        result.append(Observation(
            source.id, source.name, 'official', 'eia', f'{series}|{period}',
            f'EIA {description}: {value} {units}',
            f'Official EIA commodity price for {period}: {value} {units}.', source.url,
            category='commodity', location=area, severity='moderate', published_at=period,
            raw={**item, '_area': 'REGIONAL' if any(code in series for code in padd) else 'NATIONAL', '_commodity_price': True},
        ))
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


def parse_usgs_waterservices(source, data):
    payload = _json(data)
    series = payload.get('value', {}).get('timeSeries', []) if isinstance(payload, dict) else []
    if not isinstance(series, list):
        raise CollectionError('Invalid USGS Water Services schema')
    result = []
    for stream in series:
        site = (stream.get('sourceInfo', {}).get('siteCode') or [{}])[0].get('value', '')
        site_name = stream.get('sourceInfo', {}).get('siteName', '')
        variable = stream.get('variable', {})
        parameter = (variable.get('variableCode') or [{}])[0].get('value', '')
        unit = (variable.get('unit') or {}).get('unitCode', '')
        for point in (stream.get('values') or [{}])[0].get('value', []):
            raw_value = point.get('value')
            try: numeric = float(raw_value)
            except (TypeError, ValueError): continue
            when = point.get('dateTime', '')
            exceeded = source.threshold is not None and numeric >= source.threshold
            external = f'{site}|{parameter}|{when}'
            result.append(Observation(source.id, source.name, 'official', 'usgs-waterservices', external,
                f'{site_name or site} {parameter} reading: {numeric:g} {unit}',
                f'USGS site {site}; parameter {parameter}; value {numeric:g} {unit}.', source.url,
                category='regional', location=source.area or 'ETN/WNC',
                severity='severe' if exceeded else 'moderate', published_at=when,
                raw={'site': site, 'site_name': site_name, 'parameter': parameter,
                     'value': numeric, 'unit': unit, 'dateTime': when,
                     '_threshold_exceeded': exceeded, '_area': source.area or 'ETN/WNC'}))
    return result


def parse_radnet(source, data):
    text = data.decode('utf-8-sig', 'replace')
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise CollectionError('RadNet CSV contained no rows')
    result = []
    for row in rows[-50:]:
        normalized = {str(k).strip().lower(): (v or '').strip() for k, v in row.items() if k}
        when = normalized.get('date/time') or normalized.get('datetime') or normalized.get('date') or ''
        value = next((normalized.get(key) for key in ('exposure rate', 'gamma gross count rate', 'value')
                      if normalized.get(key) not in (None, '')), '')
        try: numeric = float(value)
        except (TypeError, ValueError): continue
        station = normalized.get('location') or normalized.get('station') or source.name
        external = f'{station}|{when}|{value}'
        result.append(Observation(source.id, source.name, 'official', 'epa-radnet', external,
            f'RadNet {station}: {numeric:g}', f'RadNet reading {numeric:g} {normalized.get("unit", "")} at {station}.', source.url,
            category='radiological', location=source.area or station, severity='moderate', published_at=when,
            raw={**row, '_area': source.area or 'ETN/WNC'}))
    return result


def parse_safecast(source, data):
    payload = _json(data)
    rows = payload.get('measurements', payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list): raise CollectionError('Invalid Safecast API schema')
    # The API performs the primary geographic query. Recheck returned points so
    # a bad upstream response cannot turn out-of-area data into local reports.
    query = parse_qs(urlparse(source.url).query)
    try:
        center = (float(query['latitude'][0]), float(query['longitude'][0]))
        radius = float(query['distance'][0])
    except (KeyError, IndexError, TypeError, ValueError):
        center, radius = None, None
    result = []
    for item in rows:
        value = next((item.get(key) for key in ('value', 'value_numeric', 'dose_rate')
                      if item.get(key) is not None), None)
        try: numeric = float(value)
        except (TypeError, ValueError): continue
        try:
            lat, lon = float(item['latitude']), float(item['longitude'])
        except (KeyError, TypeError, ValueError):
            if center: continue
            lat = lon = None
        if center:
            lat1, lon1, lat2, lon2 = map(radians, (*center, lat, lon))
            hav = sin((lat2-lat1)/2)**2 + cos(lat1)*cos(lat2)*sin((lon2-lon1)/2)**2
            if 6371000 * 2 * asin(min(1, sqrt(hav))) > radius:
                continue
        location = f'{lat},{lon}' if lat is not None else (source.area or '')
        external = str(item.get('id') or f'{location}|{item.get("captured_at", "")}|{numeric}')
        raw = dict(item)
        if center:
            raw['_area'] = 'ETN/WNC'  # Derived from verified coordinates inside the configured circle.
            raw['_geographic_scope'] = {'latitude': center[0], 'longitude': center[1], 'radius_m': radius}
        result.append(Observation(source.id, source.name, 'community', 'safecast', external,
            f'Safecast reading {numeric:g} {item.get("unit", "")}',
            f'Volunteer sensor reading {numeric:g} {item.get("unit", "")}; sensor {item.get("device_id", "unknown")}.', source.url,
            category='radiological', location=location, severity='moderate', published_at=item.get('captured_at', ''),
            raw=raw))
    return result


def _strip_html(value):
    return re.sub(r'<[^>]+>', ' ', unescape(value or '')).replace('&nbsp;', ' ').strip()


def parse_mastodon(source, data):
    payload = _json(data)
    if not isinstance(payload, list): raise CollectionError('Invalid Mastodon timeline schema')
    result = []
    for item in payload:
        account = item.get('account') or {}
        handle = account.get('acct') or account.get('username', '')
        published = item.get('created_at', '')
        url = item.get('url') or ''
        external = str(item.get('id') or url)
        family = source.source_family or f'mastodon-{source.instance or "instance"}-{(source.hashtag or "tag").lstrip("#")}'
        result.append(Observation(source.id, source.name, 'community', family, external,
            _strip_html(item.get('content', ''))[:240] or 'Mastodon post', _strip_html(item.get('content', '')), url or source.url,
            category=source.category, location=source.area, published_at=published,
            raw={'author': handle, 'published_at': published, 'permalink': url,
                 'reblogs_count': item.get('reblogs_count', 0), 'favourites_count': item.get('favourites_count', 0),
                 '_area': source.area}))
    return result


def _wzdx_state(row):
    return str(row.get('state') or row.get('state_code') or row.get('state_abbreviation') or 'US').upper()


def _wzdx_registry_rows(data):
    payload = _json(data)
    if isinstance(payload, dict):
        rows = payload.get('results') or payload.get('data') or payload.get('feeds') or payload.get('features') or []
    else:
        rows = payload
    result = []
    for item in rows if isinstance(rows, list) else []:
        row = item.get('properties', item) if isinstance(item, dict) else {}
        url = row.get('feed_url') or row.get('feedUrl') or row.get('api_url') or row.get('url') or row.get('feed')
        status = str(row.get('status') or row.get('feed_status') or 'active').lower()
        end = row.get('end_date') or row.get('endDate') or row.get('expiration_date')
        if not url or status in {'inactive', 'dead', 'retired', 'expired', 'removed'}:
            continue
        if end and timestamp(str(end)) and datetime.fromisoformat(iso_utc(str(end)).replace('Z', '+00:00')) <= datetime.now(timezone.utc):
            continue
        result.append((row, str(url)))
    return result


def parse_wzdx_feed(source, data, *, state='US', feed_url=None):
    payload = _json(data)
    features = payload.get('features', []) if isinstance(payload, dict) else []
    observations = []
    for feature in features if isinstance(features, list) else []:
        props = feature.get('properties', {}) or {}
        core = props.get('core_details', {}) or {}
        road_names = core.get('road_names') or props.get('road_names') or props.get('roadName') or []
        if isinstance(road_names, list): road = ', '.join(str(x) for x in road_names)
        else: road = str(road_names or '')
        event_type = core.get('event_type') or props.get('event_type') or props.get('eventType') or 'work zone'
        description = core.get('description') or props.get('description') or props.get('name') or ''
        start = core.get('start_date') or props.get('start_date') or props.get('startDate') or ''
        end = core.get('end_date') or props.get('end_date') or props.get('endDate') or ''
        geometry = feature.get('geometry') or {}
        location = road or json.dumps(geometry, separators=(',', ':'))
        external = str(feature.get('id') or props.get('road_event_id') or props.get('id') or f'{road}|{start}|{event_type}')
        observations.append(Observation(source.id, source.name, 'official', f'wzdx-{state}', external,
            f'{event_type}: {road or "work zone"}', f'{description} Start: {start}; end: {end}.', feed_url or source.url,
            category='transportation', location=location, severity='moderate', published_at=start,
            raw={**feature, '_start': start, '_end': end, '_road': road, '_event_type': event_type, '_area': state}))
    return observations


def parse_nps_alerts(source, data):
    payload = _json(data)
    rows = payload.get('data', []) if isinstance(payload, dict) else []
    result = []
    allowed = {code.lower() for code in source.park_codes}
    for item in rows if isinstance(rows, list) else []:
        park = item.get('parkCode') or item.get('park_code') or ''
        if allowed and park.lower() not in allowed:
            continue
        result.append(Observation(source.id, source.name, 'official', 'nps',
            str(item.get('id') or item.get('url') or item.get('title')), item.get('title') or 'NPS alert',
            item.get('description') or item.get('category') or '', item.get('url') or source.url,
            category='public-safety', location=item.get('parkCode') or item.get('parkName') or '',
            severity=(item.get('category') or 'moderate').lower(), published_at=item.get('lastIndexedDate') or item.get('lastUpdatedDate') or '',
            raw={**item, '_park_code': park, '_area': source.area}))
    return result


def parse_inciweb(source, data):
    rows = parse_rss(source, data)
    terms = [term.lower() for term in source.region_terms]
    if not terms:
        return [Observation(x.source_id, x.source_name, 'official', 'inciweb', x.external_id, x.title, x.body, x.url,
            category='public-safety', location=x.location, severity=x.severity, published_at=x.published_at,
            raw={**x.raw, '_area': source.area}) for x in rows]
    return [Observation(x.source_id, x.source_name, 'official', 'inciweb', x.external_id, x.title, x.body, x.url,
        category='public-safety', location=x.location, severity=x.severity, published_at=x.published_at,
        raw={**x.raw, '_area': source.area}) for x in rows if any(term in (x.title + ' ' + x.body).lower() for term in terms)]


def parse_ipaws_archive(source, data):
    payload = _json(data)
    rows = payload.get('IpawsArchivedAlerts', payload.get('data', [])) if isinstance(payload, dict) else []
    result = []
    for item in rows if isinstance(rows, list) else []:
        title = item.get('info_headline') or item.get('info_event') or item.get('event') or 'Archived IPAWS alert'
        body = item.get('info_description') or item.get('note') or ''
        location = item.get('area_areadesc') or item.get('areaDesc') or item.get('state') or ''
        published = item.get('sent') or item.get('info_effective') or ''
        result.append(Observation(source.id, source.name, 'official', 'ipaws-archive',
            str(item.get('identifier') or item.get('id') or title), title, body, item.get('info_web') or source.url,
            category='public-safety', location=location, severity=(item.get('info_severity') or 'unknown').lower(),
            published_at=published, raw={**item, '_lagging_archive': True, '_area': source.area}))
    return result


def _collect_wzdx_registry(source, *, user_agent, timeout, max_bytes):
    headers = {}
    key = os.environ.get(source.api_key_env, '') if source.api_key_env else ''
    if source.api_key_env and not key:
        raise CollectionError(f'missing credential environment variable: {source.api_key_env}')
    if key: headers['Authorization'] = f'Bearer {key}'
    registry = fetch_bytes(source.url, user_agent=user_agent, timeout=timeout, max_bytes=max_bytes, headers=headers)
    observations = []
    failures = []
    succeeded = 0
    for row, url in _wzdx_registry_rows(registry)[:max(0, source.max_requests_per_poll-1)]:
        try:
            data = fetch_bytes(url, user_agent=user_agent, timeout=timeout, max_bytes=max_bytes, headers=headers)
            state = _wzdx_state(row)
            observations.extend(parse_wzdx_feed(source, data, state=state, feed_url=url))
            succeeded += 1
        except CollectionError as exc:
            failures.append(f'{url}: {exc}')
    if failures and not succeeded:
        raise CollectionError(f'{len(failures)} WZDx child feed(s) failed: '+ '; '.join(failures[:3]))
    if failures:
        raise PartialCollectionError(f'{len(failures)} WZDx child feed(s) failed: '+ '; '.join(failures[:3]), observations)
    return observations


def _mastodon_url(source):
    tag = (source.hashtag or '').lstrip('#')
    if source.url: return source.url
    return f'https://{source.instance}/api/v1/timelines/tag/{tag}?limit=40'


def _collect_mastodon(source, *, user_agent, timeout, max_bytes):
    tags = [source.hashtag.lstrip('#')] if source.hashtag else hashtags(source.keywords)
    all_rows = []
    requests = 0
    for tag in tags:
        if requests >= source.max_requests_per_poll: break
        page_url = source.url if source.hashtag and source.url else f'https://{source.instance}/api/v1/timelines/tag/{tag}?limit=40'
        pages = 0
        while page_url and pages < 3 and requests < source.max_requests_per_poll and len(all_rows) < source.max_posts_per_poll:
            data, headers = fetch_bytes(page_url, user_agent=user_agent, timeout=timeout, max_bytes=max_bytes, return_headers=True)
            all_rows.extend(parse_mastodon(source, data)); pages += 1; requests += 1
            link = next((value for name, value in headers.items() if name.lower() == 'link'), '')
            match = re.search(r'<([^>]+)>;\s*rel="next"', link)
            page_url = match.group(1) if match else ''
    return all_rows[:source.max_posts_per_poll]


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


def _node_js8_placeholder(source, data):
    raise CollectionError('node_js8_forms reads local NODE logs; handled in collect()')


PARSERS = {
    "node_js8_forms": _node_js8_placeholder,
    "commstat_db": _node_js8_placeholder,
    "nws_alerts": parse_nws,
    "usgs_geojson": parse_usgs,
    "swpc_alerts": parse_swpc,
    "rss": parse_rss,
    "cisa_kev": parse_kev,
    "fema_declarations": parse_fema,
    "fema_disaster_declarations": parse_fema,
    "eia_prices": parse_eia_prices,
    "faa_status": parse_faa,
    "status_api": parse_status,
    "tdot_events": parse_tdot,
    "telegram_preview": parse_telegram_preview,
    "acled_api": parse_acled,
    "usgs_waterservices": parse_usgs_waterservices,
    "epa_radnet": parse_radnet,
    "safecast": parse_safecast,
    "mastodon_hashtag": parse_mastodon,
    "google_news_search": parse_rss,
    "wzdx_registry": parse_wzdx_feed,
    "nps_alerts": parse_nps_alerts,
    "inciweb": parse_inciweb,
    "ipaws_archive": parse_ipaws_archive,
    "notam_search": parse_notams,
}


def collect(source: SourceConfig, *, user_agent: str, timeout: int, max_bytes: int,
            conditional: dict | None = None, response_meta: dict | None = None) -> list[Observation]:
    if source.kind == 'node_js8_forms':
        from .node_js8 import collect_node_js8
        return collect_node_js8(source)
    if source.kind == 'commstat_db':
        from .commstat import collect_commstat_db
        return collect_commstat_db(source)
    parser = PARSERS.get(source.kind)
    if not parser:
        raise CollectionError(f"unknown collector kind: {source.kind}")
    if source.kind == 'epa_radnet':
        # The station config is a URL template; use the current UTC year on
        # every request and in the observation's source link.
        current_url = re.sub(r'(/csv/)\d{4}(/fixed/)',
                             lambda match: match.group(1) + str(datetime.now(timezone.utc).year) + match.group(2),
                             source.url, count=1)
        source = replace(source, url=current_url)
    headers = {}
    key = ''
    if source.api_key_env:
        key = os.environ.get(source.api_key_env, '')
        if not key:
            raise CollectionError(f"missing credential environment variable: {source.api_key_env}")
        if source.kind != 'eia_prices':
            headers['Authorization'] = f'Bearer {key}'
    if source.kind == 'mastodon_hashtag':
        return _collect_mastodon(source, user_agent=user_agent, timeout=timeout, max_bytes=max_bytes)
    if source.kind == 'wzdx_registry':
        return _collect_wzdx_registry(source, user_agent=user_agent, timeout=timeout, max_bytes=max_bytes)
    if source.kind == 'nps_alerts':
        key = os.environ.get(source.api_key_env or 'NPS_API_KEY', '')
        if not key:
            raise CollectionError('missing credential environment variable: ' + (source.api_key_env or 'NPS_API_KEY'))
        parsed = urlparse(source.url); query = parse_qs(parsed.query)
        if source.park_codes: query['parkCode'] = [','.join(source.park_codes)]
        url = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
        headers['X-Api-Key'] = key
        return parser(source, fetch_bytes(url, user_agent=user_agent, timeout=timeout, max_bytes=max_bytes, headers=headers))
    if source.kind == 'google_news_search':
        terms = search_queries(source) if source.region_terms else (source.query_terms or source.keywords.get(source.keyword_tier, []))
        if not terms:
            raise CollectionError('google_news_search has no keyword terms')
        observations = []
        failures = []
        for term in terms[:source.max_requests_per_poll]:
            parsed = urlparse(source.url)
            query = parse_qs(parsed.query)
            query['q'] = [term]
            url = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
            try:
                found = parse_rss(source, fetch_bytes(url, user_agent=user_agent, timeout=timeout, max_bytes=max_bytes, headers=headers))
                for observation in found:
                    observation.raw['_discovery_query'] = term
                    observation.raw['_discovery_url'] = url
                observations.extend(found)
                if response_meta is not None:
                    response_meta.setdefault('query_attempts', []).append({'query': term, 'url': url, 'count': len(found), 'error': ''})
            except CollectionError as exc:
                failures.append(f'{term}: {exc}')
                if response_meta is not None:
                    response_meta.setdefault('query_attempts', []).append({'query': term, 'url': url, 'count': 0, 'error': str(exc)})
        if failures:
            raise PartialCollectionError('; '.join(failures), observations)
        return observations
    url = source.url
    if source.kind == 'eia_prices':
        parsed = urlparse(url); query = parse_qs(parsed.query)
        query['api_key'] = [key]
        url = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
    if source.kind == 'usgs_waterservices':
        query = {'format': 'json', 'sites': ','.join(source.sites), 'parameterCd': ','.join(source.parameters)}
        parsed = urlparse(url); current = parse_qs(parsed.query); current.update({k: [v] for k, v in query.items()})
        url = urlunparse(parsed._replace(query=urlencode(current, doseq=True)))
    if source.kind == 'rss' and conditional:
        if conditional.get('etag'): headers['If-None-Match'] = conditional['etag']
        if conditional.get('last_modified'): headers['If-Modified-Since'] = conditional['last_modified']
    try:
        if source.kind == 'rss':
            data, received = fetch_bytes(url,user_agent=user_agent,timeout=timeout,max_bytes=max_bytes,headers=headers,return_headers=True)
            if response_meta is not None:
                normalized={name.lower(): value for name, value in received.items()}
                response_meta.update(etag=normalized.get('etag',''), last_modified=normalized.get('last-modified',''))
        else:
            data=fetch_bytes(url,user_agent=user_agent,timeout=timeout,max_bytes=max_bytes,headers=headers)
    except NotModified:
        if response_meta is not None: response_meta['not_modified'] = True
        return []
    try:
        return parser(source,data)
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError) as exc:
        raise CollectionError('Feed schema does not match '+source.kind) from exc
