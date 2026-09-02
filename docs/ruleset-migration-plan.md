# CI ruleset migration and rollback plan

This document is a review artifact, not authorization to change repository settings.
The work-package-D implementation deliberately leaves both live rulesets untouched.
The machine-readable old and proposed values are frozen in
[`ruleset-migration-state.json`](ruleset-migration-state.json).

## Read-only snapshot

The snapshot was obtained from the GitHub REST API on 2026-09-02 against baseline
`main@21cf4105b98da106a221b2ace87492d4978bfa53`. Re-read before any authorized write:

```bash
gh api repos/littlemuu/make-bilingual-study-pdf/rulesets/21659417
gh api repos/littlemuu/make-bilingual-study-pdf/rulesets/21659622
```

The observed live state is:

| Ruleset | Target and condition | Enforcement | Strict | Required GitHub Actions contexts |
| --- | --- | --- | --- | --- |
| `21659417` `main-baseline-gate` | branch, `~DEFAULT_BRANCH` | active | true | `workflow-lint`, `self-test`, `installer-parity`, `automated-forward`, `windows-filesystem`, `macos-filesystem` |
| `21659622` `v-release-tags` | tag, `refs/tags/v*` | active | false | the same six contexts |

Both use GitHub Actions integration ID `15368`, `do_not_enforce_on_create=false`, an
empty bypass list, and deletion/non-fast-forward protection. The branch ruleset also
preserves its exact pull-request parameters; the tag ruleset also prohibits updates.

## Proposed values

Only each `required_status_checks` list changes. Every other condition, rule,
enforcement, strictness, integration ID, pull-request parameter, and bypass value stays
byte-for-byte equivalent to the editable projection in the JSON artifact.

- Branch ruleset `21659417`: replace the six migration contexts with the single
  `pr-fast` context from GitHub Actions integration `15368`.
- Tag ruleset `21659622`: replace the six migration contexts with `main-full` and
  `safety`, both from GitHub Actions integration `15368`.

`pr-fast` is the strict all-success aggregate of all six compatibility jobs on a pull
request. `main-full` is the corresponding aggregate on a push to `main`. Both aggregates
run under `always()` and explicitly fail for failed, cancelled, skipped, neutral,
unknown, or missing dependency results. `safety` applies the same strict evaluator,
binds its run
to the current default-branch head and aggregates APFS, four-path installer/fallback,
and fault-injection evidence. The guarded release workflow independently requires an
exact-SHA successful `main-full` workflow and `safety` evidence no older than 168 hours.

## Authorized migration order

Do not start this sequence without separate explicit authorization to modify live
rulesets.

1. Merge the workflow PR only after its exact head has all six old contexts and
   `pr-fast` successful. Confirm the resulting `main` SHA has all six old contexts and
   `main-full` successful.
2. Manually dispatch `safety.yml` on that current `main` SHA. Confirm the workflow and
   its final `safety` context succeeded, with no skipped APFS, installer, fallback, or
   fault-injection job.
3. Re-read both rulesets. Stop if the IDs, `updated_at`, conditions, bypass list, rule
   order, pull-request parameters, strictness, integration IDs, or required contexts
   differ from `ruleset-migration-state.json`; update and re-review this plan instead of
   overwriting drift.
4. Update branch ruleset `21659417` with its complete `proposed` object. Immediately
   re-read it and compare every returned editable field. Open a harmless Draft PR and
   confirm `pr-fast` is requested, runs, and resolves; do not remove any compatibility
   job yet.
5. Update tag ruleset `21659622` with its complete `proposed` object only while the
   current `main` SHA has successful `main-full` and fresh `safety` contexts.
   Immediately re-read and compare every editable field. Do not create a test tag.
6. After both live reads and a fresh PR prove the new contexts, authorize a separate
   cleanup PR to remove the six migration jobs from PR frequency and move the remaining
   Windows/APFS cost to the documented tiers. That cleanup is intentionally impossible
   before steps 1-5; otherwise the old rulesets could wait forever.

At every step, inspect the exact commit's checks rather than a cached PR summary. A
successful workflow on another SHA is not migration evidence.

## Verification gates

Migration is accepted only if all of the following are true:

- the workflow-implementation PR head and its merged `main` SHA each produced all six
  old contexts with successful conclusions;
- the implementation PR head produced `pr-fast`, and the merged SHA produced
  `main-full`;
- the manual `Safety` run is on the exact current `main` SHA, all evidence jobs ran,
  and the final `safety` context succeeded;
- a post-branch-migration Draft PR is blocked while `pr-fast` is pending and unblocked
  only after it succeeds;
- live GET responses match the complete proposed values; no context is absent, skipped,
  pending forever, or accepted from another SHA;
- the guarded release remains untriggered. No tag or Release is created as part of
  ruleset migration.

## Rollback

Rollback uses the complete `observed` object for the affected ruleset, never a partial
context-only edit.

1. If the branch update does not request or resolve `pr-fast`, stop before the tag
   update and restore branch ruleset `21659417` to its complete `observed` object. The
   six compatibility jobs are still present and will satisfy the restored policy.
2. If the tag update differs from the proposal or cannot observe both new contexts on
   the exact SHA, restore tag ruleset `21659622` to its complete `observed` object.
   Restore the branch ruleset too if repository policy should return atomically to the
   pre-migration model.
3. Re-read restored settings and compare all editable fields to `observed`. Run a new
   Draft PR at a fresh head and require all six contexts to complete. Do not delete,
   retarget, or create tags during rollback.
4. Preserve failed API responses and before/after GET output for review. If a write's
   outcome is unknown, make no second write until a read determines the live state.

The compatibility jobs and their exact names must remain merged until rollback is no
longer needed and the separately authorized cleanup PR is accepted.
