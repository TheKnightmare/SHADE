"""SHADE browser console (Phase 1: Inbox). Disposable databases, loopback only."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from shade_node.db import connect, ingest
from shade_node.desktop import DesktopService
from shade_node.model import Observation
from shade_node.web import Console, make_server, render_page


def obs(i, title, source_type='official', hours=1):
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace('+00:00', 'Z')
    return Observation(f'src{i}', f'Source {i}', source_type, f'fam{i}', f'e{i}', title, 'Service outage confirmed',
                       f'https://example.test/{i}', category='infrastructure', location='Knoxville TN', severity='severe',
                       published_at=stamp, observed_at=stamp)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        cfg = Path(self.temp.name) / 'config.toml'
        cfg.write_text('[operator]\ncallsign="TEST"\n')
        self.service = DesktopService(cfg)
        with connect(self.service.settings.database) as db:
            self.ids = [ingest(db, obs(i, f'Major outage {i} in Knoxville', hours=i))[0] for i in range(1, 6)]
        self.console = Console(self.service)


class ConsoleTests(Base):
    def test_inbox_paging_and_sort(self):
        page = self.console.inbox({'size': ['50']})
        self.assertEqual(page['total'], 5)
        self.assertEqual({r['id'] for r in page['rows']}, set(self.ids))
        by_id = self.console.inbox({'sort': ['id'], 'desc': ['0']})
        self.assertEqual([r['id'] for r in by_id['rows']], sorted(self.ids))
        self.assertEqual(self.console.inbox({'page': ['99']})['page'], 1)       # clamped
        self.assertEqual(self.console.inbox({'size': ['7']})['size'], 100)      # only allowed sizes

    def test_cache_reused_until_data_changes(self):
        self.assertFalse(self.console.inbox({})['cached'])
        self.assertTrue(self.console.inbox({})['cached'])
        self.service.mark(self.ids[0], 'REVIEW')
        page = self.console.inbox({})
        self.assertFalse(page['cached'])
        self.assertEqual(next(r for r in page['rows'] if r['id'] == self.ids[0])['status'], 'REVIEW')

    def test_cached_views_after_actions_equal_full_recompute(self):
        views = [{}, {'lane': ['All evidence']}, {'state': ['REVIEW']}, {'state': ['REJECTED']}, {'broad': ['1']}, {'q': ['outage 2']}]
        for v in views:
            self.console.inbox(v)
        self.console.mark({'ids': self.ids[:2], 'target': 'REVIEW'})
        self.console.mark({'ids': [self.ids[2]], 'target': 'REJECTED'})
        self.console.mark({'ids': [self.ids[2]], 'target': 'REVIEW'})      # re-enters views it left
        for v in views:
            got = [(r['id'], r['status']) for r in self.console.inbox(v)['rows']]
            want = [(r['id'], r['status']) for r in Console(self.service).inbox(v)['rows']]
            self.assertEqual(got, want, v)

    def test_detail_and_review_actions(self):
        d = self.console.detail(self.ids[0], 'standard')
        self.assertEqual({a['target'] for a in d['actions']}, {'REVIEW', 'CORRELATING', 'TX_CANDIDATE', 'REJECTED'})
        self.assertTrue(d['links'][0]['url'].startswith('https://example.test/'))
        self.console.mark({'ids': [self.ids[0]], 'target': 'REVIEW'})
        self.assertEqual(self.service.detail(self.ids[0])[0]['status'], 'REVIEW')
        self.console.mark({'ids': self.ids[1:3], 'target': 'REJECTED'})
        self.assertEqual({self.service.detail(i)[0]['status'] for i in self.ids[1:3]}, {'REJECTED'})

    def test_sent_and_unknown_actions_refused(self):
        for target in ('SENT', 'EXPIRED', 'whatever'):
            with self.assertRaises(ValueError):
                self.console.mark({'ids': [self.ids[0]], 'target': target})
        with self.assertRaises(ValueError):
            self.console.mark({'ids': [], 'target': 'REVIEW'})

    def test_tx_candidate_still_requires_corroboration(self):
        with connect(self.service.settings.database) as db:
            weak, _ = ingest(db, obs(9, 'Rumor of outage', source_type='community'))
        with self.assertRaises(ValueError):
            self.console.mark({'ids': [weak, self.ids[0]], 'target': 'TX_CANDIDATE'})

    def test_collect_runs_in_background(self):
        release = threading.Event()
        def slow(mode):
            release.wait(5)
            return {'sources_ok': 2, 'sources_partial': 0, 'sources_failed': 0, 'inserted': 3,
                    'sources_skipped_cooldown': 1, 'errors': []}
        self.service.collect = slow
        self.assertTrue(self.console.start_collect('standard')['started'])
        self.assertFalse(self.console.start_collect('standard')['started'])     # one at a time
        self.assertTrue(self.console.status()['collecting'])
        self.assertEqual(self.console.inbox({})['total'], 5)                      # reading still works
        release.set()
        for _ in range(100):
            if not self.console.status()['collecting']:
                break
            time.sleep(0.05)
        self.assertIn('3 new observations', self.console.status()['last_collect']['summary'])

    def test_page_has_no_outside_resources(self):
        page = render_page('/s/t/api/', 'NONCE', 'TEST')
        self.assertNotRegex(page, r'(src|href)="https?://')
        self.assertIn('<script nonce="NONCE">', page)
        self.assertNotIn('@import', page)


class Phase2Tests(Base):
    def test_relay_requires_confirm_and_review_state(self):
        with connect(self.service.settings.database) as db:
            weak, _ = ingest(db, obs(9, 'Rumor of outage', source_type='community'))
        with self.assertRaises(ValueError):
            self.console.relay({'id': weak})                       # no confirmation
        with self.assertRaises(ValueError):
            self.console.relay({'id': weak, 'confirm': True})      # still NEW, not REVIEW
        self.console.mark({'ids': [weak], 'target': 'REVIEW'})
        self.console.relay({'id': weak, 'confirm': True})
        claim = self.service.detail(weak)[0]
        self.assertEqual((claim['status'], claim['tx_candidate_basis']), ('TX_CANDIDATE', 'operator_relay'))

    def test_format_needs_review_and_actual_confirmation(self):
        with self.assertRaises(ValueError):
            self.console.format_message({'id': self.ids[0]})       # NEW: review first
        self.console.mark({'ids': [self.ids[0]], 'target': 'REVIEW'})
        text = self.console.format_message({'id': self.ids[0]})['text']
        self.assertIn('MAJOR OUTAGE', text.upper())
        with self.assertRaises(ValueError):
            self.console.format_message({'id': self.ids[0], 'mode': 'actual'})
        self.assertIn('ACTUAL', self.console.format_message({'id': self.ids[0], 'mode': 'actual', 'confirm_actual': True})['text'])

    def test_record_sent_needs_confirmation_and_text(self):
        self.console.mark({'ids': [self.ids[0]], 'target': 'REVIEW'})
        self.console.mark({'ids': [self.ids[0]], 'target': 'TX_CANDIDATE'})
        base = {'id': self.ids[0], 'sent_at': '2026-10-09T20:00:00-04:00', 'channel': 'JS8', 'text': 'GHOSTNET test'}
        with self.assertRaises(ValueError):
            self.console.record_sent(base)
        with self.assertRaises(ValueError):
            self.console.record_sent(dict(base, text='  ', confirm=True))
        self.console.record_sent(dict(base, confirm=True))
        self.assertEqual(self.service.detail(self.ids[0])[0]['status'], 'SENT')

    def test_merge_needs_confirmation_and_keep_in_selection(self):
        with self.assertRaises(ValueError):
            self.console.merge({'keep': self.ids[0], 'ids': self.ids[:2]})
        with self.assertRaises(ValueError):
            self.console.merge({'keep': 999, 'ids': self.ids[:2], 'confirm': True})
        self.console.merge({'keep': self.ids[0], 'ids': self.ids[:3], 'confirm': True})
        page = self.console.inbox({})
        self.assertNotIn(self.ids[1], [r['id'] for r in page['rows']])

    def test_only_previewed_articles_can_be_attached(self):
        article = {'url': 'https://news.example/outage', 'title': 'Major outage 1 in Knoxville', 'publisher': 'Example News',
                   'published_at': '2026-10-09T20:00:00Z', 'body': 'Outage confirmed.'}
        self.service.preview_article = lambda url: dict(article, url=url)
        attached = []
        self.service.attach_article = lambda claim_id, art, mode='standard': attached.append((claim_id, art['url']))
        with self.assertRaises(ValueError):
            self.console.article_attach({'id': self.ids[0], 'token': 'forged', 'confirm': True})
        with self.assertRaises(ValueError):
            self.console.article_preview({'url': 'javascript:alert(1)'})
        token = self.console.article_preview({'url': 'https://news.example/outage'})['token']
        with self.assertRaises(ValueError):
            self.console.article_attach({'id': self.ids[0], 'token': token})        # no confirmation
        token = self.console.article_preview({'url': 'https://news.example/outage'})['token']
        self.console.article_attach({'id': self.ids[0], 'token': token, 'confirm': True})
        self.assertEqual(attached, [(self.ids[0], 'https://news.example/outage')])
        with self.assertRaises(ValueError):                                            # tokens are single-use
            self.console.article_attach({'id': self.ids[0], 'token': token, 'confirm': True})


class Phase3Tests(Base):
    def test_sources_listing(self):
        d = self.console.sources('standard')
        self.assertEqual(d['counts']['total'], len(d['sources']))
        self.assertIn('does not prove current coverage', d['note'])

    def test_purge_needs_matching_preview_and_confirmation_and_keeps_tx(self):
        with connect(self.service.settings.database) as db:
            old, _ = ingest(db, obs(20, 'Old outage', hours=80))
            old_tx, _ = ingest(db, obs(21, 'Old outage relayed', hours=80))
        self.service.mark(old_tx, 'REVIEW'); self.service.mark(old_tx, 'TX_CANDIDATE')
        preview = self.console.housekeeping_preview({'hours': 72})
        self.assertEqual(preview['purge']['eligible'], 1)
        with self.assertRaises(ValueError):
            self.console.purge_apply({'token': preview['token']})                    # no confirmation
        preview = self.console.housekeeping_preview({'hours': 72})
        with self.assertRaises(ValueError):
            self.console.purge_apply({'token': 'forged', 'confirm': True})
        stale = preview['token']
        fresh = self.console.housekeeping_preview({'hours': 72})['token']          # newer preview wins
        with self.assertRaises(ValueError):
            self.console.purge_apply({'token': stale, 'confirm': True})
        result = self.console.purge_apply({'token': fresh, 'confirm': True})
        self.assertEqual(result['deleted']['claims'], 1)
        self.assertTrue(Path(result['backup']).exists())
        with connect(self.service.settings.database) as db:
            ids = {r[0] for r in db.execute('SELECT id FROM claims')}
        self.assertNotIn(old, ids); self.assertIn(old_tx, ids)

    def test_housekeeping_apply(self):
        preview = self.console.housekeeping_preview({'hours': 72})
        result = self.console.housekeeping_apply({'token': preview['token'], 'confirm': True})
        self.assertEqual(result['changed'], preview['changes_total'])
        with self.assertRaises(ValueError):                                          # single use
            self.console.housekeeping_apply({'token': preview['token'], 'confirm': True})

    def test_hours_out_of_range_refused(self):
        with self.assertRaises(ValueError):
            self.console.housekeeping_preview({'hours': 100})


class HttpTests(Base):
    def setUp(self):
        super().setUp()
        self.server, self.url = make_server(self.console)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        parts = urlsplit(self.url)
        self.port, self.base = parts.port, parts.path

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        h = {'Host': f'127.0.0.1:{self.port}'}
        if body is not None:
            h['Content-Type'] = 'application/json'
        h.update(headers or {})
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = conn.getresponse(); data = r.read(); conn.close()
        return r.status, data, dict(r.getheaders())

    def test_page_and_api(self):
        status, data, headers = self.req('GET', self.base)
        self.assertEqual(status, 200)
        self.assertIn(b'SHADE', data)
        self.assertIn("script-src 'nonce-", headers['Content-Security-Policy'])
        status, data, _ = self.req('GET', self.base + 'api/inbox')
        self.assertEqual(json.loads(data)['total'], 5)
        status, data, _ = self.req('POST', self.base + 'api/mark', {'ids': [self.ids[0]], 'target': 'REVIEW'})
        self.assertEqual(status, 200)

    def test_wrong_token_or_host_or_origin_refused(self):
        self.assertEqual(self.req('GET', '/s/wrong/api/inbox')[0], 404)
        self.assertEqual(self.req('GET', self.base, headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.req('POST', self.base + 'api/mark', {'ids': [self.ids[0]], 'target': 'REVIEW'},
                                  headers={'Origin': 'http://evil.example'})[0], 403)
        status, _, _ = self.req('POST', self.base + 'api/mark', None, headers={'Content-Type': 'text/plain'})
        self.assertEqual(status, 415)
        self.assertEqual(self.service.detail(self.ids[0])[0]['status'], 'NEW')   # nothing changed

    def test_bound_to_loopback_only(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')


class ColumnFilterTests(unittest.TestCase):
    def test_facets_count_with_other_filters_and_filtering_is_multi_value(self):
        from shade_node.web import column_filters
        rows = [{'area': 'REGIONAL', 'category': 'regional', 'confidence_label': 'REPORTED', 'status': 'NEW'},
                {'area': 'ETN/WNC', 'category': 'weather', 'confidence_label': 'OFFICIAL-REPORT', 'status': 'NEW'},
                {'area': 'GLOBAL', 'category': 'regional', 'confidence_label': 'REPORTED', 'status': 'TX_CANDIDATE'},
                {'area': 'REGIONAL', 'category': 'weather', 'confidence_label': 'REPORTED', 'status': 'REVIEW'}]
        facets, out = column_filters(rows, {'fa': ['REGIONAL\x1fETN/WNC']})
        self.assertEqual(len(out), 3)
        self.assertEqual(dict(facets['fa']), {'REGIONAL': 2, 'ETN/WNC': 1, 'GLOBAL': 1})   # own column ignores itself
        self.assertEqual(dict(facets['ft']), {'REGIONAL': 1, 'WEATHER': 2})
        facets, out = column_filters(rows, {'fa': ['REGIONAL'], 'ft': ['WEATHER']})
        self.assertEqual(len(out), 1)
        self.assertEqual(column_filters(rows, {})[1], rows)


if __name__ == '__main__':
    unittest.main()


class FightMountTests(Base):
    """FIGHT hosted in the SHADE console, with a stub FIGHT session."""
    class Session:
        def __init__(self):
            self.calls = []
        def home_page(self):
            return ('<!doctype html><html><head><title>FIGHT</title></head><body><header class="osint-bar">x</header><main>'
                    '<p><a href="/s/__TOKEN__">All lists</a></p><form method="post" action="__ACTION__">'
                    '<input name="action" value="create_list"></form><script>1</script></main></body></html>')
        def dispatch(self, fields):
            self.calls.append(fields)
            if fields.get('action') == 'boom':
                raise ValueError('refused <now>')
            return self.home_page()

    def setUp(self):
        super().setUp()
        from shade_node.fight_mount import FightMount
        self.session = self.Session()
        self.console.fight = FightMount(self.service, 'unused.toml', session=self.session)
        self.server, self.url = make_server(self.console)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        parts = urlsplit(self.url); self.port, self.base = parts.port, parts.path

    def post(self, body, headers):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        h = {'Host': f'127.0.0.1:{self.port}'}; h.update(headers)
        conn.request('POST', self.base + 'fight/action', body=body, headers=h)
        r = conn.getresponse(); data = r.read(); conn.close(); return r.status, data

    def test_fight_page_routed_under_shade(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        conn.request('GET', self.base + 'fight', headers={'Host': f'127.0.0.1:{self.port}'})
        r = conn.getresponse(); page = r.read().decode(); conn.close()
        self.assertEqual(r.status, 200)
        self.assertIn(f'action="{self.base}fight/action"', page)
        self.assertIn(f'href="{self.base}fight"', page)
        self.assertNotIn('__TOKEN__', page); self.assertNotIn('__ACTION__', page)
        self.assertIn('class="osint-tabs"', page)
        self.assertIn('id="shade-quit"', page)                       # Quit on every tab, FIGHT too
        self.assertIn(f'fetch("{self.base}api/quit"', page)
        self.assertNotRegex(page, r'<script>')                         # every script carries the nonce
        self.assertRegex(page, r'<script nonce="[^"]+">')
        self.assertIn("form-action 'self'", r.getheader('Content-Security-Policy'))

    def test_form_posts_checked_like_fight(self):
        form = {'Content-Type': 'application/x-www-form-urlencoded'}
        origin = {'Origin': f'http://127.0.0.1:{self.port}'}
        self.assertEqual(self.post(b'action=create_list&name=x', {**form, **origin})[0], 200)
        self.assertEqual(self.session.calls[-1], {'action': 'create_list', 'name': 'x'})
        self.assertEqual(self.post(b'action=x', {**form, 'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.post(b'action=x', form)[0], 403)                          # no Origin, no Referer
        self.assertEqual(self.post(b'action=x', {**form, 'Referer': f'http://127.0.0.1:{self.port}{self.base}fight'})[0], 200)
        self.assertEqual(self.post(b'action=x', {**origin, 'Content-Type': 'text/plain'})[0], 415)
        status, data = self.post(b'action=a&action=b', {**form, **origin})                # duplicate fields
        self.assertEqual(status, 400); self.assertIn(b'Nothing was sent', data)
        status, data = self.post(b'action=boom', {**form, **origin})
        self.assertEqual(status, 400); self.assertIn(b'refused &lt;now&gt;', data)
        self.post(b'bluf_choice=a&bluf_choice=b&action=build_bluf', {**form, **origin})
        self.assertEqual(self.session.calls[-1]['bluf_choice'], ['a', 'b'])

    def test_missing_fight_is_explained(self):
        from shade_node.fight_mount import FightMount
        self.console.fight = FightMount(self.service, '/nowhere/config.toml', fight_dir=Path('/nowhere'))
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        conn.request('GET', self.base + 'fight', headers={'Host': f'127.0.0.1:{self.port}'})
        page = conn.getresponse().read().decode(); conn.close()
        self.assertIn('FIGHT is unavailable', page)


class WireWarningsTests(Base):
    def test_inbox_candidates_carry_daily_wire_warnings(self):
        from shade_node.candidate_snapshot import list_candidates
        self.service.mark(self.ids[0], 'REVIEW'); self.service.mark(self.ids[0], 'TX_CANDIDATE')
        result = list_candidates(self.service.settings.database, callsign='TEST', network='GHOSTNET',
                                 policy=self.service.policy('standard'))
        self.assertEqual([c['claim_id'] for c in result['candidates']], [self.ids[0]])
        self.assertIsInstance(result['candidates'][0]['wire_warnings'], list)


class OperatorFeedbackFixes(Base):
    """Fixes from the operator's 2026-10-09 evening session."""
    def test_bulk_tx_candidate_skips_ones_already_tx(self):
        for i in self.ids[:2]:
            self.service.mark(i, 'REVIEW'); self.service.mark(i, 'TX_CANDIDATE')
        r = self.console.mark({'ids': self.ids[:4], 'target': 'TX_CANDIDATE'})
        self.assertEqual((r['changed'], r['skipped']), (2, 2))
        self.assertEqual({self.service.detail(i)[0]['status'] for i in self.ids[:4]}, {'TX_CANDIDATE'})
        self.assertEqual(self.console.mark({'ids': [self.ids[0]], 'target': 'TX_CANDIDATE'})['skipped'], 1)

    def test_group_different_headlines_needs_override(self):
        with connect(self.service.settings.database) as db:
            a, _ = ingest(db, obs(30, 'Sheriff gives new details after homecoming shooting'))
            b, _ = ingest(db, obs(31, 'Two dead dozens injured at block party'))
        with self.assertRaisesRegex(ValueError, 'too different'):
            self.console.merge({'keep': a, 'ids': [a, b], 'confirm': True})
        self.console.merge({'keep': a, 'ids': [a, b], 'confirm': True, 'override': True})
        self.assertEqual(self.service.detail(b)[0]['status'], 'MERGED')
        with connect(self.service.settings.database) as db:
            reason = db.execute("SELECT reason FROM workflow_audit WHERE claim_id=? AND new_status='MERGED'", (b,)).fetchone()[0]
        self.assertIn('operator override', reason)

    def test_housekeeping_apply_tolerates_changes_since_preview(self):
        with connect(self.service.settings.database) as db:
            gone = ingest(db, obs(40, 'Warning ended', hours=1))[0]
        fake = [{'id': gone, 'status': 'EXPIRED', 'reason': 'test'}]
        token = 'tok'
        self.console.pending[token] = {'mode': 'standard', 'changes': fake, 'plan': {'claim_ids': []}, 'used': set()}
        # The report is not actually expired now: the preview no longer holds,
        # so it is skipped instead of the whole apply failing.
        r = self.console.housekeeping_apply({'token': token, 'confirm': True})
        self.assertEqual((r['changed'], r['skipped']), (0, 1))
        self.assertEqual(self.service.detail(gone)[0]['status'], 'NEW')

    def test_same_preview_can_run_both_cleanups_once_each(self):
        p = self.console.housekeeping_preview({'hours': 72})
        self.console.housekeeping_apply({'token': p['token'], 'confirm': True})
        with self.assertRaisesRegex(ValueError, 'already applied'):
            self.console.housekeeping_apply({'token': p['token'], 'confirm': True})
