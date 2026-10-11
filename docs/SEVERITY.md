# Event severity scoring

SHADE uses deterministic, case-insensitive word-boundary patterns in
relevance.py as one shared event vocabulary. The highest matching tier
contributes impact points; a source's structured severity can raise that
contribution. Operators can inspect every tier and pattern in SEVERITY_TIERS.
No model or factual verification is involved.

| Tier | Impact points | Examples |
|---|---:|---|
| Fatality or mass casualty | 30 | fatal shooting, deaths, bodies recovered |
| Self-harm or crisis | 25 | suicide attempts, self-harm, mental health crisis |
| Structural failure | 22 | bridge collapse, dam failure |
| Injury or acute harm | 20 | shooting, wounded, collision |
| Disruption or civic impact | 15 | outage, evacuation, protest |

The same vocabulary supplies the shared event signal used in scoring.
A claim already categorized as civil unrest is not subject to a second,
narrower keyword gate. Geography contributes 0 to 30 points but never hides
NEW or REVIEW reports. Earthquake magnitude uses one area-neutral threshold;
weather validity and event type still control the NOW lane. Routine newsletters and announcements have a narrow
title-pattern filter; explicit SUPPRESSED workflow states remain hidden unless
the operator chooses to show them. The suppression-review command reports old
suppressions whose retired reason has changed; it never changes their state.

The claims.structured_severity_score column stores the highest source-supplied
severity value. The significance_score returned in the operator view is the
live 0-100 policy score recomputed with current freshness, source confidence,
geography, and these impact tiers. These are intentionally distinct fields.
