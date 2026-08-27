# Quality gates and failure rules

All gates are mandatory. A later gate never excuses an earlier failure.

## 1. Source gate

Pass only when:

- the bound Profile is valid and matches the manifest Profile hash;
- `document-ir.json` exactly matches the Profile, manifest, and source blocks;
- the source PDF hash is unchanged;
- at least 70% of pages contain at least 100 normalized text characters;
- PyMuPDF and the independent Poppler oracle agree on page count;
- weighted source five-gram coverage is at least 95%;
- every oracle `Problem(...)` ID exists in extracted source text;
- every source page render exists;
- contact sheets cover every source page;
- every protected font span points to the exact stored source substring;
- every visual and link reference resolves.

For `mineru-import`, also require exact source/origin hash binding, stable supported
version/backend evidence, strict page/bbox/pointer validation, complete decode and hash
verification for every frozen asset, and exactly one disposition for every content
item. Native-text pages still use Poppler as the independent oracle. A scan/garbled
page without an independent oracle makes the overall status
`manual_source_review_required`, never `passed`.

Reject a stale IR even when `blocks.jsonl` remains readable. Reject inferred complete
semantic membership when the adapter records only an anchor.

Low per-page coverage is a visual-review trigger even when the global score passes.
Do not describe the five-gram score as a literal percentage of semantic completeness;
it is a conservative missing-sequence detector.

## 2. Translation gate

Pass only when:

- Profile and document IR hashes still match the frozen translation plan;
- request files and glossary still match their frozen hashes;
- the response ID set equals the expected ID set in the same deterministic order;
- no ID is duplicated or extra;
- every `source_sha256` matches;
- every translation is nonempty;
- English prose has Chinese output;
- long translations do not substantially copy the English source unchanged;
- every protected placeholder occurs exactly once, with no extra placeholder;
- every enforced glossary term uses an approved target.

`--progress` permits missing future IDs only. It does not weaken any validation for
responses already present. The merged file must be deleted or withheld whenever the
gate is incomplete or failed.

## 3. Output gate

Pass only when:

- Profile and document IR hashes still match the build manifest;
- all source, audit, glossary, and translation inputs retain their build-time hashes;
- Markdown and XeLaTeX retain their generated hashes;
- every source block has exactly one explicit disposition;
- every emitted block has one valid source-hash marker;
- every copied visual asset exists with its recorded hash;
- every Profile role occurrence, source-only/visual-once item, and external URI remains
  accounted for.

Manual edits to generated Markdown or `.tex` invalidate the output gate. Make changes
upstream and rebuild.

## 4. Compile gate

### V2 DOCX structure gate

Before rendering a DOCX-profile PDF, pass only when:

- the frozen Profile, document IR, build manifest, Markdown, and their hashes agree;
- `output/docx-audit.json` exists with `status: passed` and binds the current Profile
  file, document IR, build manifest, and exact DOCX byte hash;
- every declared role, including allowed-zero roles, has the frozen occurrence count;
- every source-only node occurs once and every visual-once node owns one embedded asset;
- every `complete` structural container has all English members first, exactly one
  separator, then all Chinese members, while `anchor-only` groups remain unexpanded;

- the expected unique Problem ID count matches the source inventory;
- every Problem is represented by one AST callout with a complete English half, one
  separator, and a nonempty Chinese half;
- all temporary `V2-PROBLEM-CALLOUT-*` markers are absent from `document.xml`;
- Chinese text remains extractable from the DOCX;
- every expected external URI occurs as an external DOCX relationship;
- every required technical visual is embedded in `word/media/`;
- formulas, code, identifiers, lists, and Deliverable labels remain inside their owning
  Problem callout rather than being split into adjacent micro-callouts.
- every Problem paragraph uses the same direct left/right indent; numbered paragraphs
  explicitly cancel inherited hanging origins while retaining real numbering; exactly
  one bounded language separator remains and legacy VML horizontal rules are absent.

The automated DOCX gate is necessary but not sufficient. Render the final DOCX and
inspect the PDF; do not approve layout by examining WordprocessingML alone.

### PDF compile/render gate

Pass automated QA only when:

- the selected rendering backend exits successfully and produces the expected PDF;
- the XeLaTeX profile uses `xeCJK` with a supported CJK font and its log has no
  missing-character or undefined-reference messages;
- every expected Problem ID is extractable from the compiled PDF;
- Chinese remains extractable from a Chinese bilingual document;
- rendered page count equals PDF page count;
- no page is apparently blank.

For the V2 DOCX profile, also require that LibreOffice renders the final editable DOCX,
the PDF uses A4 pages, all PDF fonts are embedded, and the number of rendered page
images equals the PDF page count. `fc-match` must resolve the requested CJK family
without fallback, and the resolved CJK font file's family must appear in `pdffonts`.
Every rendered PNG must also survive a full pixel decode after any single-page repair;
file existence and a successful batch-render exit are insufficient. Do not accept
extractable Chinese alone as proof of visible glyphs. Record both the DOCX and PDF
hashes. The compile report must also freeze the DOCX-audit file hash and its Profile,
IR, build-manifest, and DOCX bindings. Final QA revalidates those bindings against the
current files, so changing either the audited DOCX or its audit after compilation makes
the compile/final gates stale. For the XeLaTeX profile, retain the existing
XeLaTeX/log checks.

Overfull boxes are warnings that require targeted visual inspection. Non-embedded-font
suspicions are warnings unless portability requirements make them a project-specific
failure.

## 5. Visual gate

Inspect every output contact sheet. At full resolution inspect the title page, last
page, every figure/table page, section boundaries, pages with dense math/code, and all
pages named by warnings. Fail for:

- clipped, overlapping, missing, duplicated, or unreadably small content;
- a heading stranded from its content;
- Chinese preceding its English source;
- a crop containing unrelated prose or omitting part of a technical visual;
- broken code indentation, malformed links, unexpected blank pages, or severe spacing;
- missing glyph boxes or visibly substituted characters.

For V2 Problem callouts, also fail if the card alternates English and Chinese in small
fragments, if the divider is missing or duplicated, or if a nested list/formula escapes
the card. Cross-page cards are acceptable only when the left and right borders keep one
fixed horizontal position on every fragment and the indentation, reading order, and
content continuity remain clear.

Bind the review to the compiled PDF SHA-256. Recompilation invalidates an older visual
review even when the filename is unchanged.

The compile report must contain nonempty, hash-bound contact sheets whose page ranges
cover every output page exactly once. `record_visual_review.py` rejects empty notes,
missing/changed contact sheets, partial page coverage, or a PDF hash mismatch. CI may
test this gate and may reach `automated_status: passed`, but it must not write a human
`visual-review.json: passed` on its own.

## Completion language

Only a passed `qa-report.json` permits “complete,” “verified,” or equivalent wording.
Otherwise report the gate and exact blocker. Never infer success from a readable sample
page, a zero exit from extraction alone, or a model's statement that it translated the
whole document.
