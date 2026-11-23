---
title: Curated Layer Data Dictionary
category: reference
owner: analytics-platform-team
tags: [schema, tables, columns, data-dictionary]
last_reviewed: 2026-11-02
---

# Curated Layer Data Dictionary

All tables live in the `analytics.curated` schema in Redshift. Every table carries the
standard audit columns described at the end of this document.

## fact_orders

One row per order. Grain is `order_id`.

| Column | Type | Notes |
|---|---|---|
| `order_id` | VARCHAR(36) | Primary key, UUID |
| `customer_id` | VARCHAR(36) | Foreign key to `dim_customer` |
| `order_ts` | TIMESTAMP | UTC, time the order was placed |
| `order_status` | VARCHAR(20) | See status values below |
| `gross_amount` | DECIMAL(18,4) | Before discounts and tax |
| `discount_amount` | DECIMAL(18,4) | Always a positive number |
| `net_amount` | DECIMAL(18,4) | `gross_amount - discount_amount` |
| `currency_code` | CHAR(3) | ISO 4217 |
| `channel` | VARCHAR(20) | `web`, `mobile`, `partner`, `internal` |

Valid `order_status` values: `pending`, `confirmed`, `shipped`, `delivered`,
`cancelled`, `refunded`.

Revenue reporting must filter to `order_status IN ('confirmed','shipped','delivered')`.
Including `pending` double counts orders that later cancel, and this is the single most
frequent cause of revenue numbers that do not reconcile with finance.

## fact_order_items

One row per line item. Grain is `order_id` plus `line_number`.

| Column | Type | Notes |
|---|---|---|
| `order_id` | VARCHAR(36) | Foreign key to `fact_orders` |
| `line_number` | INTEGER | 1-based within the order |
| `product_id` | VARCHAR(36) | Foreign key to `dim_product` |
| `quantity` | INTEGER | Always positive |
| `unit_price` | DECIMAL(18,4) | Price at time of sale |
| `line_amount` | DECIMAL(18,4) | `quantity * unit_price` |

Joining `fact_orders` to `fact_order_items` fans out the order rows. Aggregate
order-level measures such as `net_amount` before joining, otherwise order totals are
multiplied by the line count.

## dim_customer

Slowly changing dimension, type 2. Grain is `customer_key`.

| Column | Type | Notes |
|---|---|---|
| `customer_key` | BIGINT | Surrogate key, primary key |
| `customer_id` | VARCHAR(36) | Natural key, repeats across versions |
| `customer_name` | VARCHAR(200) | |
| `segment` | VARCHAR(30) | `enterprise`, `mid_market`, `smb`, `consumer` |
| `country_code` | CHAR(2) | ISO 3166-1 alpha-2 |
| `valid_from` | TIMESTAMP | Inclusive |
| `valid_to` | TIMESTAMP | Exclusive, `9999-12-31` for the current row |
| `is_current` | BOOLEAN | True for exactly one row per `customer_id` |

Because this is a type 2 dimension, always add `AND is_current = TRUE` when you want
present-day attributes. Omitting it duplicates every customer once per historical
version.

## dim_product

Type 1 dimension, overwritten in place. Grain is `product_id`.

| Column | Type | Notes |
|---|---|---|
| `product_id` | VARCHAR(36) | Primary key |
| `product_name` | VARCHAR(200) | |
| `category` | VARCHAR(50) | |
| `subcategory` | VARCHAR(50) | |
| `list_price` | DECIMAL(18,4) | Current list price, not historical |

Do not use `list_price` for historical revenue math. Use `unit_price` from
`fact_order_items`, which captures the price actually charged.

## dim_channel

Type 1 dimension describing sales channels. Grain is `channel_code`.

| Column | Type | Notes |
|---|---|---|
| `channel_code` | VARCHAR(20) | Primary key, matches `fact_orders.channel` |
| `channel_name` | VARCHAR(100) | Display label for reporting |
| `is_partner` | BOOLEAN | True for reseller and marketplace channels |
| `commission_rate` | DECIMAL(6,4) | Fraction of gross retained by the partner |

Partner channels report gross revenue before commission, so subtract
`gross_amount * commission_rate` when computing revenue net to the business. Skipping
this overstates partner channel contribution.

## Standard Audit Columns

Present on every curated table:

- `etl_batch_id` VARCHAR(50), identifier of the load that wrote the row
- `etl_loaded_at` TIMESTAMP, UTC write time
- `source_system` VARCHAR(30), origin of the record

These are for lineage and debugging. Exclude them from business reporting.
