"""Deterministic v0.3 acceptance fixtures; no live Internet dependency."""
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr, closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
from shade_node.collectors import parse_nws, parse_kev, parse_status, parse_fema, parse_rss, parse_telegram_preview, parse_acled, parse_usgs_waterservices, parse_radnet, parse_safecast, parse_mastodon, parse_wzdx_feed, parse_nps_alerts, parse_inciweb, parse_ipaws_archive, fetch_bytes, CollectionError
from shade_node.db import connect, ingest, queue, claim_detail, housekeeping, transition
from shade_node.model import Observation
from shade_node.relevance import age, remaining, evaluate, DEFAULT_POLICY
from shade_node.cli import main, parser
from shade_node.engine import load_settings, run_once
from shade_node.formatters import traffic_message, evidence_summary, split_message, bulletin_messages

NOW=datetime(2026,9,22,3,tzinfo=timezone.utc)

def obs(**kwargs):
    base=Observation('fixture','Fixture','official','agency','event-1','Major communications outage','Service outage confirmed','https://example.test/1',category='infrastructure',location='Knoxville TN',severity='severe',published_at='2026-09-22T02:00:00Z',observed_at='2026-09-22T02:10:00Z')
    return replace(base,**kwargs)

def weather(event='Tornado Warning', location='Knox, TN', expires='2026-09-22T04:00:00Z', **props):
    return obs(category='weather',location=location,title=event,raw={'geometry':None,'properties':{'event':event,'areaDesc':location,'sent':'2026-09-22T02:00:00Z','expires':expires,'effective':'2026-09-22T02:00:00Z','urgency':'Immediate','certainty':'Observed',**props}})

def quake(mag=6.4,coords=(146,-6),**props):
    return obs(category='earthquake',location='Remote area',title='Earthquake',raw={'geometry':{'coordinates':list(coords)},'properties':{'mag':mag,'sig':650,'felt':8,'alert':'green','tsunami':0,**props}})

class AcceptanceTests(unittest.TestCase):
    def visible(self,o,lane='inbox',**kwargs):
        with connect(':memory:') as db:
            ingest(db,o)
            return queue(db,lane=lane,now=NOW,**kwargs)

    def test_expired_weather_absent_both_lanes(self):
        o=weather(expires='2026-09-22T02:30:00Z')
        self.assertEqual(self.visible(o),[]);self.assertEqual(self.visible(o,'now'),[])
        self.assertEqual(self.visible(o,'now',include_suppressed=True),[])

    def test_active_local_tornado_in_now_only(self):
        self.assertEqual(len(self.visible(weather(),'now')),1)
        self.assertEqual(self.visible(weather()),[])

    def test_routine_and_distant_weather_hidden(self):
        for o in [weather('Special Weather Statement'),weather('Dense Fog Advisory'),weather(location='Fulton, GA')]:
            self.assertEqual(self.visible(o,'now'),[])
            self.assertEqual(self.visible(o),[])
            self.assertEqual(len(self.visible(o,'now',include_suppressed=True)),1)

    def test_onset_missing_expiration_and_cancelled(self):
        for o in [weather(expires=None),weather(onset='2026-09-22T05:00:00Z'),weather(messageType='Cancel')]:
            self.assertEqual(self.visible(o,'now'),[])

    def test_ends_precedes_expires(self):
        self.assertEqual(self.visible(weather(ends='2026-09-22T02:30:00Z'),'now'),[])

    def test_remote_moderate_quakes_suppressed(self):
        self.assertEqual(self.visible(quake()),[])
        self.assertEqual(self.visible(quake(6.5,(-171,52),tsunami=1)),[])

    def test_regional_and_exceptional_quakes(self):
        for o in [quake(4.0,(-84,36)),quake(8.0),quake(6.8,(-120,38),felt=1000)]:
            self.assertEqual(len(self.visible(o)),1)

    def test_operational_infrastructure_and_cyber(self):
        self.assertEqual(len(self.visible(obs())),1)
        self.assertEqual(len(self.visible(obs(category='cyber',location='United States',title='Known exploited vulnerability',raw={'_area':'NATIONAL'}))),1)
        self.assertEqual(self.visible(obs(title='Annual newsletter',body='Office announcement',severity='unknown')),[])

    def test_age_remaining_and_score_components(self):
        self.assertEqual(age('2026-09-22T02:42:00Z',NOW),'18m')
        self.assertEqual(remaining('2026-09-22T04:00:00Z',NOW),'1h left')
        self.assertEqual(remaining('2026-09-22T02:00:00Z',NOW),'ended')
        self.assertEqual(age('',NOW),'?')
        row=self.visible(obs())[0]
        self.assertEqual(row['score'],sum(row['breakdown'].values()))
        self.assertEqual(row['area'],'ETN/WNC')
        self.assertEqual(set(row['breakdown']),{'geography','category','impact','freshness','source_confidence','corroboration','time_sensitivity','operator_priority'})

    def test_affected_area_not_issuing_office(self):
        o=weather(location='Wake, NC');o.title='Tornado Warning by NWS Knoxville TN'
        self.assertEqual(self.visible(o,'now'),[])

    def test_operator_policy_and_filters(self):
        self.assertEqual(self.visible(obs(),category='cyber'),[])
        self.assertEqual(self.visible(obs(),max_age=.5),[])
        self.assertEqual(self.visible(obs(),area='GLOBAL'),[])
        self.assertEqual(self.visible(obs(),policy={'minimum_score':100}),[])
        self.assertEqual(len(self.visible(obs(),policy={'minimum_score':100,'category_priorities':{'infrastructure':50}})),1)

    def test_relevance_then_freshness_sort(self):
        with connect(':memory:') as db:
            old,_=ingest(db,obs())
            new,_=ingest(db,obs(external_id='new',title='New major communications outage',published_at='2026-09-22T02:45:00Z'))
            national,_=ingest(db,obs(external_id='national',title='National outage',location='United States',severity='extreme'))
            self.assertEqual([r['id'] for r in queue(db,now=NOW)],[new,old,national])

    def test_generic_title_events_do_not_merge(self):
        with connect(':memory:') as db:
            for i,o in enumerate([weather(),weather(location='Buncombe, NC'),obs(title='Incident',location='Knoxville TN'),obs(title='Incident',location='Knoxville TN')]):
                ingest(db,replace(o,external_id=str(i)))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claims').fetchone()[0],4)

    def test_same_title_different_dates_do_not_merge(self):
        with connect(':memory:') as db:
            a,_=ingest(db,obs());b,_=ingest(db,obs(external_id='other',published_at='2026-09-20T02:00:00Z'))
            self.assertNotEqual(a,b)

    def test_mirrors_and_community_labels(self):
        with connect(':memory:') as db:
            a,_=ingest(db,obs(title='Major communications outage near Knoxville terminal',source_type='community',source_family='origin'))
            ingest(db,obs(source_id='mirror',external_id='mirror1',title='Major communications outage near Knoxville terminal',source_type='community',source_family='origin'))
            row,_=claim_detail(db,a,now=NOW)
            self.assertEqual(row['confidence_label'],'UNVERIFIED');self.assertEqual(row['independent_families'],1)
            ingest(db,obs(source_id='other',external_id='other1',title='Major communications outage near Knoxville terminal',source_type='community',source_family='independent'))
            row,_=claim_detail(db,a,now=NOW);self.assertEqual(row['confidence_label'],'UNVERIFIED')
            with self.assertRaises(ValueError): transition(db,a,'TX_CANDIDATE')
            ingest(db,obs(source_id='official',external_id='official1',title='Major communications outage near Knoxville terminal',source_type='official',source_family='agency'))
            row,_=claim_detail(db,a,now=NOW);self.assertEqual(row['confidence_label'],'CONFIRMED')

    def test_revision_preservation_and_repeat_dedup(self):
        with connect(':memory:') as db:
            a,_=ingest(db,obs());before=tuple(db.execute('SELECT * FROM observations').fetchone())
            changed=obs(body='New confirmed service outage detail')
            self.assertTrue(ingest(db,changed)[1]);self.assertFalse(ingest(db,changed)[1])
            self.assertEqual(before,tuple(db.execute('SELECT * FROM observations').fetchone()))
            self.assertEqual(len(claim_detail(db,a,now=NOW)[1]),2)

    def test_bulk_dry_run_apply_and_terminal_state(self):
        with connect(':memory:') as db:
            a,_=ingest(db,weather(expires='2026-09-22T02:00:00Z'))
            self.assertEqual(len(housekeeping(db,now=NOW)),1)
            self.assertEqual(db.execute('SELECT status FROM claims').fetchone()[0],'NEW')
            housekeeping(db,now=NOW,apply=True)
            self.assertEqual(db.execute('SELECT status FROM claims').fetchone()[0],'EXPIRED')
            self.assertEqual(housekeeping(db,now=NOW,apply=True),[])
            with self.assertRaises(ValueError):transition(db,a,'TX_CANDIDATE')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],1)

    def test_bulk_suppression_does_not_hide_reviewed(self):
        with connect(':memory:') as db:
            a,_=ingest(db,quake());transition(db,a,'REVIEW')
            self.assertEqual(housekeeping(db,now=NOW,apply=True,suppress=True),[])

    def test_schema_migration_requires_cli_backup_and_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=str(root/'old.db')
            with connect(path) as db:
                claim_id,_=ingest(db,obs(source_type='community',source_family='relay'))
                db.execute("UPDATE claims SET status='TX_CANDIDATE',tx_candidate_basis=NULL WHERE id=?",(claim_id,))
                db.execute("UPDATE observations SET raw_json=? WHERE claim_id=?",(json.dumps({'_trusted_for_relay':True}),claim_id))
                db.execute("INSERT INTO source_polls VALUES('legacy','2026-01-01T00:00:00Z','2026-01-01T01:00:00Z',0,'Collection failed; see run output')")
                before=[tuple(r) for r in db.execute('SELECT * FROM observations')]
                db.execute('PRAGMA user_version=3')
            with self.assertRaisesRegex(ValueError,'shade migrate'):
                with connect(path): pass
            config=root/'config.toml';config.write_text('[operator]\ncallsign="TEST"\nnetwork="TEST"\nregion_label="TEST"\n[collection]\ndatabase="old.db"\nuser_agent="SHADE-Test test@example.test"\n')
            output=io.StringIO()
            with redirect_stdout(output): self.assertEqual(main(['--config',str(config),'migrate']),0)
            self.assertIn('tx_candidate_basis_backfills',output.getvalue())
            with connect(path) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],4)
                self.assertEqual(db.execute('SELECT tx_candidate_basis FROM claims WHERE id=?',(claim_id,)).fetchone()[0],'operator_relay')
                audit=db.execute("SELECT reason FROM workflow_audit WHERE claim_id=? AND reason LIKE 'migration v4:%'",(claim_id,)).fetchone()[0]
                self.assertIn('operator_relay',audit)
                self.assertIn('next poll',db.execute("SELECT error FROM source_polls WHERE source_id='legacy'").fetchone()[0])
                self.assertEqual(before,[tuple(r) for r in db.execute('SELECT * FROM observations')])
            backups=list(Path(folder).glob('*.bak'));self.assertEqual(len(backups),1)
            with closing(sqlite3.connect(backups[0])) as db:self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_source_parsers_and_attribution(self):
        from shade_node.model import SourceConfig
        s=SourceConfig('kev','cisa_kev','CISA','https://example.test','official','cisa')
        rows=parse_kev(s,json.dumps({'vulnerabilities':[{'cveID':'CVE-2026-1000','vulnerabilityName':'Test','shortDescription':'Vulnerability','requiredAction':'Patch','dateAdded':'2026-09-22'}]}).encode())
        self.assertEqual(rows[0].source_family,'cisa');self.assertEqual(rows[0].category,'cyber')
        with self.assertRaises(CollectionError):parse_kev(s,b'[]')
        status=parse_status(s,b'{"incidents":[{"id":"one","name":"Outage","created_at":"2026-09-22T02:00:00Z","impact":"major","components":[{"name":"Atlanta, United States"}]}]}')
        self.assertTrue(status[0].raw['_us_impact'])
        feed=parse_rss(s,b'<rss><channel><item><guid>x</guid><title>Report</title></item></channel></rss>')
        self.assertEqual(feed[0].published_at,'');self.assertIn('xml',feed[0].raw)

    def test_tdot_parser_keeps_incidents_and_full_closures_only(self):
        from shade_node.collectors import parse_tdot
        from shade_node.model import SourceConfig
        s=SourceConfig('tdot','tdot_events','TDOT','https://example.test','official','tdot-smartway')
        def feature(identity,kind,subtype,impact):
            return {'attributes':{'ID':identity,'EVENT_TYPE':kind,'EVENT_SUBTYPE':subtype,
                    'VEHICLE_IMPACT':impact,'DESCRIPTION':'I-40 event','COUNTY_NAME':'Knox',
                    'START_DATE':1789952400000}}
        payload={'features':[
            feature('crash','Incident','Multi-Vehicle Crash','Eastbound right lane blocked'),
            feature('work','Operations','Milling','Westbound right lane blocked'),
            feature('closed','Operations','Bridge Repair','Eastbound all lanes blocked'),
        ]}
        rows=parse_tdot(s,json.dumps(payload).encode())
        self.assertEqual([row.external_id for row in rows],['crash','closed'])
        self.assertTrue(all(row.raw['_snapshot'] for row in rows))
        self.assertEqual(rows[0].location,'Knox, TN')

    def test_top_news_has_separate_current_context_lane(self):
        item=obs(category='top-news',source_type='media',source_family='npr',
                 title='Federal government shutdown may disrupt flights',location='',severity='unknown',
                 published_at='2026-09-22T02:30:00Z',raw={'_area':'NATIONAL'})
        with connect(':memory:') as db:
            ingest(db,item)
            self.assertEqual(queue(db,lane='inbox',now=NOW),[])
            rows=queue(db,lane='context',now=NOW)
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['area'],'NATIONAL')
            self.assertEqual(rows[0]['lane'],'context')

    def test_top_news_and_chatter_noise_stay_out_of_active_lanes(self):
        quiet_top=obs(category='top-news',source_type='media',source_family='bbc',
                      title='Six-year-old sets puzzle record',location='',severity='unknown',
                      body='A child completed a puzzle very quickly.',raw={'_area':'NATIONAL'})
        noisy_body=obs(category='chatter',source_type='community',source_family='forum',
                       title='Weekend hiking guide',location='',severity='unknown',
                       raw={'_area':'Knoxville'},body='The park has a picnic shelter and school programs.')
        self.assertEqual(self.visible(quiet_top,'context'),[])
        self.assertEqual(self.visible(noisy_body),[])

    def test_fuel_price_and_durable_ice_context_are_visible(self):
        fuel=obs(category='fuel',source_type='official',source_family='eia',
                 title='Diesel prices hit record as shipping disruptions tighten supply',
                 body='National diesel price and crude oil costs rise.',location='United States',
                 raw={'_area':'NATIONAL'})
        self.assertEqual(len(self.visible(fuel)),1)
        ice=obs(category='top-news',source_type='media',source_family='cnbc',
                title='ICE enforcement protests and civil rights probe continue in Minneapolis',
                body='Federal agents and National Guard remain part of the ongoing story.',
                location='',published_at='2026-09-21T02:00:00Z',raw={'_area':'NATIONAL'})
        self.assertEqual(len(self.visible(ice,'context')),1)

    def test_insecure_fetch_refused_without_network(self):
        with self.assertRaises(CollectionError):fetch_bytes('http://example.test',user_agent='test',timeout=1,max_bytes=10)

    def test_telegram_and_acled_fixtures(self):
        from shade_node.model import SourceConfig
        tg=SourceConfig('tg','telegram_preview','TG','https://t.me/s/example','community','telegram-example',category='chatter')
        html=b'<div class="tgme_widget_message_wrap"><div data-post="example/42"><div class="tgme_widget_message_text">FBI breach update<br>details</div><time datetime="2026-09-22T02:00:00+00:00"></time><span class="tgme_widget_message_views">123</span></div></div>'
        rows=parse_telegram_preview(tg,html)
        self.assertEqual(rows[0].external_id,'example/42'); self.assertEqual(rows[0].raw['view_count'],'123')
        ac=SourceConfig('ac','acled_api','ACLED','https://example.test','official','acled',category='civil-unrest')
        rows=parse_acled(ac,json.dumps({'data':[{'data_id':1,'event_date':'2026-09-22','event_type':'Protests','location':'Knoxville','admin1':'Tennessee','country':'United States','actor1':'Group A','fatalities':0}]}).encode())
        self.assertEqual(rows[0].category,'civil-unrest'); self.assertEqual(rows[0].source_type,'official')

    def test_water_radnet_safecast_and_mastodon_fixtures(self):
        from shade_node.model import SourceConfig
        water=SourceConfig('water','usgs_waterservices','Water','https://example.test','official','usgs-waterservices',category='regional',area='ETN/WNC',threshold=10)
        payload={'value':{'timeSeries':[{'sourceInfo':{'siteCode':[{'value':'123'}],'siteName':'Test River'},'variable':{'variableCode':[{'value':'00065'}],'unit':{'unitCode':'ft'}},'values':[{'value':[{'value':'12.5','dateTime':'2026-09-23T01:00:00Z'}]}]}]}}
        rows=parse_usgs_waterservices(water,json.dumps(payload).encode()); self.assertTrue(rows[0].raw['_threshold_exceeded']); self.assertEqual(rows[0].severity,'severe')
        rad=SourceConfig('rad','epa_radnet','RadNet','https://example.test','official','epa-radnet',category='radiological',area='TN')
        rows=parse_radnet(rad,b'Date/Time,Location,Exposure Rate,Unit\n2026-09-23T01:00:00Z,Knoxville,0.12,mR/h\n'); self.assertEqual(rows[0].category,'radiological')
        safe=SourceConfig('safe','safecast','Safe','https://example.test','community','safecast',category='radiological')
        rows=parse_safecast(safe,b'{"measurements":[{"id":7,"value":0.1,"unit":"uSv/h","captured_at":"2026-09-23T01:00:00Z","latitude":35,"longitude":-84}]}'); self.assertEqual(rows[0].source_type,'community')
        mast=SourceConfig('mast','mastodon_hashtag','Mastodon','https://example.test','community','mastodon-example-Iran',category='chatter')
        rows=parse_mastodon(mast,b'[{"id":"9","url":"https://example/@a/9","created_at":"2026-09-23T01:00:00Z","content":"<p>#Iran update</p>","account":{"acct":"a@example"}}]'); self.assertEqual(rows[0].raw['author'],'a@example')

    def test_shared_keywords_and_google_search_terms(self):
        from shade_node.keywords import hashtags, load_keywords
        from shade_node.model import SourceConfig
        catalog = load_keywords(Path(__file__).parents[1] / 'keywords.toml')
        self.assertIn('Knoxville', catalog['local'])
        self.assertIn('Knoxville', hashtags(catalog))
        self.assertNotIn('Iran', hashtags(catalog))
        google = SourceConfig('news', 'google_news_search', 'Google', 'https://news.google.com/rss/search', 'media', 'google-news', keyword_tier='national', keywords=catalog)
        self.assertIn('fuel shortage', google.keywords['national'])

    def test_wzdx_feed_normalization_and_stale_registry_skip(self):
        from shade_node.collectors import _wzdx_registry_rows
        from shade_node.model import SourceConfig
        wz = SourceConfig('wz', 'wzdx_registry', 'WZDx', 'https://example.test/registry', 'official', 'wzdx-registry')
        payload = {'type':'FeatureCollection','features':[{'id':'evt-1','geometry':{'type':'Point','coordinates':[-84,36]},'properties':{'core_details':{'road_names':['I-40'],'event_type':'lane closure','start_date':'2026-09-23T01:00:00Z','end_date':'2026-09-24T01:00:00Z','description':'overnight work'}}}]}
        row = parse_wzdx_feed(wz, json.dumps(payload).encode(), state='TN')[0]
        self.assertEqual(row.source_family, 'wzdx-TN')
        self.assertIn('I-40', row.location)
        registry = {'results':[{'state':'TN','status':'inactive','feed_url':'https://dead.test/feed'},{'state':'NC','status':'active','feed_url':'https://live.test/feed'}]}
        self.assertEqual(_wzdx_registry_rows(json.dumps(registry).encode()), [({'state':'NC','status':'active','feed_url':'https://live.test/feed'}, 'https://live.test/feed')])

    def test_nps_inciweb_and_ipaws_archive_parsers(self):
        from shade_node.model import SourceConfig
        nps = SourceConfig('nps','nps_alerts','NPS','https://example.test','official','nps',park_codes=['grsm'])
        rows = parse_nps_alerts(nps, json.dumps({'data':[{'id':'a','parkCode':'grsm','title':'Trail closure','description':'Closed'}]}).encode())
        self.assertEqual(rows[0].source_family, 'nps')
        self.assertEqual(parse_nps_alerts(nps, json.dumps({'data':[{'id':'b','parkCode':'acad','title':'Other'}]}).encode()), [])
        inc = SourceConfig('inc','inciweb','InciWeb','https://example.test','official','inciweb',region_terms=['Cherokee'])
        rss = b'<rss><channel><item><title>Cherokee Fire</title><description>Incident update</description><guid>1</guid></item></channel></rss>'
        self.assertEqual(parse_inciweb(inc, rss)[0].source_family, 'inciweb')
        arc = SourceConfig('arc','ipaws_archive','IPAWS','https://example.test','official','ipaws-archive')
        archived = parse_ipaws_archive(arc, json.dumps({'IpawsArchivedAlerts':[{'identifier':'x','info_headline':'Test','sent':'2026-09-22T00:00:00Z'}]}).encode())
        self.assertTrue(archived[0].raw['_lagging_archive'])

    def test_high_credibility_is_informational_and_operator_relay_is_explicit(self):
        trusted=obs(source_type='community',source_family='any-community-source',category='chatter',
                    title='Major outage reported near Knoxville',raw={'_area':'Knoxville','_high_credibility_source':True})
        with connect(':memory:') as db:
            claim_id,_=ingest(db,trusted)
            row,_=claim_detail(db,claim_id,now=NOW)
            self.assertEqual(row['confidence_label'],'UNVERIFIED (HIGH-CRED SOURCE)')
            transition(db,claim_id,'REVIEW')
            with self.assertRaises(ValueError): transition(db,claim_id,'TX_CANDIDATE')
            transition(db,claim_id,'TX_CANDIDATE',operator_override=True)
            row,_=claim_detail(db,claim_id,now=NOW)
            self.assertEqual(row['tx_candidate_basis'],'operator_relay')
            self.assertEqual(row['confidence_label'],'UNVERIFIED (HIGH-CRED SOURCE)')
            audit=db.execute('SELECT reason,tx_candidate_basis FROM workflow_audit ORDER BY id DESC').fetchone()
            self.assertEqual(tuple(audit),('operator relay override','operator_relay'))

    def test_cli_relay_override_end_to_end(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=root/'config.toml';database=root/'test.db'
            config.write_text('[operator]\ncallsign="TEST"\nnetwork="TEST"\nregion_label="TEST"\n[collection]\ndatabase="test.db"\nuser_agent="SHADE-Test test@example.test"\n')
            with connect(str(database)) as db:
                claim_id,_=ingest(db,obs(source_type='community',source_family='community'))
                transition(db,claim_id,'REVIEW')
            output=io.StringIO()
            with redirect_stdout(output): self.assertEqual(main(['--config',str(config),'relay',str(claim_id)]),0)
            self.assertIn('operator relay',output.getvalue())
            with connect(str(database)) as db:
                claim,_=claim_detail(db,claim_id,now=NOW)
                self.assertEqual((claim['status'],claim['tx_candidate_basis']),('TX_CANDIDATE','operator_relay'))
            output=io.StringIO()
            with redirect_stdout(output): self.assertEqual(main(['--config',str(config),'inbox','--status','TX_CANDIDATE','--lane','all','--all']),0)
            self.assertIn('TX_CANDIDATE (operator relay)',output.getvalue())

    def test_ordinary_community_and_media_confidence_are_canonical(self):
        with connect(':memory:') as db:
            ordinary,_=ingest(db,obs(source_type='community',source_family='ordinary'))
            row,_=claim_detail(db,ordinary,now=NOW)
            self.assertEqual(row['confidence_label'],'UNVERIFIED')
            ingest(db,obs(source_type='community',source_family='ordinary',raw={'_high_credibility_source':True}))
            persisted=db.execute('SELECT confidence_label FROM claims WHERE id=?',(ordinary,)).fetchone()[0]
            row,_=claim_detail(db,ordinary,now=NOW)
            self.assertEqual(persisted,row['confidence_label'])
            self.assertEqual(persisted,'UNVERIFIED (HIGH-CRED SOURCE)')
            media,_=ingest(db,obs(source_id='media',external_id='media',source_type='media',source_family='news',
                                  title='Major outage reported near Knoxville terminal'))
            ingest(db,obs(source_id='community',external_id='community',source_type='community',source_family='witness',
                          title='Major outage reported near Knoxville terminal'))
            persisted=db.execute('SELECT confidence_label FROM claims WHERE id=?',(media,)).fetchone()[0]
            queried,_=claim_detail(db,media,now=NOW)
            self.assertEqual(persisted,'CORROBORATED')
            self.assertEqual(queried['confidence_label'],persisted)

    def test_bulletin_split_and_numbering(self):
        parts=split_message('Sentence one. Sentence two with enough words to split cleanly.', 24)
        self.assertTrue(all(len(part)<=24 for part in parts)); self.assertEqual(''.join(parts).replace(' ',''), 'Sentenceone.Sentencetwowithenoughwordstosplitcleanly.')
        claim=dict(self.visible(obs())[0]); claim['confidence_label']='OFFICIAL-REPORT'; claim['status']='REVIEW'
        official=dict(source_family='agency',source_type='official',raw_json='{}')
        messages=bulletin_messages([(claim,[official])],callsign='T',network='N',max_chars=120)
        self.assertTrue(all('/' in line for line in messages));self.assertIn('(C)',messages[1])
        claim['status']='TX_CANDIDATE';claim['tx_candidate_basis']='operator_relay'
        community=dict(source_family='community',source_type='community',raw_json='{}')
        self.assertIn('(O)',bulletin_messages([(claim,[community])],callsign='T',network='N',max_chars=120)[1])
        claim['status']='REVIEW';claim['tx_candidate_basis']=None
        self.assertIn('(U)',bulletin_messages([(claim,[community])],callsign='T',network='N',max_chars=120)[1])
        high=dict(source_family='community',source_type='community',raw_json='{"_high_credibility_source":true}')
        self.assertIn('(U-HC)',bulletin_messages([(claim,[high])],callsign='T',network='N',max_chars=120)[1])

    def test_format_length_and_exercise_markers(self):
        with connect(':memory:') as db:
            a,_=ingest(db,obs());row,items=claim_detail(db,a,now=NOW)
            message=traffic_message(row,items,callsign='TEST',network='TEST',mode='exercise',max_chars=240)
            self.assertTrue(message.endswith('EXERCISE'));self.assertLessEqual(len(message),240)
            with self.assertRaises(ValueError):traffic_message(row,items,callsign='TEST',network='TEST',mode='exercise',max_chars=20)
            self.assertIn('SIGNIFICANCE:',evidence_summary(row,items))

    def test_cli_help_primary_commands_and_safe_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            config=Path(folder)/'config.toml'
            config.write_text('[operator]\ncallsign="TEST"\nregion_label="ETN/WNC"\n[collection]\ndatabase="test.db"\nuser_agent="SHADE (https://example.test/contact)"\n')
            prefix=['--config',str(config)]
            with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                for command in ['migrate','doctor','status','inbox','queue','now','housekeep']:
                    self.assertEqual(main(prefix+[command]),0,command)
                self.assertEqual(main(prefix+['show','999']),2)
                for command in ['inbox','now','show','mark','format','run-once','doctor','housekeep','migrate']:
                    with self.assertRaises(SystemExit) as result:parser().parse_args([command,'--help'])
                    self.assertEqual(result.exception.code,0)

    def test_configuration_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            config=Path(folder)/'config.toml';config.write_text('private contents')
            with redirect_stderr(io.StringIO()):self.assertEqual(main(['--config',str(config),'init']),2)
            self.assertEqual(config.read_text(),'private contents')



    def test_faa_major_delay_and_snapshot_expiration(self):
        from shade_node.collectors import parse_faa
        from shade_node.model import SourceConfig
        s=SourceConfig('faa','faa_status','FAA','https://example.test','official','faa')
        data=b'<AIRPORT_STATUS_INFORMATION><Delay_type><Name>Ground Delay Programs</Name><Ground_Delay_List><Ground_Delay><ARPT>TYS</ARPT><Reason>equipment failure</Reason><Avg>3 hours</Avg></Ground_Delay></Ground_Delay_List></Delay_type></AIRPORT_STATUS_INFORMATION>'
        item=parse_faa(s,data)[0];item.observed_at='2026-09-22T02:50:00Z'
        with connect(':memory:') as db:
            a,_=ingest(db,item)
            db.execute('INSERT INTO source_presence VALUES(?,?,?,1)',(item.source_id,item.external_id,'2026-09-22T02:50:00Z'))
            self.assertEqual(len(queue(db,now=NOW)),1)
            self.assertEqual(queue(db,now=NOW+timedelta(hours=1)),[])
            db.execute('UPDATE source_presence SET active=0')
            self.assertEqual(queue(db,now=NOW),[])

    def test_faa_ground_stop_is_major_and_dca_is_named(self):
        from shade_node.collectors import parse_faa
        from shade_node.model import SourceConfig
        s=SourceConfig('faa','faa_status','FAA','https://example.test','official','faa')
        data=b'<AIRPORT_STATUS_INFORMATION><Delay_type><Name>Ground Stops</Name><Ground_Stop_List><Ground_Stop><ARPT>DCA</ARPT><Reason>weather</Reason></Ground_Stop></Ground_Stop_List></Delay_type></AIRPORT_STATUS_INFORMATION>'
        item=parse_faa(s,data)[0]
        self.assertEqual(item.location,'Reagan Washington National Airport')
        self.assertEqual(item.severity,'severe')

    def test_resolved_status_report_is_not_active(self):
        self.assertEqual(self.visible(obs(raw={'status':'resolved'})),[])

    def test_cooldown_and_second_identical_collection(self):
        from shade_node.engine import Settings
        from shade_node.model import SourceConfig
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(str(Path(folder)/'test.db'),7,10,1000,'test','TEST','TEST','TEST',500,'standard',35,10,'TEST','TEST',500,
                              [SourceConfig('fixture','rss','Fixture','https://example.test','official','agency')])
            with patch('shade_node.engine.collect',return_value=[obs(published_at=datetime.now(timezone.utc).isoformat())]) as collect:
                first=run_once(settings);self.assertEqual(first['inserted'],1)
                second=run_once(settings);self.assertEqual(second['sources_skipped_cooldown'],1)
                with connect(settings.database) as db:db.execute("UPDATE source_polls SET next_allowed='2000-01-01T00:00:00Z'")
                third=run_once(settings);self.assertEqual(third['inserted'],0);self.assertEqual(collect.call_count,2)

    def test_shared_telegram_and_per_instance_mastodon_budgets(self):
        from shade_node.engine import Settings
        from shade_node.model import SourceConfig
        sources=[
            SourceConfig('tg-a','telegram_preview','A','https://t.me/s/a','community','a',min_poll_seconds=60),
            SourceConfig('tg-b','telegram_preview','B','https://t.me/s/b','community','b',min_poll_seconds=60),
            SourceConfig('m-a','mastodon_hashtag','M1','https://mastodon.social/api/v1/timelines/tag/a','community','ma',instance='mastodon.social',hashtag='a',min_poll_seconds=60),
            SourceConfig('m-b','mastodon_hashtag','M2','https://mastodon.social/api/v1/timelines/tag/b','community','mb',instance='mastodon.social',hashtag='b',min_poll_seconds=60),
            SourceConfig('m-other','mastodon_hashtag','M3','https://infosec.exchange/api/v1/timelines/tag/a','community','mc',instance='infosec.exchange',hashtag='a',min_poll_seconds=60),
        ]
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(str(Path(folder)/'test.db'),7,10,1000,'test','TEST','TEST','TEST',500,'standard',35,10,'TEST','TEST',500,sources)
            with patch('shade_node.engine.collect',return_value=[]) as collect:
                result=run_once(settings)
                self.assertEqual(result['sources_selected'],3)
                selected={call.args[0].id for call in collect.call_args_list}
                self.assertEqual(len(selected & {'tg-a','tg-b'}),1)
                self.assertEqual(len(selected & {'m-a','m-b'}),1)
                self.assertIn('m-other',selected)

    def test_source_poll_records_real_failure(self):
        from shade_node.collectors import CollectionError
        from shade_node.engine import Settings
        from shade_node.model import SourceConfig
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(str(Path(folder)/'test.db'),7,10,1000,'test','TEST','TEST','TEST',500,'standard',35,10,'TEST','TEST',500,
                              [SourceConfig('broken','rss','Broken','https://example.test','official','agency')])
            with patch('shade_node.engine.collect',side_effect=CollectionError('HTTP 429',retry_after=3600)):
                run_once(settings)
            with connect(settings.database) as db:
                self.assertEqual(db.execute("SELECT error FROM source_polls WHERE source_id='broken'").fetchone()[0],'HTTP 429')
            config=Path(folder)/'config.toml'
            config.write_text('[operator]\ncallsign="TEST"\nnetwork="TEST"\nregion_label="TEST"\n[collection]\ndatabase="test.db"\nuser_agent="SHADE-Test test@example.test"\n[[sources]]\nid="broken"\nkind="rss"\nname="Broken"\nurl="https://example.test"\nsource_type="official"\nsource_family="agency"\n')
            output=io.StringIO()
            with redirect_stdout(output): self.assertEqual(main(['--config',str(config),'doctor']),1)
            self.assertIn('Source failure: broken: HTTP 429',output.getvalue())

    def test_high_credibility_config_is_stamped_without_promotion(self):
        from shade_node.engine import Settings
        from shade_node.model import SourceConfig
        with tempfile.TemporaryDirectory() as folder:
            source=SourceConfig('high','rss','High','https://example.test','community','high',high_credibility=True)
            settings=Settings(str(Path(folder)/'test.db'),7,10,1000,'test','TEST','TEST','TEST',500,'standard',35,10,'TEST','TEST',500,[source])
            item=obs(source_id='high',source_type='community',source_family='high',published_at=datetime.now(timezone.utc).isoformat())
            with patch('shade_node.engine.collect',return_value=[item]): run_once(settings)
            with connect(settings.database) as db:
                claim,_=claim_detail(db,1)
                self.assertEqual(claim['confidence_label'],'UNVERIFIED (HIGH-CRED SOURCE)')
                transition(db,1,'REVIEW')
                with self.assertRaises(ValueError): transition(db,1,'TX_CANDIDATE')

    def test_actual_and_exercise_cli_formatting_gates(self):
        with tempfile.TemporaryDirectory() as folder:
            config=Path(folder)/'config.toml'
            config.write_text('[operator]\ncallsign="TEST"\nregion_label="ETN/WNC"\n[operation]\nmode="actual"\n[collection]\ndatabase="test.db"\n')
            with connect(str(Path(folder)/'test.db')) as db:
                a,_=ingest(db,obs(published_at=datetime.now(timezone.utc).isoformat()))
                transition(db,a,'REVIEW')
            prefix=['--config',str(config)]
            with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                self.assertEqual(main(prefix+['format',str(a)]),2)
                self.assertEqual(main(prefix+['format',str(a),'--confirm-actual']),0)
                self.assertEqual(main(prefix+['--mode','exercise','format',str(a)]),0)
                out=io.StringIO()
                with redirect_stdout(out):self.assertEqual(main(prefix+['--mode','standard','status']),0)
                self.assertIn('MODE: STANDARD',out.getvalue())


    def test_no_known_exploitation_not_operational(self):
        self.assertEqual(self.visible(obs(category='cyber',source_family='cisa',title='Device advisory',body='No known public exploitation specifically targeting this vulnerability.',severity='unknown',location='United States')),[])

    def test_fema_parser_preserves_fields(self):
        from shade_node.model import SourceConfig
        s=SourceConfig('fema','fema_declarations','FEMA','https://example.test','official','fema')
        raw={'id':'one','declarationTitle':'SEVERE STORMS','disasterNumber':100,'designatedArea':'Knox (County)','state':'TN','declarationDate':'2026-09-22T00:00:00Z'}
        item=parse_fema(s,json.dumps({'DisasterDeclarationsSummaries':[raw]}).encode())[0]
        self.assertEqual(item.raw,raw);self.assertEqual(item.location,'Knox (County), TN')

    def test_configured_actual_cannot_override_scheduled_standard(self):
        # Explicit --mode standard wins over private config, proven without collection.
        with tempfile.TemporaryDirectory() as folder:
            config=Path(folder)/'config.toml';config.write_text('[operation]\nmode="actual"\n')
            out=io.StringIO()
            with redirect_stdout(out): result=main(['--config',str(config),'--mode','standard','status'])
            self.assertEqual(result,0);self.assertIn('MODE: STANDARD',out.getvalue())


    def test_nws_cancellation_supersedes_original_warning(self):
        with connect(':memory:') as db:
            ingest(db,weather())
            newer=weather(messageType='Cancel',references=[{'identifier':'event-1'}])
            ingest(db,replace(newer,external_id='cancel-1'))
            self.assertEqual(queue(db,lane='now',now=NOW),[])


    def test_same_origin_event_through_two_feeds(self):
        with connect(':memory:') as db:
            a,_=ingest(db,quake())
            db.execute("UPDATE claims SET fingerprint='legacy-fingerprint' WHERE id=?",(a,))
            b,_=ingest(db,replace(quake(),source_id='regional-feed'))
            self.assertEqual(a,b)
            row,_=claim_detail(db,a,now=NOW)
            self.assertEqual(row['independent_families'],1)


    def test_new_source_validity_extension_reopens_expired(self):
        with connect(':memory:') as db:
            a,_=ingest(db,weather(expires='2020-01-01T00:00:00Z'))
            housekeeping(db,apply=True)
            self.assertEqual(db.execute('SELECT status FROM claims').fetchone()[0],'EXPIRED')
            ingest(db,weather(expires=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()))
            self.assertEqual(db.execute('SELECT status FROM claims').fetchone()[0],'NEW')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],1)
