---
title: Data Freshness SLAs
category: reference
owner: analytics-platform-team
tags: [sla, freshness, latency, refresh, commitments]
last_reviewed: 2026-11-02
---

# Data Freshness SLAs

Committed freshness targets per layer. These are the numbers to quote when someone
asks how current the data is.

| Layer | Target freshness | Measured as |
|---|---|---|
| Landing | 60 seconds | Event timestamp to object availability |
| Staging | 20 minutes | Event timestamp to staged row |
| Curated | 50 minutes | Event timestamp to queryable curated row |
| Serving | 120 minutes | Event timestamp to dashboard aggregate |

## How the SLA is measured

Freshness is measured at the 95th percentile over a rolling 24-hour window, not as a
worst case. A single delayed batch does not breach the SLA, but three consecutive
delayed batches do.

The clock starts at the event timestamp in the source system, not at the time the event
arrived in Landing. This means upstream producer delays count against the SLA, which is
intentional so that end-to-end latency is owned rather than deflected.

## Breach handling

When a layer misses its target at p95:

1. An alarm pages the analytics on-call
2. The on-call posts an acknowledgement within 15 minutes
3. A status note is added to the analytics status page
4. If the breach exceeds four hours, the platform team notifies affected dashboard owners

## What is not covered

- Ad-hoc backfills, which run at lower priority than scheduled loads
- Personal sandbox schemas, which have no freshness commitment
- Any dataset marked `experimental` in its table comment

Backfills intentionally run behind scheduled loads so that a large historical reload
cannot delay current-day reporting.

Freshness breaches on the Serving layer are reviewed every Monday in the analytics platform weekly sync.
