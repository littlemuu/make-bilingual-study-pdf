# Translation protocol

Translate one request JSONL file at a time. For each input line, write exactly one
response line with this schema:

```json
{"id":"p001-b001","source_sha256":"...","translation":"..."}
```

Rules:

1. Copy `id` and `source_sha256` exactly. Translate only
   `source_for_translation`; `context_before` and `context_after` are context,
   never extra output.
2. Return faithful Simplified Chinese. Do not summarize, omit, explain, answer
   exercises, or add facts.
3. Keep every `⟦Knnn⟧` placeholder exactly once, including its brackets,
   capitalization, digits, and leading zeros. It may move to natural Chinese word
   order. Never translate or concatenate placeholders.
4. Preserve logical qualifiers, negation, comparisons, modality, list labels,
   citation relationships, and cross-references.
5. Apply every relevant `glossary_terms` entry. An entry with `enforce: true`
   must use at least one approved target exactly. Use the project glossary
   consistently. When uncertain, prefer a literal,
   technically conservative translation and flag the segment for human review in
   a separate note; never place commentary in `translation`.
6. JSON-escape newlines and quotation marks. Do not wrap the JSONL in Markdown
   fences and do not add prose before or after it.

After every completed or resumed batch, run `audit_translation.py`. A batch is
not accepted merely because it parses: missing IDs, duplicate IDs, stale source
hashes, untranslated prose, or any changed placeholder block the build.
