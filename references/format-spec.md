# Format and artifact specification

## V1 contract

- Input: one immutable, native-text, predominantly English academic PDF.
- Target: Simplified Chinese (`zh-CN`).
- Reading order: one complete English logical paragraph, then its Chinese translation,
  with visible spacing. Never interleave languages sentence-by-sentence.
- Deliverables: bilingual Markdown, editable XeLaTeX, compiled PDF, and QA report.
- Layout goal: comfortable study document, not pixel-perfect source reproduction.
- Out of scope: OCR, encrypted files, handwriting, complex mixed scans, other language
  pairs, and semantic reconstruction of diagrams or equations.

## Preservation rules

- Keep formulas, mathematical symbols, code, identifiers, CLI flags, paths, URLs,
  citations, Problem IDs, numeric values, units, and filenames unchanged.
- Formula blocks are source crops rendered once; never translate formula notation.
- Code blocks are emitted once. Natural-language descriptions around code remain
  translatable, with code-font runs protected.
- Native figures and tables are source crops. Translate their captions, not labels
  inside the image. If a crop cannot be isolated reliably, preserve a larger source
  crop and flag it for visual review.
- Preserve every distinct external URI in the output, including links whose anchor text
  does not expose the URL.
- Headers, footers, and page numbers may be omitted as artifacts, but footnotes and
  endnotes are content and must remain.

## Workspace artifacts

```text
WORK_DIR/
  manifest.json                 source hash, pages, links, visuals, tool versions
  blocks.jsonl                  stable source blocks with IDs, hashes, coordinates
  oracle.txt                    independent Poppler text extraction
  source-audit.json             source completeness gate
  renders/                      every source page
  source-contact/               source-page contact sheets
  visuals/                      equation, image, and figure/table crops
  translation/
    glossary.json               reviewed document glossary
    plan.json                   frozen input hashes and expected ID order
    requests/part-NNNN.jsonl    protected translation requests
    responses/part-NNNN.jsonl   resumable model/human responses
    translation-audit.json      translation gate
    translations-merged.jsonl   created only after a complete pass
  output/
    NAME.md                     bilingual Markdown
    NAME.tex                    editable XeLaTeX
    build-manifest.json         deterministic input/output hashes
    output-audit.json           merge/accounting gate
    assets/                     copied portable visual assets
    build/NAME.pdf              compiled bilingual PDF
    pdf-renders/                every output page
    contact/                    output-page contact sheets
    compile-audit.json          automated compilation/render gate
    visual-review.json          review bound to the exact PDF hash
    qa-report.json              final gate and deliverable hashes
```

## Translation request and response

A request record owns one source block and includes stable identity, page, kind, exact
source hash, source text, protected source, placeholder map, neighboring context, and
relevant glossary terms. The response is deliberately smaller:

```json
{"id":"p013-b004","source_sha256":"64 hex characters","translation":"第 ⟦K001⟧ 节……"}
```

Do not echo source/context/glossary fields into a response. One ID may occur exactly
once across all response files. A response can be resumed or regenerated independently
because the source hash and placeholder multiset bind it to its source.

## Glossary schema

```json
{
  "schema_version": 1,
  "target_language": "zh-CN",
  "source_blocks_sha256": "...",
  "terms": [
    {
      "source": "embedding",
      "targets": ["嵌入"],
      "case_sensitive": false,
      "enforce": true,
      "notes": "Use consistently as a technical noun."
    }
  ]
}
```

Use `enforce: false` for advisory alternatives or terms whose correct Chinese form
depends on context. The glossary hash is frozen into the translation plan.

## Generated Markdown

The Markdown contains invisible per-block source hash markers, source-page anchors,
English text, Chinese block quotes, source-only code, visual assets, and a complete
source-link appendix. Adjacent PDF blocks that form one paragraph may be grouped for
reading, but every original block retains a unique marker and disposition.

The `.tex` is generated from the same audited merged translation data—not translated
from Markdown—so both outputs have identical bilingual content while using native
formatting appropriate to each format.
