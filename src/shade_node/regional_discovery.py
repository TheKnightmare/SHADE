"""Rotating regional news searches. Each three-hour slot covers new pairs.

The locations and concept families are data, so adding a county or a synonym
does not require adding a new collector or copying a long query string.
"""
from datetime import datetime, timezone


CONCEPTS = (
    '(police OR sheriff OR arrest OR investigation OR shooting)',
    '(fire OR crash OR closure OR derailment OR landslide)',
    '(outage OR blackout OR utility OR water OR communications)',
    '(camera OR surveillance OR ALPR OR "license plate reader" OR Flock)',
    '(drone OR prison OR hazmat OR nuclear OR DOE)',
    '(earthquake OR wildfire OR explosion OR cyber OR ransomware)',
)


def search_queries(source, now: datetime | None = None) -> list[str]:
    """Return a bounded, rotating slice that covers every pair in 24 hours."""
    locations = source.region_terms
    if not locations:
        return source.query_terms[:source.max_requests_per_poll]
    now = now or datetime.now(timezone.utc)
    # A deterministic daily rotation makes missed searches reproducible. Four
    # location groups x six concepts fit in eight three-hour polls at 3 requests.
    slot = int(now.timestamp() // 10800) % 8
    pairs = [(location, concept) for location in locations for concept in CONCEPTS]
    budget = source.max_requests_per_poll
    start = slot * budget
    selected = [pairs[(start + offset) % len(pairs)] for offset in range(budget)]
    return [f'{location} {concept} when:7d' for location, concept in selected]
