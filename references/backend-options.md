# Optional parser and layout backends

V1 deliberately uses a small, inspectable PyMuPDF + Poppler pipeline. Escalate only
when the source cannot pass the native-text scope gate or when the user explicitly
needs original-page layout preservation.

## Decision guide

| Need | Candidate | What it adds | Integration rule |
|---|---|---|---|
| Original-layout scientific bilingual PDF | [BabelDOC](https://github.com/funstory-ai/BabelDOC) | Parsing/rendering stages, formula-aware translation, glossary support, and OpenAI-compatible translation APIs | Treat as an optional extraction/rendering backend. Do not copy or bundle its AGPL-3.0 code. Keep this skill's hashes, record IDs, audits, and English-first study output as a separate path. |
| Mature scientific-PDF translation interface | [PDFMathTranslate](https://github.com/PDFMathTranslate/PDFMathTranslate) | Layout-preserving translation with formulas, charts, annotations, CLI, GUI, and multiple providers | Recommend only when source-layout preservation is the primary requirement. It is AGPL-3.0; do not silently add it as a dependency. |
| Scans, complex tables, or difficult multi-column extraction | [MinerU](https://github.com/opendatalab/MinerU) | OCR-capable structured Markdown/JSON extraction, tables, formulas, and complex layouts | Future opt-in backend. Confirm its model/download footprint and license terms before installation. Convert its output into this skill's stable block schema and rerun all audits. |
| General document conversion or VLM-assisted parsing | [Docling](https://github.com/docling-project/docling) | PDF/Office/image conversion to structured Markdown or JSON through CLI and Python APIs | Future opt-in backend for unsupported inputs. Never treat its output as proof of completeness; compare it against an independent oracle and the rendered pages. |

## Architectural boundary

Borrow ideas, not implementation coupling:

- keep extraction, translation, rendering, and QA as separate stages;
- protect formulas, code, paths, links, citations, and identifiers before translation;
- keep translation resumable and glossary-aware;
- retain cross-page context without letting context become translated content;
- render the result and visually inspect every page through contact sheets.

Every optional parser must emit the same fields required by the current manifest:
stable ID, page, bounding box, source text, source hash, kind, protected spans, links,
visual references, and deterministic reading order. If it cannot, stop with a clear
limitation report rather than weakening the audit rules.

## Why the default remains lightweight

The default output is an English-first study edition, not a pixel-level overlay of the
source. PyMuPDF supplies geometry, fonts, links, and crops; Poppler independently
checks text sequences and renders pages. This gives the workflow two independent
views of the source without model downloads or a copyleft runtime dependency. The
more capable backends above remain replaceable options for later versions.
