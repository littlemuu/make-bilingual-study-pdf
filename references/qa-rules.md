# Quality gates and failure rules

All gates are mandatory. A later gate never excuses an earlier failure.

## 1. Source gate

Pass only when:

- the source PDF hash is unchanged;
- at least 70% of pages contain at least 100 normalized text characters;
- PyMuPDF and the independent Poppler oracle agree on page count;
- weighted source five-gram coverage is at least 95%;
- every oracle `Problem(...)` ID exists in extracted source text;
- every source page render exists;
- contact sheets cover every source page;
- every protected font span points to the exact stored source substring;
- every visual and link reference resolves.

Low per-page coverage is a visual-review trigger even when the global score passes.
Do not describe the five-gram score as a literal percentage of semantic completeness;
it is a conservative missing-sequence detector.

## 2. Translation gate

Pass only when:

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

- all source, audit, glossary, and translation inputs retain their build-time hashes;
- Markdown and XeLaTeX retain their generated hashes;
- every source block has exactly one explicit disposition;
- every emitted block has one valid source-hash marker;
- every copied visual asset exists with its recorded hash;
- every source Problem ID and external URI remains accounted for.

Manual edits to generated Markdown or `.tex` invalidate the output gate. Make changes
upstream and rebuild.

## 4. Compile gate

Pass automated QA only when:

- XeLaTeX exits successfully and produces the expected PDF;
- Chinese text has `xeCJK` and a supported CJK font;
- the log has no missing-character or undefined-reference messages;
- every expected Problem ID is extractable from the compiled PDF;
- Chinese remains extractable from a Chinese bilingual document;
- rendered page count equals PDF page count;
- no page is apparently blank.

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

Bind the review to the compiled PDF SHA-256. Recompilation invalidates an older visual
review even when the filename is unchanged.

## Completion language

Only a passed `qa-report.json` permits “complete,” “verified,” or equivalent wording.
Otherwise report the gate and exact blocker. Never infer success from a readable sample
page, a zero exit from extraction alone, or a model's statement that it translated the
whole document.
