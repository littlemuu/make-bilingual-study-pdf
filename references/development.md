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

The complete document workflow additionally needs `pdftotext`, Pandoc, Fontconfig,
LibreOffice, reviewed CJK fonts, and optionally XeLaTeX/latexmk. Those tools are not
needed by the repository self-test and remain operating-system dependencies.

## Run the baseline

```bash
.venv/bin/python -m pip check
.venv/bin/python scripts/self_test.py
.venv/bin/python scripts/pipeline.py validate-profile assignment-en-zh
```

On Windows, replace `.venv/bin/python` with `.\.venv\Scripts\python.exe`.

The expected result is 19 passing self-tests plus a passed validation report for the
default `assignment-en-zh` Profile. A missing Python module or `pdftoppm` is an
environment failure, not a product regression. Do not begin V2.3 implementation until
both commands pass from a clean checkout.

GitHub Actions runs the same baseline on Ubuntu 24.04 for every pull request and every
push to `main`. Its action revisions and Python packages are pinned; the job prints the
resolved Python, package, and Poppler versions into the log for auditability.
