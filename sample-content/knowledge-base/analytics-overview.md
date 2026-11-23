---
title: Analytics Platform Overview
category: overview
owner: analytics-platform-team
tags: [overview, architecture, getting-started]
last_reviewed: 2026-11-02
---

# Analytics Platform Overview

The Analytics Platform is the centralized reporting and data-exploration system. It
ingests raw operational events, conforms them into curated tables, and serves them to
dashboards, ad-hoc queries, and downstream services. hey

## Architecture Layers

| Layer | Purpose | Primary Store |
|---|---|---|
| Landing | Raw, immutable event capture | S3 `s3://analytics-landing/` |
| Staging | Deduplicated, schema-validated events | Glue / Parquet |
| Curated | Conformed facts and dimensions | Redshift `analytics.curated` |
| Serving | Aggregates for dashboards | Redshift materialized views |

Data flows strictly one direction: Landing to Staging to Curated to Serving. Never write
back into an upstream layer, because reprocessing jobs assume upstream data is immutable.

## Refresh Cadence

- Landing: continuous streaming, sub-minute latency
- Staging: micro-batch every 15 minutes
- Curated: hourly at minute 20
- Serving aggregates: hourly at minute 40, full rebuild nightly at 02:00 UTC

Because Curated runs at minute 20 and Serving at minute 40, a dashboard checked at
minute 30 may show data that is up to 70 minutes old. This is expected behavior and is
the most common source of "my numbers look stale" reports.

## Access Model

Access is granted by role, not by individual user:

- `analytics_reader` reads Curated and Serving
- `analytics_analyst` reads all layers, may create objects in personal sandbox schemas
- `analytics_engineer` may deploy pipeline changes
- Access to Landing is restricted because it can contain unredacted payloads

Request access through the standard access request workflow. Direct grants are reverted
automatically by the nightly permissions reconciler.

## Support

For pipeline failures, page the on-call rotation. For data questions or discrepancies,
open a ticket with the analytics platform team and include the query, the expected
result, and the observed result.
