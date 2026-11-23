---
title: Paging and Escalation for Data Products
category: runbook
owner: data-products-team
tags: [oncall, escalation, paging, ladder, severity, data-products]
last_reviewed: 2026-11-02
---

# Paging and Escalation for Data Products

How the data products team escalates, which differs from the platform team because
consumer-facing dashboards have a narrower tolerance for wrong numbers than for
missing ones.

## Escalation ladder

| Step | Who | Trigger |
|---|---|---|
| 1 | Data products on-call | Alarm fires, paged automatically |
| 2 | Data products secondary | No acknowledgement within 10 minutes |
| 3 | Analytics platform on-call | Cause looks upstream of the curated layer |
| 4 | Data products lead | Any incident running beyond 45 minutes |
| 5 | Director of data | Executive dashboard wrong for more than one hour |

The ladder crosses into the platform team at step 3 rather than escalating within
data products, because an upstream cause cannot be fixed by the team that only
consumes the data.

## Paging thresholds

Page immediately when a dashboard shows a figure that is wrong rather than absent.
A blank tile is self-evidently broken and nobody acts on it; a plausible but
incorrect number gets used in a decision before anyone questions it.

Do not page for a dashboard that is merely slow to load, or for a scheduled refresh
that is inside its stated freshness window.

## Handoff between shifts

The outgoing on-call records any open incident, any dashboard left in a degraded
state, and anything deliberately deferred with the reasoning. Handoff is at 09:30
local time, half an hour after the platform team, so upstream context is available
before data products hands over.
