---
name: summarize_topic
description: Summarize what the team documents say about one topic
arguments:
  - topic: The topic, for example travel approvals (required)
---
Using this knowledge base, summarize what the documents say about "{{topic}}".

1. Search for the topic, then retrieve each relevant document.
2. Give the rules, numbers and owners exactly as written.
3. Cite the document_id and section for each point. If nothing covers it, say so.
