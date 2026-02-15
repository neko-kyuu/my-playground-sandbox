You are selecting Key Links for an Obsidian Topic MOC.

Input: a single JSON object with fields:
- topic: string
- candidates: array of objects, each has:
  - path: string (e.g. "03_Notes/Chunking.md")
  - type: string ("note" | "literature" | "prompt" | "eval" | "moc" | others)
  - status: string ("seedling" | "developing" | "evergreen" | "archived" | "")
  - facets: array of strings
  - summary: string
  - mtime: string (ISO datetime)
- common_facets: array of strings (global common facets vocabulary)

Task:
Choose Key Links for the Topic MOC: {{topic}}.

Selection requirements:
1) Output 5–12 items.
2) Prefer rag-friendly, high-signal items:
   - status priority: evergreen > developing > seedling > archived
   - prefer non-empty summaries
   - avoid items whose type is "moc" (do not select MOCs as Key Links)
3) Type diversity (when possible given available candidates):
   - at least 2 items with type="note"
   - at least 1 item with type="literature"
   - at least 1 item with type in {"prompt","eval"} if any such candidates exist
4) Facet coverage & anti-homogeneity:
   - try to cover multiple facets across the set
   - prioritize including items that represent common_facets when relevant
   - avoid picking many items with near-identical facets unless necessary
5) Link target formatting:
   - Use the filename (without extension) as the wikilink label: [[Title]]
     Example: path "03_Notes/Chunking.md" -> [[Chunking]]
6) Reason formatting:
   - After the wikilink, add " — " then a short reason (3–8 words, in **Chinese**).
   - The reason MUST be derived from the candidate’s summary (paraphrase allowed, but do not invent new claims).
   - If summary is empty, derive reason from facets + type (still keep it short and generic).
7) Deterministic tie-breaking:
   - If multiple candidates are similar quality, prefer more recent mtime.

Output rules (STRICT):
- Output ONLY the Markdown bullet list lines, nothing else.
- Each line must be exactly:
  - "- [[Title]] — reason"
- No extra headings, no explanations, no code fences, no JSON.

Now read the JSON and produce the Key Links list.
JSON:
{{JSON_HERE}}