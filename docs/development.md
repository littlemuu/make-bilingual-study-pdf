# Development and release gates

The repository targets Python 3.11.7 through `.python-version`. Runtime code has one
canonical location: `skills/make-bilingual-study-pdf/`. Repository tests import that
subtree directly; do not copy runtime modules back to the repository root.

## Current simplification boundary

The approved architecture direction is documented in
[`architecture-simplification.md`](architecture-simplification.md). Its first phase is
behavior-preserving deduplication: one shared Job/Gate evaluator, one runtime artifact
safety layer, one repository payload helper, and a staged CI/ruleset migration.

This document describes the **currently active** commands and gates. CI workflow
tiering is staged ahead of the separately authorized live-ruleset migration:

- every existing required check remains required;
- no workflow job or check context may be removed merely because the target CI design is
  lighter;
- Profile schema, output bytes, runtime dependencies, VERSION, release metadata, tag and
  Release state must remain unchanged;
- changes to live branch/tag rulesets require a separate, explicitly authorized settings
  operation with recorded old values, new values, and rollback steps.

A simplification PR must identify which duplicated implementation it removes and prove
that content-integrity, freeze-chain, source-review, font, render and visual-review gates
remain equivalent. It must not treat reduced test frequency as permission to weaken the
release-candidate evidence.

## Create the environment

On Linux or macOS:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

On Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

The quick baseline requires Poppler's `pdftoppm` and `pdftotext`. The automated
DOCX/PDF forward test additionally requires Pandoc, Fontconfig, LibreOffice Writer,
`pdffonts`, Noto Sans/Noto CJK fonts, and DejaVu Sans Mono. Ubuntu 24.04 CI installs:

```bash
sudo apt-get update
sudo apt-get install --yes --no-install-recommends \
  fontconfig fonts-dejavu-core fonts-noto-cjk fonts-noto-core \
  libreoffice-writer pandoc poppler-utils
```

XeLaTeX/latexmk remain optional for the separate TeX output path. MinerU and its
models are not development dependencies; tests consume only committed frozen fixtures.

## Maintain the install manifest

Every file in the installable subtree except `release-manifest.json` itself is frozen
by path, byte count, and SHA-256. After an intentional payload edit, regenerate and
then verify the deterministic manifest:

```bash
.venv/bin/python tools/check_skill_eol.py --fix
.venv/bin/python tools/check_skill_eol.py
.venv/bin/python tools/build_release_manifest.py
.venv/bin/python tools/build_release_manifest.py --check
.venv/bin/python skills/make-bilingual-study-pdf/scripts/release_check.py
.venv/bin/python tools/repository_release_check.py
```

The release check is strict by default. It rejects missing, extra, changed, unsafe,
case-colliding, or symbolic-link payload entries. `--ignore-generated-cache` is only
for a post-use local recheck; release CI never uses it. The separate repository check
binds the human-facing install and installed-verification commands in the root
`README.md` to the current Skill `VERSION`; the installed Skill does not carry or
depend on that repository documentation.

The EOL checker and manifest builder inspect the lexical path from its filesystem
anchor through the repository, `skills/`, and the Skill root without resolving away
links. The EOL checker then inventories and classifies the complete Skill tree before
changing any bytes. It normalizes only explicitly allowlisted UTF-8 text types,
passes through known binary asset types without reading them, and rejects unknown
types. Both check and `--fix` modes also reject symbolic links, hard-linked files,
Windows reparse points, and non-regular entries without reading or rewriting their
targets. `--fix` writes and fsyncs a same-directory temporary file, preserves the
original permission bits, and publishes it with atomic replacement; a failed write
or replacement leaves the original bytes intact and removes the owned temporary.

## Validate Skill metadata

Baseline and Release check out `openai/skills` at the exact commit
`49f948faa9258a0c61caceaf225e179651397431`. To reproduce the metadata gate locally,
make `<OPENAI_SKILLS_CHECKOUT>` a checkout of that commit, then run:

```bash
.venv/bin/python tools/validate_skill.py skills/make-bilingual-study-pdf \
  --upstream-validator "<OPENAI_SKILLS_CHECKOUT>/skills/.system/skill-creator/scripts/quick_validate.py"
.venv/bin/python tests/skill_validator_test.py \
  --upstream-validator "<OPENAI_SKILLS_CHECKOUT>/skills/.system/skill-creator/scripts/quick_validate.py"
```

`tools/validate_skill.py` first runs the pinned official validator, then rejects
duplicate `SKILL.md` keys and enforces the repository contract for
`agents/openai.yaml`: valid YAML with unique keys at every level, unquoted keys,
quoted string values, the required interface fields and explicit Skill token, safe
existing `./assets/` icons, optional `#RRGGBB`, the preserved policy field types, and
the documented optional MCP dependency shape. Its ten regression methods cover the
current Skill, malformed/non-string/duplicate `SKILL.md` metadata, and invalid,
duplicate, unquoted, mistyped, tokenless, or unsafe/missing-resource interface
metadata, including policy and MCP dependency cases.

## Run the current full baseline

`tools/run_test_suite.py` is the single command registry used by local development and
CI. It includes `pip check`, payload and metadata gates, workflow-contract regressions,
all existing unit and adversarial suites, the Skill self-test, and all three Profile
validations. From the repository root, point it at the pinned upstream validator:

```bash
.venv/bin/python tools/run_test_suite.py full \
  --upstream-validator "<OPENAI_SKILLS_CHECKOUT>/skills/.system/skill-creator/scripts/quick_validate.py"
```

On Windows, replace `.venv/bin/python` with `.\.venv\Scripts\python.exe`. The
runner's named suites are CI composition units, not alternative command lists. Use
`--list-json` or `--dry-run` for inspection. A missing Python module or external
executable is an environment failure, not a product pass.

## Installer parity gate

CI checks out OpenAI's system `skill-installer` at the exact commit
`1131cea7b17214e5a96300a5a72c94642346ef34`. The PR and `main` Baseline use the
default automatic installer at the immutable event SHA as a real clean-install smoke.
The scheduled/manual safety tier installs the exact path
`skills/make-bilingual-study-pdf` through all four real paths:

1. forced archive download at the immutable event SHA;
2. forced Git sparse checkout at the event's named branch ref;
3. the installer's default automatic path at the immutable event SHA;
4. the default automatic path with intentionally invalid archive credentials, proving
   that a real download failure falls back to Git at the named ref.

The two automatic runs use separate `GIT_TRACE2_EVENT` files. The normal automatic
download must produce no Git trace file. The invalid-authentication run must produce a
non-empty trace containing a Git `cmd_name` event for `clone`; matching output bytes
alone are not accepted as proof that fallback actually occurred.

The upstream Git fallback performs a shallow clone, so the two Git paths need a named
branch ref rather than an arbitrary detached SHA. CI resolves the exact full ref
anonymously with `git ls-remote --exit-code --refs` both before and after installation,
requires exactly one returned record, and binds its SHA and ref name to the event. This
avoids incorrectly using the base repository's scoped `GITHUB_TOKEN` for a public fork.
The installer's recognized token variables are likewise unset for fork installs so its
normal archive request is public and anonymous; same-repository installs retain the
read-only workflow token, while the fallback lane still injects its intentional invalid
token. A same-PR/ref concurrency group cancels a superseded run after a newer push
instead of letting the older mutable-ref run report a misleading result. Pull requests
use their head repository, SHA, and branch, including when the head is a fork.

All four installed directories must:

- pass their own strict `scripts/release_check.py`;
- match the checked-out canonical subtree and each other by file path, byte count, and
  SHA-256;
- contain no `.git`, workflow, fixture, test, development, or repository-root files;
- leave the default automatic installation able to pass `pip check`, `self_test.py`,
  and all three Profile validations in a new venv outside the installed Skill.

## CI tiers and migration contexts

The workflow responsibilities are now explicit:

| Context or workflow | Trigger | Responsibility |
| --- | --- | --- |
| `pr-fast` | pull request | lint and workflow contract, repository/metadata gates, core regressions, Linux synthetic forward chains, one exact-SHA clean install |
| `main-full` | push to `main` | the complete command registry, full Windows filesystem suite, Linux forward chains, one exact-SHA clean install |
| `safety` | weekly schedule or manual dispatch on current `main` | real APFS aliases, four installer paths including authenticated-failure fallback, complete fault injection |
| guarded draft release | explicitly authorized `repository_dispatch` | exact candidate binding, fresh `main-full` and `safety` evidence, release-path validation, tag/Release state guards |

`Baseline` also supports manual dispatch so reviewers can run the complete `main-full`
matrix on an exact feature-branch head before merge. Release validation accepts only a
successful `push` run on `main`, never this manual review run.

During the ruleset migration window, `.github/workflows/baseline.yml` continues to
execute and emit the six live required contexts on every pull request and every push to
`main`: `workflow-lint`, `self-test`, `windows-filesystem`, `macos-filesystem`,
`installer-parity`, and `automated-forward`. None has a job-level condition. The final
`pr-fast` or `main-full` job depends on all six, so it cannot report success after a
failed compatibility context. Windows runs a focused real smoke on PRs and its full
filesystem group on `main`; macOS APFS remains active on both events until the live
ruleset is separately migrated. This transitional cost prevents a dangling required
check.

`tools/check_workflow_contract.py` and `tests/workflow_contract_test.py` reject a
missing or conditional required context, missing trigger, incomplete safety aggregate,
removed fresh-safety release gate, or reintroduced development command list. The live
ruleset old/new values, authorization boundary, ordered migration, verification, and
rollback are recorded in [`ruleset-migration-plan.md`](ruleset-migration-plan.md).

## Default-branch release path

`.github/workflows/release.yml` listens only for a `repository_dispatch` event named
`release-candidate`. GitHub binds that event to the current default-branch commit and
uses the workflow file on the default branch. The workflow requires all of the
following before it can create anything:

- a full 40-character candidate SHA equal to both the event SHA and remote `main` HEAD;
- an input version exactly equal to the Skill's `VERSION`, with the tag derived as
  `v<VERSION>`;
- a successful `Baseline` push run for that exact SHA on `main` (including the
  `main-full` aggregate);
- a successful scheduled/manual `Safety` run for that exact SHA no more than 168 hours
  old;
- absent tag and Release names;
- strict payload and repository-documentation checks, the pinned official Skill
  validator plus duplicate-key hardening, both release installer paths, and
  clean-environment checks;
- a second `main`/tag/Release check after validation.

The validation job has read-only permissions. A separate final job has only
`contents: write` and checks the same state again. It atomically reserves the new
lightweight tag with the Git refs API at the candidate SHA; a competing existing tag
causes that request to fail instead of attaching a Release to it. Only after successful
reservation does `gh release create --verify-tag --draft` create the Draft without
creating or retargeting a tag. Post-checks require the exact tag SHA, `Draft=true`,
`prerelease=false`, and the expected tag name. It never publishes the Release.

On any failure in the final job, the trap is report-only and never sends a DELETE
request. It queries the exact tag and same-name Release, then prints only sanitized
recovery fields: whether this run received the tag-reservation response, whether it
attempted Release creation, the observed tag SHA and candidate match, Release ID,
Draft/prerelease state and tag name, and whether the body contains this run attempt's
hidden `guarded-draft-release:<run-id>:<run-attempt>` marker. It does not print the
Release body.

A failure after atomic ref creation may therefore leave the exact tag, and a failure
after Release creation may leave the owned Draft. An unavailable API response can also
leave ownership fields unknown. A maintainer must inspect the reported SHA, ID, state,
and marker before any separately authorized manual recovery; the workflow never
deletes either object automatically and never publishes the Draft. Git tag and Release
APIs do not offer one cross-API transaction, so extreme external delete/recreate races
remain outside this observation boundary. The final `main` freshness checks are best
effort; the hard invariant is that any successfully verified tag resolves to the exact
SHA that passed validation.
Repository-wide bypass by a maintainer is still possible unless a tag ruleset is
configured, so this is the guarded official release path rather than an absolute
repository policy.

Triggering it creates a public tag and a Draft Release. Treat that as a separate,
explicitly authorized release action; do not run it merely because a PR merged or CI
passed. After authorization, set `CANDIDATE_SHA` to the exact verified `main` SHA and
send:

```bash
VERSION=$(tr -d '\r\n' < skills/make-bilingual-study-pdf/VERSION)
gh api --method POST repos/littlemuu/make-bilingual-study-pdf/dispatches \
  -f event_type=release-candidate \
  -f 'client_payload[candidate_sha]'="$CANDIDATE_SHA" \
  -f "client_payload[version]=$VERSION" \
  -f 'client_payload[confirm]'=CREATE_DRAFT_RELEASE
```

Publishing the Draft Release, replacing an installed user Skill, adding a license, or
packaging this capability as a cross-product Plugin are all separate decisions.
