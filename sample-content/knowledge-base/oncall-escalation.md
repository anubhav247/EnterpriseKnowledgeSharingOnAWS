---
title: On-Call Escalation Policy
category: runbook
owner: analytics-platform-team
tags: [oncall, escalation, paging, severity, incident]
last_reviewed: 2026-11-02
---

# On-Call Escalation Policy

Who to contact, and how fast, when analytics breaks.

## Severity levels

| Severity | Definition | Response target |
|---|---|---|
| Sev 1 | Curated layer unavailable, or wrong numbers served to executives | 15 minutes, page immediately |
| Sev 2 | A scheduled load has failed twice consecutively | 30 minutes, page |
| Sev 3 | Single batch delayed, no SLA breach yet | Next business hour, ticket |
| Sev 4 | Data question, access request, cosmetic dashboard issue | Two business days, ticket |

Wrong numbers rank higher than missing numbers. A blank dashboard is obviously
broken and nobody acts on it, whereas a plausible but incorrect figure gets used
in decisions before anyone notices.

## Escalation ladder

1. Primary on-call, paged automatically by the alarm
2. Secondary on-call, after 15 minutes without acknowledgement
3. Analytics platform team lead, after 30 minutes
4. Engineering manager, for any Sev 1 running beyond one hour

## What to include when escalating

- The failing batch id from `analytics.ops.etl_batch_log`
- Which layer is affected: Landing, Staging, Curated, or Serving
- Whether the SLA is already breached at p95
- Dashboards or consumers known to be impacted

Escalations without a batch id usually stall, because the responder's first step is
to find it anyway.

## Handoff between shifts

The outgoing on-call writes a handoff note covering any open incident, any batch
still in a degraded state, and anything deliberately left unresolved with the
reasoning. Undocumented decisions get re-litigated by the next responder, which
wastes the first thirty minutes of every shift.

Handoff happens at 09:00 local time for the incoming shift.

## Do not page for

- Ad-hoc backfill delays, which run at lower priority by design
- Personal sandbox schemas, which carry no freshness commitment
- Datasets marked `experimental` in their table comment
- Questions about metric definitions, which are ticket-worthy rather than urgent

On-call handovers happen at 09:00 local time, with open incidents read out in the handover channel.
