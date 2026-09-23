# S2 watcher layer

SHADE treats S2 Underground Wire as an operator-designated high-credibility
source and also watches the watcher. This designation is informational only.
S2-originated reports retain the `s2-underground-*` family and are labeled
`UNVERIFIED (HIGH-CRED SOURCE)` when the configured Wire source is their only
evidence; they are never silently relabeled as government or independent
official evidence and never advance automatically.

## Automated sources

- `@S2undergroundWire` — Wire reports; high-credibility display designation.
- `@s2_underground_project` — project updates; ordinary community evidence.
- `s2underground/GhostMaps` GitHub Atom activity — map/KMZ changes.
- S2 podcast RSS — longer-form reports and methodology context.

## Watchlist-only sources

GhostNotes also links to the S2 tipline forms, logistical-shortage and drone
sighting forms, ArcGIS profile/CIP/dashboards, X, YouTube, Rumble, Gab, Odysee,
BitChute, Nostr, NomadNet/LXMF, and the public PGP key. These are not all
reliable public feeds: some accept submissions, some require interactive map
state, and some do not expose a stable unauthenticated feed. They remain
operator watchlist destinations rather than invented scrapers.

The watcher layer is a discovery and provenance aid. A report can be relayed
with S2 attribution, but local impact, freshness, and any non-S2 corroboration
remain visible to the operator.
