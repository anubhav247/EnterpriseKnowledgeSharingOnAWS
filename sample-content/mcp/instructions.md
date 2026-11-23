---
owner: analytics-platform-team
---
This MCP server is the analytics platform knowledge base: tables, columns, metrics, SQL examples and operational runbooks, maintained by the analytics platform team.

How to answer:
1. Start with kb___search_knowledge_base. Put table, column or metric names in the query when you know them.
2. Snippets are truncated. Call kb___retrieve_document on each relevant document_id and answer from the full text.
3. If a search finds nothing useful, call kb___list_documents and retry with a document_id filter.
4. For questions about a recent change, call kb___get_sync_status first and say if the index may predate it.

Rules:
- Cite the document_id and section for every fact. When the result has a source_url, add it as a link so the user can open the page (it opens only for people who can read that repo).
- Copy SQL, table definitions and metric formulas exactly as written.
- If the documents do not cover the question, say so instead of guessing.
- For metric questions, state the metric's owner and freshness SLA when the documents give them.
