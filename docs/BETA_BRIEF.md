# SHADE Beta Brief

## One-line description

SHADE turns participating amateur-radio operators into geographically
distributed, provenance-aware information nodes without automating RF
transmission.

> **A thousand reports can blot out the signal. We fight in the shade.**

Forward operators communicate and act. SHADE is the back end: it works beneath
the information noise, preserves provenance, removes repeated echoes, and
delivers a clearer sourced picture to the people out front.

## Problem

Operators can find large volumes of public information, but useful traffic is
buried among duplicates, mirrors, stale posts, weak sourcing, and routine
noise. Copying the first headline found can make repeated reports look like
independent corroboration.

## Approach

SHADE separates the process into distinct stages:

1. Harvest permitted public feeds and APIs.
2. Preserve the original observation and source metadata.
3. Attribute each observation to an originating source family.
4. Deduplicate and correlate likely repeats into claims.
5. Score significance and describe confidence without claiming truth.
6. Present evidence to a human operator.
7. Export concise traffic only after review.

## Safety boundary

- No CAT, PTT, serial, or automatic transmitter control
- No automatic claim of truth
- No automatic emergency declaration
- No bypass of source authentication or access controls
- No direct transition from `NEW` to `SENT`
- Actual EmComm formatting requires an explicit confirmation flag

## EmComm profiles

- **STANDARD:** routine collection and conservative queue threshold
- **EXERCISE:** activates emergency-only sources and labels generated traffic as an exercise at both ends
- **ACTUAL:** activates emergency-only sources and adds a confirmation gate before copy-ready formatting

## Proposed proof of concept

Run a private, operator-controlled POC for several months and measure:

- duplicate reduction
- independent source-family count
- time from first observation to reviewed candidate
- false correlations corrected by the operator
- number of candidates accepted, rejected, and transmitted
- usefulness during scheduled exercises

## Beta ask

Invite a small group of experienced operators to review the workflow, source
model, message format, and EmComm controls. The goal is feedback and evidence,
not endorsement or organizational affiliation.

> **BE FREE // FIGHT IN THE SHADE**
