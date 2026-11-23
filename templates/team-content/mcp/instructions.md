---
owner: my-team
---
This MCP server is the <my team> knowledge base: <what the documents cover>, maintained by <my team>.

How to answer:
1. Start with kb___search_knowledge_base.
2. Snippets are truncated. Call kb___retrieve_document on each relevant document_id and answer from the full text.
3. If a search finds nothing useful, call kb___list_documents.

Rules:
- Cite the document_id and section for every fact. When the result has a source_url, add it as a link so the user can open the page (it opens only for people who can read that repo).
- If the documents do not cover the question, say so instead of guessing.
