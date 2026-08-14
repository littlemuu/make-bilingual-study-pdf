# Development environment and baseline

Use the pinned Python dependency set and run the deterministic baseline before every
V2.3 change. The repository targets Python 3.11.7 through `.python-version`.

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

The baseline also requires Poppler's `pdftoppm` executable on `PATH`. On Ubuntu:

```bash
sudo apt-get update
sudo apt-get install --yes --no-install-recommends poppler-utils
```

The V2.3 DOCX/PDF forward workflow additionally needs `pdftotext`, Pandoc, Fontconfig,
LibreOffice Writer, Noto Sans/Noto CJK fonts, and DejaVu Sans Mono. On Ubuntu 24.04 the
CI job installs:

```bash
sudo apt-get install --yes --no-install-recommends \
  fontconfig fonts-dejavu-core fonts-noto-cjk fonts-noto-core \
  libreoffice-writer pandoc poppler-utils
```

XeLaTeX/latexmk remain optional for the separate TeX output path. MinerU itself and its
models are not development dependencies; tests consume only committed frozen fixtures.

## Run the baseline

```bash
.venv/bin/python -m pip check
.venv/bin/python scripts/self_test.py
.venv/bin/python scripts/v23_profile_test.py
.venv/bin/python scripts/v23_mineru_test.py
.venv/bin/python scripts/v23_ir_source_test.py
.venv/bin/python scripts/v23_output_test.py
.venv/bin/python scripts/v23_docx_test.py
.venv/bin/python scripts/v23_visual_gate_test.py
.venv/bin/python scripts/pipeline.py validate-profile assignment-en-zh
.venv/bin/python scripts/pipeline.py validate-profile academic-paper-en-zh
.venv/bin/python scripts/pipeline.py validate-profile lecture-notes-en-zh
```

On Windows, replace `.venv/bin/python` with `.\.venv\Scripts\python.exe`.

The expected result is 19 passing self-tests plus a passed validation report for the
default `assignment-en-zh` Profile. A missing Python module or `pdftoppm` is an
environment failure, not a product regression. Do not begin V2.3 implementation until
both commands pass from a clean checkout.

GitHub Actions runs the same quick matrix on Ubuntu 24.04 for every pull request and
every push to `main`. A separate `automated-forward` job runs both schema V2 Profiles
through LibreOffice PDF conversion and uploads evidence. It intentionally stops before
human visual approval and asserts that finalization is still blocked. Action revisions
and Python packages are pinned; both jobs print the resolved toolchain into their logs.
