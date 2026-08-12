# Profile and document IR contract

Read this reference when adding a document type, language pair, parser adapter,
renderer, or QA policy. V2.2 ships one supported profile: `assignment-en-zh`.

## Profile boundary

A Profile is a versioned JSON control plane. It declares the input adapter and scope
thresholds, languages and target-script detection, semantic roles, reading order,
DOCX theme, and QA thresholds.

Bind the selected Profile to `WORK_DIR/profile.json` before extraction. Hash the bound
copy into the manifest, document IR, translation plan, and output manifest. The field
`profile_sha256` always means the canonical JSON hash; `profile_file_sha256` is the
separate byte-level stale-input hash where needed. Never read
a newer installed Profile in place of the bound copy during a resumed job.

V2.2 accepts only the `native-text-pdf` adapter and source-then-target reading order.
Adding a Profile value does not make an adapter, language, or renderer supported. Add
its implementation and failure tests before advertising it.

## Unified document IR

`document-ir.json` is deterministic and contains:

- source adapter, path, hash, page count, and input-artifact hashes;
- ordered nodes with stable IDs, kinds, text hashes, page/bounding-box evidence,
  translation eligibility, protected spans, link IDs, and relations;
- semantic groups and inventories for roles, links, and visuals;
- the exact bound Profile ID and canonical Profile hash.

The native PDF adapter can prove a semantic label and its anchor, but not necessarily
the full visual extent of a colored callout. Such groups must use:

```json
{
  "membership": "anchor-only",
  "member_node_ids": ["p003-b012"]
}
```

Do not infer following paragraphs into the group without structural evidence. A future
MinerU, Docling, DOCX, or Markdown adapter may emit complete membership only when its
source structure proves the container relationship. Preserve the original adapter
evidence so downstream renderers can distinguish proved containers from anchors.

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
- explicit semantic membership confidence (`anchor-only` or proved members).

Run an independent completeness oracle and rendered-page review for every PDF-derived
adapter. Parser output alone is never proof of completeness.
