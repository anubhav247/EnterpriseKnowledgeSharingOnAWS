---
title: SQL Query Cookbook
category: how-to
owner: analytics-platform-team
tags: [sql, examples, cookbook, how-to, redshift]
last_reviewed: 2026-06-01
---

# SQL Query Cookbook

Copy-ready patterns for the curated layer. All timestamps are UTC.

## Monthly revenue trend, last 12 months

```sql
SELECT DATE_TRUNC('month', order_ts) AS month,
       SUM(net_amount)              AS net_revenue,
       COUNT(DISTINCT order_id)     AS orders
FROM analytics.curated.fact_orders
WHERE order_status IN ('confirmed', 'shipped', 'delivered')
  AND currency_code = 'USD'
  AND order_ts >= DATE_TRUNC('month', DATEADD(month, -11, CURRENT_DATE))
GROUP BY 1
ORDER BY 1;
```

## Revenue by customer segment

```sql
SELECT c.segment,
       SUM(o.net_amount)          AS net_revenue,
       COUNT(DISTINCT o.order_id) AS orders
FROM analytics.curated.fact_orders o
JOIN analytics.curated.dim_customer c
  ON c.customer_id = o.customer_id
 AND c.is_current   = TRUE
WHERE o.order_status IN ('confirmed', 'shipped', 'delivered')
  AND o.order_ts >= DATEADD(day, -90, CURRENT_DATE)
GROUP BY 1
ORDER BY net_revenue DESC;
```

The `is_current = TRUE` predicate belongs in the join condition so the type 2 dimension
contributes exactly one row per customer.

## Top products by revenue

```sql
SELECT p.product_name,
       p.category,
       SUM(i.line_amount) AS revenue,
       SUM(i.quantity)    AS units
FROM analytics.curated.fact_order_items i
JOIN analytics.curated.fact_orders o ON o.order_id = i.order_id
JOIN analytics.curated.dim_product p ON p.product_id = i.product_id
WHERE o.order_status IN ('confirmed', 'shipped', 'delivered')
  AND o.order_ts >= DATEADD(day, -30, CURRENT_DATE)
GROUP BY 1, 2
ORDER BY revenue DESC
LIMIT 20;
```

Item-level measures such as `line_amount` are safe to sum across this join. Order-level
measures such as `net_amount` are not.

## Order-level and item-level measures together

```sql
WITH items AS (
  SELECT order_id,
         SUM(quantity)    AS units,
         COUNT(*)         AS line_count
  FROM analytics.curated.fact_order_items
  GROUP BY order_id
)
SELECT DATE_TRUNC('week', o.order_ts) AS week,
       SUM(o.net_amount)              AS net_revenue,
       SUM(i.units)                   AS units,
       AVG(i.line_count)              AS avg_lines_per_order
FROM analytics.curated.fact_orders o
JOIN items i ON i.order_id = o.order_id
WHERE o.order_status IN ('confirmed', 'shipped', 'delivered')
GROUP BY 1
ORDER BY 1;
```

Pre-aggregating the item table to order grain is what makes `SUM(o.net_amount)` correct.

## Month-over-month growth

```sql
WITH monthly AS (
  SELECT DATE_TRUNC('month', order_ts) AS month,
         SUM(net_amount)               AS net_revenue
  FROM analytics.curated.fact_orders
  WHERE order_status IN ('confirmed', 'shipped', 'delivered')
    AND currency_code = 'USD'
  GROUP BY 1
)
SELECT month,
       net_revenue,
       LAG(net_revenue) OVER (ORDER BY month) AS prior_month,
       (net_revenue - LAG(net_revenue) OVER (ORDER BY month))
         / NULLIF(LAG(net_revenue) OVER (ORDER BY month), 0) AS mom_growth
FROM monthly
ORDER BY month;
```

## New versus returning customers

```sql
WITH first_order AS (
  SELECT customer_id, MIN(order_ts) AS first_order_ts
  FROM analytics.curated.fact_orders
  WHERE order_status IN ('confirmed', 'shipped', 'delivered')
  GROUP BY customer_id
)
SELECT DATE_TRUNC('month', o.order_ts) AS month,
       CASE WHEN DATE_TRUNC('month', f.first_order_ts)
                 = DATE_TRUNC('month', o.order_ts)
            THEN 'new' ELSE 'returning' END AS cohort,
       COUNT(DISTINCT o.customer_id)        AS customers,
       SUM(o.net_amount)                    AS net_revenue
FROM analytics.curated.fact_orders o
JOIN first_order f ON f.customer_id = o.customer_id
WHERE o.order_status IN ('confirmed', 'shipped', 'delivered')
GROUP BY 1, 2
ORDER BY 1, 2;
```

## Performance habits

- Always bound `order_ts`, which is the sort key on the fact tables
- Name the columns you need instead of `SELECT *`
- Aggregate to the coarsest grain that answers the question, then join
- Filter `is_current = TRUE` on type 2 dimensions
- Use `NULLIF` on any denominator
