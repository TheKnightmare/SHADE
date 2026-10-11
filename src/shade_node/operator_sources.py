"""Operator-added article evidence. Fetch and preview before attaching to a claim."""
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from datetime import datetime, timezone
import ipaddress
import json
import re
import socket

from .model import Observation, token_set, iso_utc, utc_now
from .db import recompute_claim


def _public_https(url):
    parsed = urlsplit(url.strip())
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Enter a public HTTPS article URL.')
    host = parsed.hostname
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f'Cannot resolve article host: {exc}') from exc
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('Article URL must resolve only to public internet addresses.')
    return urlunsplit(('https', parsed.netloc, parsed.path or '/', parsed.query, ''))


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = {}
        self.in_title = False
        self.title = ''

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'meta':
            key = (values.get('property') or values.get('name') or '').lower()
            if key:
                self.meta[key] = values.get('content', '').strip()
        elif tag == 'title':
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


def preview_article(url, timeout=12):
    url = _public_https(url)
    opener = build_opener(_SafeRedirect())
    request = Request(url, headers={'User-Agent': 'SHADE operator article review/1.0', 'Accept': 'text/html'})
    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = _public_https(response.url)
            if 'text/html' not in response.headers.get('Content-Type', '').lower():
                raise ValueError('The URL did not return an HTML article.')
            payload = response.read(1024 * 1024 + 1)
            if len(payload) > 1024 * 1024:
                raise ValueError('Article preview exceeded the 1 MB limit.')
            html = payload.decode(response.headers.get_content_charset() or 'utf-8', errors='replace')
    except (HTTPError, URLError, TimeoutError) as exc:
        raise ValueError(f'Could not open the article: {exc}') from exc
    parser = _ArticleParser()
    parser.feed(html)
    title = re.sub(r'\s+', ' ', parser.meta.get('og:title') or parser.meta.get('twitter:title') or parser.title).strip()
    if len(title) < 15:
        raise ValueError('The page did not supply an article headline.')
    publisher = parser.meta.get('og:site_name') or urlsplit(final_url).hostname
    description = re.sub(r'\s+', ' ', parser.meta.get('og:description') or parser.meta.get('description') or '').strip()
    published = parser.meta.get('article:published_time') or parser.meta.get('datepublished') or ''
    if published:
        try:
            published = iso_utc(datetime.fromisoformat(published.replace('Z', '+00:00')))
        except ValueError:
            published = ''
    return dict(url=final_url, title=title, publisher=publisher, description=description,
                published_at=published, family=urlsplit(final_url).hostname.lower())


def attach_article(db, claim_id, article):
    claim = db.execute('SELECT * FROM claims WHERE id=?', (claim_id,)).fetchone()
    if not claim:
        raise ValueError('Report no longer exists.')
    if claim['status'] in {'SENT', 'EXPIRED'}:
        raise ValueError('Cannot add review evidence to a sent or expired report.')
    url = _public_https(article['url'])
    family = urlsplit(url).hostname.lower()
    title = article['title'].strip()
    if not title or not token_set(title):
        raise ValueError('Article headline is missing.')
    existing = db.execute('SELECT claim_id FROM observations WHERE source_id=? AND external_id=?',
                          ('operator-article', url)).fetchone()
    if existing:
        if existing['claim_id'] == claim_id:
            return False
        raise ValueError(f'This article is already attached to report {existing["claim_id"]}.')
    observed = iso_utc(utc_now())
    observation = Observation('operator-article', article['publisher'], 'media', family, url,
                              title, article.get('description', ''), url,
                              category=claim['category'], location=claim['location'],
                              published_at=article.get('published_at') or observed, observed_at=observed,
                              raw={'_operator_added_article': True, '_article_preview': article,
                                   '_publication_time_unavailable': not bool(article.get('published_at'))})
    db.execute(
        """INSERT INTO observations(claim_id,source_id,source_name,source_type,source_family,external_id,
        content_hash,title,body,url,category,location,severity,published_at,observed_at,raw_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (claim_id, observation.source_id, observation.source_name, observation.source_type,
         observation.source_family, observation.external_id, observation.content_hash,
         observation.title, observation.body, observation.url, observation.category,
         observation.location, observation.severity, observation.published_at,
         observation.observed_at, observation.raw_json()))
    db.execute('UPDATE claims SET last_seen=? WHERE id=?', (observed, claim_id))
    recompute_claim(db, claim_id)
    db.execute('INSERT INTO workflow_audit(claim_id,previous_status,new_status,reason,changed_at) VALUES(?,?,?,?,?)',
               (claim_id, claim['status'], claim['status'], f'Operator attached reviewed article: {url}', observed))
    return True
