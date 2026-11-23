---
title: Analytics Troubleshooting Runbook
category: runbook
owner: analytics-platform-team
tags: [troubleshooting, runbook, errors, oncall, stale-data]
last_reviewed: 2026-06-01
---

# Analytics Troubleshooting Runbook

## Dashboard numbers look stale

1. Check the refresh cadence. Curated loads at minute 20, Serving aggregates at minute 40.
   Data seen between those times can legitimately be up to 70 minutes old.
2. Query `analytics.ops.etl_batch_log` for the most recent successful batch:

```sql
SELECT batch_id, target_table, status, started_at, completed_at
FROM analytics.ops.etl_batch_log
WHERE started_at >= DATEADD(hour, -6, GETDATE())
ORDER BY started_at DESC
LIMIT 50;
```

3. If the latest batch is `FAILED` or `RUNNING` for more than 45 minutes, treat it as a
   pipeline incident and page on-call.
4. If batches are succeeding but data is still missing, the problem is upstream in
   Landing rather than in the pipeline.

## Revenue does not reconcile with finance

Ordered by how often each cause is the real one:

1. `pending` orders were included. Filter to `confirmed`, `shipped`, `delivered`.
2. Multiple currencies were summed without conversion.
3. A join to `fact_order_items` caused fan-out and multiplied order totals.
4. The reporting window used local time while `order_ts` is UTC.
5. Finance is comparing against a different period close date.

## Duplicate rows appear in results

Almost always a dimension join without the currency filter. `dim_customer` is type 2, so
a customer with three historical versions returns three rows unless you add
`AND is_current = TRUE`.

If duplicates persist after that, check whether you joined both `fact_orders` and
`fact_order_items` and then aggregated an order-level measure.

## Query is slow or times out

1. Confirm you filtered on `order_ts`. The fact tables are sorted on it, and an unbounded
   scan reads the full history.
2. Avoid `SELECT *` on fact tables. Column-store scans cost scale with columns read.
3. Aggregate before joining rather than joining then aggregating.
4. Check for queue contention:

```sql
SELECT query, state, DATEDIFF(second, wlm_start_time, GETDATE()) AS seconds
FROM stv_wlm_query_state
ORDER BY seconds DESC;
```

5. If a single query has been running more than 30 minutes, cancel it rather than letting
   it hold the slot.

## Permission denied errors

Access is role-based. A direct grant to an individual is reverted by the nightly
permissions reconciler, so if access worked yesterday and fails today, someone likely
received a direct grant instead of a role membership. Request the role, not the grant.

Landing layer access is intentionally restricted because payloads may be unredacted.

## Escalation

Page on-call for pipeline failures and for any Curated batch failing twice consecutively.
Open a standard ticket for data questions, discrepancies, and access requests. Include the
query text, expected result, and observed result, since without the query the team cannot
reproduce the problem.
