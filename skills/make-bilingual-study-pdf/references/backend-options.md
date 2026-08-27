# Optional parser and layout backends

V1 deliberately uses a small, inspectable PyMuPDF + Poppler pipeline. Escalate only
when the source cannot pass the native-text scope gate or when the user explicitly
needs original-page layout preservation.

## Decision guide

| Need | Candidate | What it adds | Integration rule |
|---|---|---|---|
| Original-layout scientific bilingual PDF | [BabelDOC](https://github.com/funstory-ai/BabelDOC) | Parsing/rendering stages, formula-aware translation, glossary support, and OpenAI-compatible translation APIs | Treat as an optional extraction/rendering backend. Do not copy or bundle its AGPL-3.0 code. Keep this skill's hashes, record IDs, audits, and English-first study output as a separate path. |
| Mature scientific-PDF translation interface | [PDFMathTranslate](https://github.com/PDFMathTranslate/PDFMathTranslate) | Layout-preserving translation with formulas, charts, annotations, CLI, GUI, and multiple providers | Recommend only when source-layout preservation is the primary requirement. It is AGPL-3.0; do not silently add it as a dependency. |
| Native-text papers/notes already parsed by MinerU | [MinerU](https://github.com/opendatalab/MinerU) | Structured legacy JSON, tables, formulas, visuals, and complex reading order | V2.3 imports frozen stable 3.x `pipeline` output without installing or running MinerU. Require hash-equal `origin.pdf`, freeze every input/asset, and rerun independent Poppler/source audits. |
| Scans or garbled pages parsed by MinerU | [MinerU](https://github.com/opendatalab/MinerU) | OCR/layout evidence for manual comparison | Import may prepare source/layout/span comparison pages, but status remains `manual_source_review_required`; it cannot automatically pass source or final QA. |
| General document conversion or VLM-assisted parsing | [Docling](https://github.com/docling-project/docling) | PDF/Office/image conversion to structured Markdown or JSON through CLI and Python APIs | Future opt-in backend for unsupported inputs. Never treat its output as proof of completeness; compare it against an independent oracle and the rendered pages. |

## Architectural boundary

Borrow ideas, not implementation coupling:

- keep extraction, translation, rendering, and QA as separate stages;
- protect formulas, code, paths, links, citations, and identifiers before translation;
- keep translation resumable and glossary-aware;
- retain cross-page context without letting context become translated content;
- render the result and visually inspect every page through contact sheets.

Every optional parser must satisfy the Profile/IR adapter contract in `profile-ir.md`
and emit the same evidence fields required by the current manifest:
stable ID, page, bounding box, source text, source hash, kind, protected spans, links,
visual references, and deterministic reading order. If it cannot, stop with a clear
limitation report rather than weakening the audit rules.

## Why the default remains lightweight

The default output is an English-first study edition, not a pixel-level overlay of the
source. PyMuPDF supplies geometry, fonts, links, and crops; Poppler independently
checks text sequences and renders pages. This gives the workflow two independent
views of the source without model downloads or a copyleft runtime dependency. The
more capable backends above remain replaceable options for later versions.

## MinerU V2.3 support boundary

The verified public fixture records MinerU 3.4.4 with `_backend: "pipeline"` and the
legacy flat `content_list.json` format. Other stable 3.x pipeline versions are accepted
only after strict field validation and recorded as compatible rather than fixture-
verified. VLM, hybrid, office, prerelease/4.x output, `content_list_v2.json`-only output,
and `structured_content.json` are not accepted as V2.3 success inputs.

Review MinerU's current license before each release or hosted-service deployment. The
importer contains no MinerU code, model, or weight and never invokes its CLI; that does
not remove an operator's obligations for the external parser they chose to run.
