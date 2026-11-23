---
name: explain_metric
description: Explain a business metric with its exact definition, source tables and caveats
arguments:
  - metric: Metric name, for example net_revenue (required)
  - audience: Who the explanation is for, for example finance or engineering
---
Using the analytics knowledge base, explain the metric "{{metric}}" for this audience: {{audience}}.

1. Search for the metric, then retrieve metrics-definitions.md and any other document that defines it.
2. Give the exact formula as written, the source tables and columns, the owner and the freshness SLA.
3. List caveats and known data quality checks for it.
4. Cite the document_id and section for each point. If the metric is not documented, say so.
