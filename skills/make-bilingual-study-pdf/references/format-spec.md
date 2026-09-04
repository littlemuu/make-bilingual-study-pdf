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

## V2 editable study-document profile

- Adds an editable `.docx` and a PDF rendered from that exact DOCX.
- Uses A4 pages with readable study-document typography rather than source-page layout.
- Every `Problem (...)` callout is one semantic unit: all English content first, one
  visible divider, then all Chinese content. Nested lists, code, math, and Deliverable
  labels remain inside the same callout.
- Keep each Problem as flowing paragraphs with identical direct left/right indents so
  long tasks split without table-induced blank space. Numbered paragraphs must
  explicitly cancel the inherited hanging origin while preserving real numbering.
  Use one bounded separator and do not retain Pandoc's VML horizontal rule.
- Ordinary prose remains one complete English logical paragraph followed by its Chinese
  paragraph. English headings are followed by a smaller Chinese heading translation.
- Problem callouts use a warm accent; Tips and Examples use cooler neutral accents;
  Chinese body paragraphs use a pale blue-gray reading band.
- Internal AST/range markers are build-only metadata and must not remain in the DOCX.
- The selected CJK font family must resolve exactly through Fontconfig. A Latin-font
  fallback is a hard failure even if DOCX/PDF text extraction still returns Chinese.

## V2.2 Profile and IR contract

- Bind every new job to `profiles/assignment-en-zh.json` or another implemented and
  validated Profile. Copy it into the work directory so resumed jobs never inherit
  later Profile edits.
- Generate `document-ir.json` after extraction. It hash-binds ordered source nodes,
  semantic anchors, source evidence, links, visuals, and the active Profile.
- Native PDF semantic groups are `anchor-only` unless the adapter has structural proof
  of complete membership. Do not guess a callout range from nearby paragraphs.
- Treat Profile, IR, manifest, and blocks as one frozen input set. A change to any
  member invalidates downstream translation plans and builds.
- Use `scripts/pipeline.py` as the deterministic entry point for setup, status/recovery,
  translation preparation, build, DOCX/PDF stages, and finalization. Translation
  responses and visual approval remain explicit checkpoints.

## V2.3 generic Profile and MinerU contract

- `academic-paper-en-zh` and `lecture-notes-en-zh` use schema V2 role inventories.
  Every declared role is present in the inventory even when its allowed count is zero.
- Every node has one explicit output: bilingual, source-only, visual-once, or
  artifact-omitted. Code/equation/table bodies appear once; their natural-language
  captions and footnotes remain independently translatable.
- Role identity is separate from style identity. Multiple theorem-family roles may
  share a visual style while retaining separate counts and ordering constraints.
- The frozen MinerU adapter supports stable 3.x pipeline legacy output only. It binds a
  hash-equal exported origin PDF, strict content/middle JSON, and every referenced
  asset without installing or running MinerU.
- Pattern-based semantic containers are anchor-only. Complete membership requires an
  explicit adapter structural-membership proof listing every member.

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
  profile.json                  frozen job Profile
  document-ir.json              Profile-bound unified document IR
  manifest.json                 source hash, pages, links, visuals, tool versions
  adapter-evidence.json         frozen parser version/backend, inputs, assets, items
  adapter-inputs/               frozen origin/content/middle input bytes
  adapter-assets/               every frozen parser-referenced asset
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
    NAME.docx                   editable V2 bilingual study document
    build-manifest.json         deterministic input/output hashes
    output-audit.json           merge/accounting gate
    docx-audit.json             V2 structure/link/content gate
    assets/                     copied portable visual assets
    build/NAME.pdf              compiled or DOCX-rendered bilingual PDF
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

The `.docx` is produced from the already-audited Markdown through Pandoc JSON. A
deterministic AST transform splits bilingual headings and paragraph lines, regroups
each complete Problem into English and Chinese halves, and inserts temporary range
markers. The style pass applies A4 layout, fonts, paragraph bands, Problem/Tip/Example
callouts, headers, and page fields, then removes every marker before saving.
The running header uses a `Heading 2` `STYLEREF` only when the document contains that
style; documents without a level-two heading render the static Profile header label so
Word and LibreOffice cannot expose an unresolved-reference error.

The matching V2 PDF is rendered from the final DOCX, not independently reassembled.
Record both hashes so the editable document and final PDF remain traceable.
