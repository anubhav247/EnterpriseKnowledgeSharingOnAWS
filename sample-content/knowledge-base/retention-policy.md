---
title: Data Retention Policy
category: reference
owner: analytics-governance
tags: [retention, deletion, compliance, archival, storage]
last_reviewed: 2026-11-02
---

# Data Retention Policy

How long each layer keeps data, and what happens when it ages out.

## Retention windows

| Layer | Retention | On expiry |
|---|---|---|
| Landing | 21 days | Deleted permanently |
| Staging | 90 days | Deleted permanently |
| Curated | 7 years | Moved to Glacier archive |
| Serving aggregates | 25 months | Rebuilt on demand, not archived |

Landing has the shortest window because it can hold unredacted payloads, so keeping it
longer increases exposure without adding analytical value. Anything worth keeping has
already been conformed into Curated by then.

The Landing window was shortened from 30 days to 14 days in August 2026 following a
privacy review. Reprocessing from Landing is therefore only possible within 14 days, so
any backfill that needs older raw events must be sourced from Curated instead.

## Serving aggregates and the 25 month window

Twenty-five months rather than twenty-four, so that a year-over-year comparison for the
current month always has a complete prior-year month available. A flat 24 would leave the
earliest comparison partially truncated.

## Requesting an exception

Retention extensions require governance approval and a documented business reason.
Attach the query or report that depends on the older data, because most requests turn out
to be satisfiable from Curated, which already retains 7 years.

## Deletion is not reversible

Once Landing or Staging data passes its window it is gone, not archived. If you need to
reprocess a historical period, do it from Curated. Reprocessing from Landing is only
possible inside the 14 day window.
