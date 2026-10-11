"""Focused tests for this pass's two targeted optimizations:

1. gazetteer.resolve() memoization: a bounded LRU kept across queue() calls
   (pure function of its text, so reuse is always correct).
2. The substring prefilter added in relevance.geography() before the
   regional_states regex checks - must never change which rows match, only
   how many regexes run to decide.
"""
import unittest
from dataclasses import replace

from shade_node import gazetteer
from shade_node.db import connect, ingest, queue
from shade_node.model import Observation
from shade_node.relevance import DEFAULT_POLICY, geography, metadata


def obs(**kwargs):
    base = Observation('fixture', 'Fixture', 'official', 'agency', 'event-1',
                        'Major communications outage', 'Service outage confirmed',
                        'https://example.test/1', category='infrastructure', location='Knoxville TN',
                        severity='severe', published_at='2026-09-22T02:00:00Z', observed_at='2026-09-22T02:10:00Z')
    return replace(base, **kwargs)


def geo(overrides=None):
    """geography() needs a full evidence-row dict (via metadata()), not the
    Observation itself - build one the same way db.py's bulk/single paths do."""
    row = {'title': 'Fixture', 'body': '', 'location': '', 'category': 'infrastructure',
           'source_type': 'official', 'source_family': 'fixture',
           'published_at': '2026-09-22T02:00:00Z', 'observed_at': '2026-09-22T02:10:00Z',
           'raw_json': '{}'}
    row.update(overrides or {})
    meta = metadata(row)
    return geography(row, meta, DEFAULT_POLICY)


class TestGazetteerCacheCorrectness(unittest.TestCase):
    def setUp(self):
        gazetteer.resolve.cache_clear()

    def tearDown(self):
        gazetteer.resolve.cache_clear()

    def test_cached_result_matches_uncached_result(self):
        text = 'Substation fire reported in Knoxville, Tennessee overnight'
        gazetteer.resolve.cache_clear()
        first = gazetteer.resolve(text)
        cached = gazetteer.resolve(text)
        self.assertEqual(first, cached)
        self.assertEqual(gazetteer.resolve.cache_info().hits, 1)

    def test_distinct_texts_do_not_collide_in_cache(self):
        a = gazetteer.resolve('Flooding reported in Asheville, North Carolina')
        b = gazetteer.resolve('Flooding reported in Knoxville, Tennessee')
        self.assertNotEqual(a, b)
        self.assertEqual(a['state'], 'NC')
        self.assertEqual(b['state'], 'TN')

    def test_cache_is_bounded_and_reused_across_queue_calls(self):
        # Speed pass 2026-10-09: resolve() is a pure function of its text, so
        # the cache is now kept across queue() calls (bounded LRU) instead of
        # cleared each call; repeated desktop refreshes reuse it.
        self.assertEqual(gazetteer.resolve.cache_info().maxsize, 65536)
        sentinel = 'Sentinel unique headline about Sylva, North Carolina today'
        gazetteer.resolve.cache_clear()
        first = gazetteer.resolve(sentinel)
        with connect(':memory:') as db:
            ingest(db, obs())
            queue(db, lane='inbox')
        before = gazetteer.resolve.cache_info()
        self.assertEqual(gazetteer.resolve(sentinel), first)
        self.assertEqual(gazetteer.resolve.cache_info().hits, before.hits + 1)

    def test_resolve_is_pure_cache_never_changes_the_answer(self):
        texts = [
            'Substation outage in Knoxville, Tennessee affects downtown',
            'Wildfire near Gatlinburg prompts evacuations',
            'Storm damage reported across the Midwest',
            '',
            'Random headline with no place names at all',
        ]
        gazetteer.resolve.cache_clear()
        uncached = [gazetteer.resolve(t) for t in texts]
        for t in texts:
            gazetteer.resolve.cache_clear()
        gazetteer.resolve.cache_clear()
        cached_twice = [gazetteer.resolve(t) for t in texts] + [gazetteer.resolve(t) for t in texts]
        self.assertEqual(uncached, cached_twice[:len(texts)])
        self.assertEqual(cached_twice[:len(texts)], cached_twice[len(texts):])


class TestGeographyRegionalStatePrefilter(unittest.TestCase):
    def test_state_abbreviation_present_is_still_regional(self):
        area, score = geo({'location': '', 'title': 'Outage reported near Macon GA today', 'body': ''})
        self.assertEqual(area, 'REGIONAL')

    def test_state_abbreviation_absent_is_not_regional_from_this_branch(self):
        area, score = geo({'location': '', 'title': 'Outage reported in Springfield', 'body': 'No state mentioned anywhere.'})
        self.assertNotEqual(area, 'REGIONAL')

    def test_word_boundary_prevents_false_positive_substring_match(self):
        # 'GA' is a substring of 'MEGA' and 'NC' of 'FINANCE' - the cheap
        # prefilter will flag both as *candidates*, but the existing \b-
        # wrapped regex must still reject them, exactly as before this pass.
        area, score = geo({'location': '', 'title': 'MEGA finance conference held virtually', 'body': ''})
        self.assertNotEqual(area, 'REGIONAL')

    def test_full_state_name_still_matches_case_insensitively(self):
        area, score = geo({'location': '', 'title': 'Flooding reported across north carolina communities', 'body': ''})
        self.assertEqual(area, 'REGIONAL')

    def test_full_state_name_mixed_case_matches(self):
        area, score = geo({'location': '', 'title': 'Storm damage reported in Kentucky overnight', 'body': ''})
        self.assertEqual(area, 'REGIONAL')

    def test_no_state_signal_at_all_falls_through(self):
        area, score = geo({'location': '', 'title': 'Storm damage reported overnight', 'body': 'No further detail available.'})
        self.assertNotIn(area, {'ETN/WNC', 'REGIONAL'})

    def test_boundary_state_code_at_string_start_and_end(self):
        self.assertEqual(geo({'location': '', 'title': 'NC', 'body': ''})[0], 'REGIONAL')
        self.assertEqual(geo({'location': '', 'title': 'severe weather hits GA', 'body': ''})[0], 'REGIONAL')


if __name__ == '__main__':
    unittest.main(verbosity=2)
