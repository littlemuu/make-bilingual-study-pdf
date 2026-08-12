---
name: make-bilingual-study-pdf
description: Convert a native-text English academic PDF—especially assignment handouts, lecture notes, and papers—into complete English-first Simplified-Chinese bilingual Markdown, an editable DOCX study edition, optional XeLaTeX source, compiled PDF, and an auditable QA report. Use when correctness, formula/code/link preservation, resumable translation, and proof against silent omissions matter more than reproducing the source page layout pixel-for-pixel. Also use to diagnose why an attempted bilingual conversion is incomplete. Scanned or substantially mixed-image PDFs require a different backend.
---

# Make Bilingual Study PDF

Create a content-faithful bilingual study edition through deterministic extraction,
translation, merge, compilation, and independent audits. Never ask a model to emit a
whole DOCX or LaTeX document. The model may fill only numbered translation response
records; scripts own document structure and presentation.

Read [format-spec.md](references/format-spec.md) before translating and
[qa-rules.md](references/qa-rules.md) before reporting completion. Resolve every
script path relative to this `SKILL.md` directory, even if the installed directory has
an opaque generated name.

## Profile and IR

V2.2 uses a versioned Profile plus a unified document IR. The supported default is
`profiles/assignment-en-zh.json`; it preserves the current English-assignment to
Simplified-Chinese behavior. Read [profile-ir.md](references/profile-ir.md) before
adding a document type, language, parser, renderer, or QA policy.

Bind the Profile into `WORK_DIR/profile.json` and generate `document-ir.json`. Treat
both files as frozen source artifacts. The native PDF adapter records semantic labels
as `anchor-only` unless structural evidence proves complete container membership.
Never turn neighboring paragraphs into a semantic group by proximity alone.

Use the Profile-aware entry point for new jobs and recovery:

```bash
python3 "$SKILL_DIR/scripts/pipeline.py" validate-profile assignment-en-zh
python3 "$SKILL_DIR/scripts/pipeline.py" source SOURCE.pdf \
  --work-dir "$WORK_DIR" --profile assignment-en-zh
python3 "$SKILL_DIR/scripts/pipeline.py" status "$WORK_DIR"
```

The entry point runs deterministic stages and stops at glossary review, translation,
and visual-review checkpoints. Its status output names the next safe resumable action.

## Scope gate

Accept only an English, native-text PDF whose goal is English-first Simplified Chinese.
Keep the source PDF immutable. Work in a dedicated conversion directory outside a
coursework/code repository so repository instructions do not accidentally govern the
document workflow.

Refuse or stop with an explicit limitation report when the PDF is encrypted, scanned,
has usable text on fewer than 70% of pages, needs OCR, or requires pixel-perfect layout.
Do not silently switch to OCR. Tables, equations, and diagrams may be preserved as
high-resolution source crops with translated captions in V1.

When a document fails this scope gate, read
[backend-options.md](references/backend-options.md) and explain the smallest suitable
optional backend. Do not install a heavyweight parser, download model weights, or add
an AGPL component without the user's explicit approval. An alternate backend must
still feed the same stable-ID records and pass the same downstream audits.

Before starting a long translation, confirm that Poppler, PyMuPDF, Pillow, Pandoc, and
a supported CJK font are available. The XeLaTeX profile additionally requires XeLaTeX,
`latexmk`, `xeCJK`, `unicode-math`, and Latin Modern Math. The V2 DOCX profile requires
`python-docx` and LibreOffice for the matching PDF render. Source artifacts can still
be built when a rendering backend is absent, but the PDF gate must remain failed.

## Workflow

Use `python3` for all scripts below. Let `SKILL_DIR` mean the directory containing this
file and `WORK_DIR` mean a new dedicated directory for this one document.

### 1. Extract and prove the source inventory

Run:

```bash
python3 "$SKILL_DIR/scripts/extract_pdf.py" SOURCE.pdf --work-dir "$WORK_DIR"
python3 "$SKILL_DIR/scripts/audit_source.py" "$WORK_DIR"
```

The extractor uses PyMuPDF for coordinates, font runs, links, and crops, then Poppler
as an independent text oracle and renderer. It creates stable block IDs and hashes,
all page renders, contact sheets, external-link inventory, equation crops, and
figure/table crops. Never continue unless `source-audit.json` says `passed`.

Inspect every image in `source-contact/`. Open full-resolution renders for pages with
low coverage, dense equations, columns, diagrams, tables, or suspicious ordering.
Confirm that visual crops contain the complete figure/equation and not neighboring
prose. If crop heuristics are wrong, fix extraction or preserve a larger source crop;
never reconstruct a technical diagram from memory.

For an older extracted work directory, run `pipeline.py ir WORK_DIR`, then rerun the
source audit. Profile/IR migration invalidates every older downstream hash.

### 2. Freeze and review the glossary

Run:

```bash
python3 "$SKILL_DIR/scripts/init_glossary.py" "$WORK_DIR"
```

Review `translation/glossary.json`. Add repeated domain terms with approved Chinese
targets. Set `enforce` to `true` only when every occurrence must use an approved target.
Do this before planning; later glossary edits deliberately invalidate the plan.

### 3. Create resumable translation batches

Run:

```bash
python3 "$SKILL_DIR/scripts/prepare_translation.py" "$WORK_DIR"
```

Read [translation-protocol.md](references/translation-protocol.md). Translate each
`translation/requests/part-NNNN.jsonl` into the corresponding
`translation/responses/part-NNNN.jsonl`. Copy `id` and `source_sha256`; write only the
`translation` value. Preserve every `⟦Knnn⟧` placeholder exactly once. Context fields
are context only, not content to append.

After each batch or resumed session run:

```bash
python3 "$SKILL_DIR/scripts/audit_translation.py" "$WORK_DIR" --progress
```

The progress mode tolerates only missing future IDs. It still rejects duplicate IDs,
stale hashes, empty output, broken placeholders, unchanged English prose, and enforced
glossary violations. At the end, rerun without `--progress`; only `passed` creates
`translations-merged.jsonl`.

### 4. Build Markdown and XeLaTeX deterministically

Run:

```bash
python3 "$SKILL_DIR/scripts/build_outputs.py" "$WORK_DIR"
python3 "$SKILL_DIR/scripts/audit_outputs.py" "$WORK_DIR"
```

The build must consume the audited source and merged translations without another
translation pass. It places a complete English logical paragraph first and its Chinese
paragraph immediately after, preserves code once, restores protected values, embeds
source equation/figure crops, and includes every external URI. Never hand-edit the
generated Markdown or `.tex`; edit response records or glossary entries, reaudit, and
rebuild.

### 4a. Build the V2 editable DOCX study edition

Use the audited Markdown as the only content input. The V2 AST pass recognizes each
`Problem (...)` block quote, gathers the complete English task first, inserts one
language separator, then gathers the complete Chinese task. Nested lists, code, and
formula blocks remain inside the same callout. Headings and ordinary prose remain
English-first paragraph pairs.

Run:

```bash
fc-match "Noto Sans S Chinese"
python3 "$SKILL_DIR/scripts/build_docx.py" \
  "$WORK_DIR/output/NAME.md" "$WORK_DIR/output/NAME.docx" \
  --profile "$WORK_DIR/profile.json" \
  --resource-path "$WORK_DIR/output" --expected-problems EXPECTED_COUNT \
  --title "DOCUMENT TITLE — English-Chinese Bilingual Study Edition"
python3 "$SKILL_DIR/scripts/audit_docx.py" "$WORK_DIR/output/NAME.docx" \
  --profile "$WORK_DIR/profile.json" \
  --expected-problems EXPECTED_COUNT --expected-examples EXPECTED_EXAMPLES \
  --expected-tips EXPECTED_TIPS --expected-links EXPECTED_LINKS \
  --minimum-images EXPECTED_MINIMUM_IMAGES \
  --output "$WORK_DIR/output/docx-audit.json"
```

`fc-match` must resolve the requested family exactly, not silently fall back to a Latin
font. If the project carries its own fonts, point `FONTCONFIG_FILE` to its reviewed
`fonts.conf` for the build and render commands. Record the resolved family and font file.

The builder removes all internal Problem-range markers before saving. Treat a Problem
count mismatch, leaked marker, missing Chinese text, or lost external link as a build
failure. Do not style Problem paragraphs independently before the AST regrouping pass;
that recreates alternating micro-blocks instead of one coherent English-then-Chinese
task card. Keep Problem callouts as flowing paragraphs so long tasks split naturally
without table-induced blank space. Give every paragraph the same direct left and right
indent. For every numbered paragraph, explicitly set a zero first-line indent so the
numbering definition cannot contribute an inherited hanging origin and shift that
paragraph's border. Keep the real numbering, remove Pandoc's VML horizontal rule, and
draw exactly one bounded separator between the English and Chinese halves.

### 5. Compile, render, and inspect

Run:

```bash
python3 "$SKILL_DIR/scripts/compile_pdf.py" "$WORK_DIR"
```

Compilation must use `latexmk -xelatex`. The automated gate rejects missing glyphs,
undefined references, missing Problem IDs, blank pages, rendering-count mismatches,
and absent Chinese text. It reports overfull boxes for inspection and creates contact
sheets for every output page.

Inspect every `output/contact/contact-NNN.png`; open full-resolution output renders for
the title page, every page with a figure/table, pages around section transitions, the
last page, and every page flagged by logs or contact-sheet review. Check English-before-
Chinese order, clipping, overlap, broken URLs, isolated headings, tiny formulas, and
unintended blank space. Then record what was actually inspected:

```bash
python3 "$SKILL_DIR/scripts/record_visual_review.py" "$WORK_DIR" \
  --status passed --reviewed-pages all --spot-check-pages 1,5,12 \
  --notes "Concrete observations from this PDF"
python3 "$SKILL_DIR/scripts/finalize_qa.py" "$WORK_DIR"
```

Use the real relevant spot-check pages, not the example numbers. If visual review finds
a defect, record `--status failed`, repair upstream, rebuild, recompile, and inspect the
new PDF hash again.

For the V2 DOCX profile, render the approved editable document instead so the delivered
PDF matches it exactly:

```bash
python3 "$SKILL_DIR/scripts/compile_docx_pdf.py" \
  "$WORK_DIR/output/NAME.docx" "$WORK_DIR/output/NAME.pdf" \
  --render-dir "$WORK_DIR/output/pdf-renders" \
  --audit-output "$WORK_DIR/output/compile-audit.json" \
  --expected-problems EXPECTED_COUNT --cjk-font "Noto Sans S Chinese"
```

This conversion gate requires A4 pages, embedded PDF fonts, extractable Chinese, every
expected Problem ID, one render per page, no apparently blank page, exact resolution of
the requested CJK family, and that resolved CJK font embedded in the PDF. Extractable
Chinese alone is insufficient: an absent CJK font can leave invisible glyphs while the
PDF text layer still looks complete. Fully decode every rendered PNG; if a batch render
is truncated, rerender only that page in a temporary directory, copy it with a blocking
operation, and decode it again before creating contact sheets. Inspect every render
after the final conversion.
Any DOCX rebuild or PDF reconversion invalidates an older visual review, even if
filenames stay the same.

## Completion rule

Call the job complete only when `output/qa-report.json` is `passed`. For the V2 profile,
deliver the bilingual `.md`, editable `.docx`, matching `.pdf`, and QA report; include
the editable `.tex` when requested or when the project uses the XeLaTeX profile. State
any warnings from the report. If a gate is blocked by missing software, fonts, an
unsupported PDF, or unresolved layout, report the exact blocker and do not claim a
verified PDF.
