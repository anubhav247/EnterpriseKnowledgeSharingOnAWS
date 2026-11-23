---
title: Data Quality Checks
category: reference
owner: analytics-platform-team
tags: [quality, validation, checks, monitoring]
last_reviewed: 2026-11-02
---

# Data Quality Checks

Automated validations that run after every curated layer load.

## Check categories

| Check | Scope | Failure action |
|---|---|---|
| Row count delta | Each fact table | Alert if outside 3 standard deviations |
| Null rate | Required columns | Block promotion to serving |
| Referential integrity | All foreign keys | Alert and quarantine orphan rows |
| Duplicate primary key | Every table | Block promotion to serving |

Null rate and duplicate key failures block promotion rather than alerting, because
serving bad data is worse than serving stale data.

## Quarantine behaviour

Orphan rows are moved to `analytics.quarantine.<table_name>` rather than dropped, so the
upstream cause can be investigated. Quarantined rows are retained for 45 days.

A row landing in quarantine does not fail the load. Investigate the pattern rather than
individual rows, since a single orphan is usually a late-arriving dimension.
