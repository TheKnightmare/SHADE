import tempfile
import tomllib
import unittest
from pathlib import Path

from shade_node import local_config
from shade_node.desktop import DesktopService
from shade_node.engine import load_settings
from shade_node.web import Console

LOCAL = ('# my tuning\r\n[relevance]\r\nminimum_score = 3\r\n\r\n[source_packs]\r\n'
         'enabled = ["cyber", "top-news"]\r\n\r\n[[sources]]\r\nid = "mine"\r\nkind = "rss"\r\nname = "Mine"\r\n'
         'url = "https://example.org/feed"\r\nsource_type = "media"\r\nsource_family = "mine"\r\n')


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.cfg = Path(self.temp.name) / 'config.toml'
        self.cfg.write_text('[operator]\ncallsign="TEST"\n')
        self.local = self.cfg.with_name('config.local.toml')
        self.local.write_bytes(LOCAL.encode())

    def test_write_keeps_everything_else_and_crlf(self):
        local_config.write(self.cfg, disabled={'mine'}, enabled=set(), packs=['cyber'])
        raw = self.local.read_bytes().decode()
        self.assertIn('# my tuning', raw)
        self.assertNotIn('\n', raw.replace('\r\n', ''))                # still CRLF throughout
        data = tomllib.loads(raw)
        self.assertEqual(data['source_packs']['enabled'], ['cyber'])
        self.assertEqual(data['source_overrides']['disabled'], ['mine'])
        self.assertEqual(data['relevance']['minimum_score'], 3)
        self.assertTrue(self.local.with_name('config.local.toml.web-backup').exists())
        # second write replaces the managed block rather than adding another
        local_config.write(self.cfg, disabled=set(), enabled={'mine'})
        raw = self.local.read_text()
        self.assertEqual(raw.count(local_config.BEGIN), 1)
        self.assertEqual(tomllib.loads(raw)['source_packs']['enabled'], ['cyber'])

    def test_engine_applies_overrides(self):
        local_config.write(self.cfg, disabled={'mine'}, enabled=set())
        self.assertFalse(next(s for s in load_settings(self.cfg).sources if s.id == 'mine').enabled)
        local_config.write(self.cfg, disabled=set(), enabled={'mine'})
        self.assertTrue(next(s for s in load_settings(self.cfg).sources if s.id == 'mine').enabled)

    def test_console_toggle_and_packs(self):
        console = Console(DesktopService(self.cfg))
        listing = console.sources("standard")
        self.assertTrue(next(r for r in listing['sources'] if r['id'] == 'mine')['enabled'])
        self.assertEqual({p['name'] for p in listing['packs'] if p['on']}, {'cyber', 'top-news'})
        console.source_toggle({'id': 'mine', 'on': False})
        self.assertFalse(next(r for r in console.sources("standard")['sources'] if r['id'] == 'mine')['enabled'])
        console.source_packs({'packs': ['cyber']})
        self.assertEqual({p['name'] for p in console.sources("standard")['packs'] if p['on']}, {'cyber'})
        with self.assertRaises(ValueError):
            console.source_toggle({'id': 'nope', 'on': True})
        with self.assertRaises(ValueError):
            console.source_packs({'packs': ['made-up']})
        self.assertFalse(next(r for r in console.sources("standard")['sources'] if r['id'] == 'mine')['enabled'])


NODE_CFG = {"_comment": "x", "my_call": "TEST", "varac_source": {"enabled": True, "db_path": "C:/VarAC/VarAC.db"},
            "sources": {"js8_all": "C:/js8/ALL.TXT"},
            "http_sources": {"xlx237": {"url": "http://xlx237.example/", "format": "xlx"}},
            "udp_sources": {"aprs": {"bind": "127.0.0.1", "port": 9999}}}


class NodeSwitchTests(unittest.TestCase):
    def setUp(self):
        import json
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name) / 'Apps' / 'StationWatch'; self.dir.mkdir(parents=True)
        self.path = self.dir / 'station-watch-config.json'
        self.path.write_bytes(json.dumps(NODE_CFG, indent=2).replace('\n', '\r\n').encode())

    def test_listing_toggle_and_backup(self):
        import json
        from shade_node.node_settings import NodeSettings
        node = NodeSettings(self.dir)
        rows = {r['key']: r for r in node.listing()['rows']}
        self.assertEqual(set(rows), {'varac_source', 'sources:js8_all', 'http_sources:xlx237', 'udp_sources:aprs'})
        self.assertTrue(all(r['on'] for r in rows.values()))
        self.assertEqual(rows['udp_sources:aprs']['detail'], '127.0.0.1:9999')
        node.set('http_sources:xlx237', False)
        node.set('varac_source', False)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['disabled_sources'], ['http_sources:xlx237', 'varac_source'])
        self.assertEqual(saved['http_sources'], NODE_CFG['http_sources'])        # settings kept intact
        self.assertEqual(list(saved)[:3], ['_comment', 'my_call', 'varac_source'])  # order kept
        self.assertIn(b'\r\n', self.path.read_bytes())
        self.assertTrue(self.path.with_name('station-watch-config.json.web-backup').exists())
        self.assertFalse({r['key']: r for r in node.listing()['rows']}['http_sources:xlx237']['on'])
        node.set('http_sources:xlx237', True)
        self.assertEqual(json.loads(self.path.read_text())['disabled_sources'], ['varac_source'])
        with self.assertRaises(ValueError):
            node.set('http_sources:nope', False)

    def test_console_node_routes(self):
        cfg = Path(self.temp.name) / 'SHADE' / 'config.toml'; cfg.parent.mkdir()
        cfg.write_text('[operator]\ncallsign="TEST"\n')
        console = Console(DesktopService(cfg))
        self.assertEqual(len(console.node()['rows']), 4)
        console.node_toggle({'key': 'sources:js8_all', 'on': False})
        self.assertFalse(next(r for r in console.node()['rows'] if r['key'] == 'sources:js8_all')['on'])


if __name__ == '__main__':
    unittest.main()


class MyFeedTests(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        from shade_node.model import Observation
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.cfg = Path(self.temp.name) / 'config.toml'
        self.cfg.write_text('[operator]\ncallsign="TEST"\n')
        self.cfg.with_name('config.local.toml').write_bytes(LOCAL.encode())
        self.service = DesktopService(self.cfg)
        fake = mock.Mock(title='Bridge closed on I-40')
        patch = mock.patch('shade_node.collectors.collect', return_value=[fake, fake])
        self.collect = patch.start(); self.addCleanup(patch.stop)

    def test_add_and_remove_feed(self):
        console = Console(self.service)
        self.assertEqual(console.feed_check({'url': 'https://www.wbir.com/feeds/news.xml'})['items'], 2)
        feed = console.feed_add({'name': 'WBIR News', 'url': 'https://www.wbir.com/feeds/news.xml', 'area': 'ETN/WNC',
                                 'category': 'regional', 'source_type': 'media'})
        self.assertEqual(feed['id'], 'my-wbir-news')
        self.assertEqual(feed['source_family'], 'wbir.com')
        row = next(r for r in console.sources('standard')['sources'] if r['id'] == 'my-wbir-news')
        self.assertTrue(row['mine'] and row['enabled'])
        raw = self.cfg.with_name('config.local.toml').read_text()
        self.assertIn('# my tuning', raw)                       # owner's own lines kept
        self.assertEqual(tomllib.loads(raw)['source_packs']['enabled'], ['cyber', 'top-news'])
        # switches and feeds blocks coexist
        console.source_toggle({'id': 'my-wbir-news', 'on': False})
        self.assertFalse(next(r for r in console.sources('standard')['sources'] if r['id'] == 'my-wbir-news')['enabled'])
        with self.assertRaises(ValueError):                     # duplicate
            console.feed_add({'name': 'Again', 'url': 'https://www.wbir.com/feeds/news.xml'})
        with self.assertRaises(ValueError):
            console.feed_add({'name': 'Plain', 'url': 'http://example.com/rss'})
        with self.assertRaises(ValueError):                     # the owner's hand-written source is not removable here
            console.feed_remove({'id': 'mine'})
        console.feed_remove({'id': 'my-wbir-news'})
        self.assertNotIn('my-wbir-news', {r['id'] for r in console.sources('standard')['sources']})

    def test_unreachable_feed_is_a_plain_error(self):
        self.collect.side_effect = OSError('timed out')
        with self.assertRaisesRegex(ValueError, 'Could not read that feed'):
            Console(self.service).feed_check({'url': 'https://nowhere.example/rss'})
