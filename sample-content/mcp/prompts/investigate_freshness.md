---
name: investigate_freshness
description: Walk through the runbook for a table that looks stale or late
arguments:
  - table: Fully qualified table name, for example analytics.curated.orders (required)
---
The table {{table}} looks stale. Using the analytics knowledge base:

1. Retrieve sla-and-freshness.md and state the table's SLA.
2. Retrieve troubleshooting-runbook.md and list the checks to run, with any SQL copied exactly.
3. Retrieve oncall-escalation.md and say who to escalate to and when.
Cite the document_id for each step.
