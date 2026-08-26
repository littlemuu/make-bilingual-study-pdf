# Profile and document IR contract

Read this reference when adding a document type, language pair, parser adapter,
renderer, or QA policy. V2.3 ships `assignment-en-zh` (schema V1 compatibility),
`academic-paper-en-zh`, and `lecture-notes-en-zh` (schema V2).

## Profile boundary

A Profile is a versioned JSON control plane. It declares the input adapter and scope
thresholds, languages and target-script detection, semantic roles, reading order,
DOCX theme, and QA thresholds.

Bind the selected Profile to `WORK_DIR/profile.json` before extraction. Hash the bound
copy into the manifest, document IR, translation plan, and output manifest. The field
`profile_sha256` always means the canonical JSON hash; `profile_file_sha256` is the
separate byte-level stale-input hash where needed. Never read
a newer installed Profile in place of the bound copy during a resumed job.

Implemented adapters are `native-text-pdf` and `mineru-import`. The latter consumes
only frozen stable MinerU 3.x `pipeline` legacy output; it does not execute MinerU.
Adapter, style, and constraint IDs are registry-backed and unknown IDs fail closed.
Adding a Profile value does not make an adapter, language, or renderer supported.

## Unified document IR

`document-ir.json` is deterministic and contains:

- source adapter, path, hash, page count, and input-artifact hashes;
- ordered nodes with stable IDs, kinds, text hashes, page/bounding-box evidence,
  translation eligibility, protected spans, link IDs, and relations;
- each schema V2 node's `{role, style, output, evidence}` semantic policy;
- semantic groups and a full Profile-ordered role inventory, including allowed-zero
  roles, occurrence IDs, membership counts, minimum/maximum, style, and output;
- the exact bound Profile ID and canonical Profile hash.

The native PDF adapter can prove a semantic label and its anchor, but not necessarily
the full visual extent of a colored callout. Such groups must use:

```json
{
  "membership": "anchor-only",
  "member_node_ids": ["p003-b012"]
}
```

Do not infer following paragraphs into the group without structural evidence. V2.3
accepts `complete` only when adapter evidence names every existing member and includes
the anchor; pattern or geometric proximity remains `anchor-only`. Preserve the original
adapter evidence so downstream renderers can distinguish proved containers from anchors.

Schema V2 selectors are OR alternatives; fields inside one selector are AND conditions.
Specific adapter roles outrank patterns, and patterns outrank generic paragraph/heading
fallbacks. Role and style are independent, so theorem, lemma, proposition, and corollary
may share one style without losing separate inventory counts.

## Compatibility and migration

Legacy work directories remain readable. Migrate one explicitly with:

```bash
python3 "$SKILL_DIR/scripts/pipeline.py" ir "$WORK_DIR"
python3 "$SKILL_DIR/scripts/pipeline.py" source-audit "$WORK_DIR"
```

Migration changes the manifest and therefore invalidates the older source audit,
translation plan, and later hashes. Do not attach a Profile to an in-progress
translated job unless the user accepts rebuilding every downstream deterministic
artifact.

## Adapter contract

Every new adapter must emit the fields needed to construct the same IR node contract:

- stable ID and deterministic order;
- page or equivalent location evidence;
- exact source text and SHA-256;
- content kind and translation eligibility;
- protected spans, links, visuals, and relations;
- exactly one output policy (`bilingual`, `source-only`, `visual-once`, or
  `artifact-omitted`);
- explicit semantic membership confidence (`none`, `anchor-only`, or `complete`).

Run an independent completeness oracle and rendered-page review for every PDF-derived
adapter. Parser output alone is never proof of completeness.

## Frozen MinerU evidence

`mineru-import` requires one matching-prefix legacy content list, middle JSON, and
`origin.pdf`; the origin hash must equal the supplied source PDF. It freezes those
inputs and referenced assets under `WORK_DIR`, validates strict JSON, page indices,
normalized content bboxes, native middle bboxes, paths, and full image decodes, and
writes `adapter-evidence.json`. Every content item owns one disposition and every
emitted node retains its JSON pointer. The evidence aggregate hash is bound into the
manifest, IR, source audit, translation plan, and later outputs.

Supported success input is stable MinerU 3.x `_backend: "pipeline"` with legacy flat
`content_list.json`. VLM, hybrid, office, prereleases, other major versions, V2-only
structured output, missing origin binding, and unsafe/corrupt inputs fail closed.
