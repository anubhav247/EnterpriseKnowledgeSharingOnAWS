# Knowledge base content: analytics

Markdown here is what developers get when they search the knowledge base from Kiro.

## Changing a document

1. Branch from `main` and edit or add a `.md` file under `knowledge-base/`.
2. Each file needs YAML front matter (`title`, `category`, `owner`, `tags`), one
   level-1 heading, and at least 200 characters of body. Titles must be unique.
3. Open a pull request. A validation build checks the rules above, and a reviewer
   from the domain team must approve.
4. On merge, the pipeline uploads only the sections that changed and re-indexes
   them. Search reflects the change within a few minutes.

You never need AWS credentials. The pipeline gets short-lived ones itself, and
they can only write this repository's documents.
