---
name: make-bilingual-study-pdf
description: Convert a native-text English academic PDF—especially assignment handouts, lecture notes, and papers—into complete English-first Simplified-Chinese bilingual Markdown, editable XeLaTeX, compiled PDF, and an auditable QA report. Use when correctness, formula/code/link preservation, resumable translation, and proof against silent omissions matter more than reproducing the source page layout pixel-for-pixel. Also use to diagnose why an attempted bilingual conversion is incomplete. V1 does not accept scanned or substantially mixed-image PDFs.
---

# Make Bilingual Study PDF

Create a content-faithful bilingual study edition through deterministic extraction,
translation, merge, compilation, and independent audits. Never ask a model to emit a
whole LaTeX document. The model may fill only numbered translation response records.

Read [format-spec.md](references/format-spec.md) before translating and
[qa-rules.md](references/qa-rules.md) before reporting completion. Resolve every
script path relative to this `SKILL.md` directory, even if the installed directory has
an opaque generated name.

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

Before starting a long translation, confirm that Poppler, PyMuPDF, Pillow, XeLaTeX,
`latexmk`, `xeCJK`, and a supported CJK font are available. Markdown and `.tex` can
still be built without the TeX prerequisites, but the PDF gate must remain failed.

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

## Completion rule

Call the job complete only when `output/qa-report.json` is `passed`. Deliver the
bilingual `.md`, editable `.tex`, compiled `.pdf`, and QA report. State any warnings
from the report. If a gate is blocked by missing software, fonts, an unsupported PDF,
or unresolved layout, report the exact blocker and do not claim a verified PDF.
