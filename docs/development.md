# Development and release gates

The repository targets Python 3.11.7 through `.python-version`. Runtime code has one
canonical location: `skills/make-bilingual-study-pdf/`. Repository tests import that
subtree directly; do not copy runtime modules back to the repository root.

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
```

The release check is strict by default. It rejects missing, extra, changed, unsafe,
case-colliding, or symbolic-link payload entries. `--ignore-generated-cache` is only
for a post-use local recheck; release CI never uses it.

## Run the quick baseline

Run from the repository root:

```bash
.venv/bin/python -m pip check
.venv/bin/python skills/make-bilingual-study-pdf/scripts/release_check.py
.venv/bin/python tests/release_check_test.py
.venv/bin/python skills/make-bilingual-study-pdf/scripts/self_test.py
.venv/bin/python tests/v23_profile_test.py
.venv/bin/python tests/v23_mineru_test.py
.venv/bin/python tests/v23_ir_source_test.py
.venv/bin/python tests/v23_output_test.py
.venv/bin/python tests/v23_docx_test.py
.venv/bin/python tests/v23_visual_gate_test.py
.venv/bin/python skills/make-bilingual-study-pdf/scripts/pipeline.py validate-profile assignment-en-zh
.venv/bin/python skills/make-bilingual-study-pdf/scripts/pipeline.py validate-profile academic-paper-en-zh
.venv/bin/python skills/make-bilingual-study-pdf/scripts/pipeline.py validate-profile lecture-notes-en-zh
```

On Windows, replace `.venv/bin/python` with `.\.venv\Scripts\python.exe`. A missing
Python module or external executable is an environment failure, not a product pass.

## Installer parity gate

CI checks out a pinned revision of OpenAI's system `skill-installer`, then installs the
same repository ref twice with the exact path
`skills/make-bilingual-study-pdf`: once with `--method download`, once with
`--method git`. Both installed directories must:

- pass their own strict `scripts/release_check.py`;
- have identical file paths, byte counts, and SHA-256 values;
- contain no `.git`, workflow, fixture, test, development, or repository-root files;
- pass `pip check`, `self_test.py`, and all three Profile validations in a new venv
  created outside the installed Skill.

The GitHub Actions `Baseline` workflow runs the quick baseline, installer parity, and
both schema V2 automated forward chains for pull requests and pushes to `main`. It does
not use tag pushes as a release gate.

## Default-branch release path

`.github/workflows/release.yml` listens only for a `repository_dispatch` event named
`release-candidate`. GitHub binds that event to the current default-branch commit and
uses the workflow file on the default branch. The workflow requires all of the
following before it can create anything:

- a full 40-character candidate SHA equal to both the event SHA and remote `main` HEAD;
- an input version exactly equal to the Skill's `VERSION`, with the tag derived as
  `v<VERSION>`;
- a successful `Baseline` push run for that exact SHA on `main`;
- absent tag and Release names;
- a strict manifest check plus both real installer paths and clean-environment checks;
- a second `main`/tag/Release check after validation.

The validation job has read-only permissions. A separate final job has only
`contents: write`, checks the same state again, and uses one `gh release create` call
with `--target <candidate SHA>` to create a **Draft** GitHub Release and its exact
lightweight tag. It then verifies both the tag SHA and Draft state. It does not publish
the Release. The final `main` freshness checks are best effort; the hard invariant is
that the created tag resolves to the exact SHA that passed validation. Repository-wide
bypass by a maintainer is still possible unless a tag ruleset is configured, so this
is the guarded official release path rather than an absolute repository policy.

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
