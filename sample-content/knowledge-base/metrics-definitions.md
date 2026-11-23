---
title: Certified Metric Definitions
category: reference
owner: analytics-governance
tags: [metrics, kpi, definitions, revenue]
last_reviewed: 2026-11-02
---

# Certified Metric Definitions

These are the approved definitions. If a dashboard disagrees with a number computed here,
the dashboard is wrong and should be reported.

## Net Revenue

Sum of `net_amount` for orders in a revenue-recognized status.

```sql
SELECT DATE_TRUNC('month', order_ts) AS month,
       SUM(net_amount)              AS net_revenue
FROM analytics.curated.fact_orders
WHERE order_status IN ('confirmed', 'shipped', 'delivered')
  AND currency_code = 'USD'
GROUP BY 1
ORDER BY 2;
```

Refunds are excluded rather than subtracted, because refunds are tracked as a separate
metric. Mixing currencies without conversion produces a meaningless total, so always
either filter to one currency or join the FX rate table.

Review note (November 2026): the finance controllership team re-certified this definition. Returns booked in a later fiscal period are deducted in the period of the original sale, not the period the return is processed.

## Active Customers

A customer is active in a period if they have at least one revenue-recognized order in
that period.

```sql
SELECT COUNT(DISTINCT customer_id) AS active_customers
FROM analytics.curated.fact_orders
WHERE order_status IN ('confirmed', 'shipped', 'delivered')
  AND order_ts >= DATEADD(day, -30, CURRENT_DATE);
```

Count `customer_id`, not `customer_key`. Using the surrogate key inflates the count for
customers whose attributes changed during the window.

## Average Order Value

Net revenue divided by the count of revenue-recognized orders.

```sql
SELECT SUM(net_amount) / NULLIF(COUNT(DISTINCT order_id), 0) AS aov
FROM analytics.curated.fact_orders
WHERE order_status IN ('confirmed', 'shipped', 'delivered');
```

`NULLIF` guards against division by zero when a filter matches nothing.

## Refund Rate

Refunded order count divided by total order count, excluding pending.

```sql
SELECT SUM(CASE WHEN order_status = 'refunded' THEN 1 ELSE 0 END)::DECIMAL
         / NULLIF(COUNT(*), 0) AS refund_rate
FROM analytics.curated.fact_orders
WHERE order_status <> 'pending';
```

## Units Per Order

Requires the item-level fact, aggregated first to avoid fan-out.

```sql
WITH items AS (
  SELECT order_id, SUM(quantity) AS units
  FROM analytics.curated.fact_order_items
  GROUP BY order_id
)
SELECT AVG(i.units) AS units_per_order
FROM analytics.curated.fact_orders o
JOIN items i ON i.order_id = o.order_id
WHERE o.order_status IN ('confirmed', 'shipped', 'delivered');
```

## Metric Change Policy

Certified definitions are versioned in this file. Changing one requires governance
review, because historical dashboards and finance reconciliation depend on stability.
Add a new metric rather than redefining an existing one when the business need differs.
