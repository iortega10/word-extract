# Releasing to PyPI

Two distributions are released together, in lockstep: `docextract-core` first, then
`word-extract` (which pins `docextract-core>=0.1,<0.2`). `.github/workflows/release.yml` does
both with PyPI Trusted Publishing, so no API token is stored anywhere.

**Before the first release (yours to settle, not the code's):** confirm the ownership of the
work with your employer in writing, and publish from a personal PyPI account on a personal
device. A PyPI release is permanent and public; a version number can never be reused.

## One-time setup

1. Create accounts on pypi.org and test.pypi.org with 2FA; keep the transfer codes.
2. On **each** site, add a *pending publisher* for **both** project names. PyPI allows one
   project per (owner, repository, workflow, environment), so the two use different
   environment names. Owner `iortega10`, repository `word-extract`, workflow `release.yml`:
   - `docextract-core`: environment `pypi` (PyPI) or `testpypi` (TestPyPI);
   - `word-extract`: environment `pypi-main` (PyPI) or `testpypi-main` (TestPyPI).
3. In the GitHub repository, the environments `pypi`, `testpypi`, `pypi-main` and
   `testpypi-main` are created on first use. Optionally require a manual approval on the
   `pypi*` ones.
4. Make the repository public when you are ready (the README's links point at it).

## Each release

1. Set `version` in **both** `pyproject.toml` files to the exact version you will tag
   (`0.1.0rc1` for the dry run, `0.1.0` for the real one); the workflow refuses a tag that
   does not match both. Bump the core pin if the minor changes. While a release candidate is on the index the pin
   reads `docextract-core>=0.1.0rc1,<0.2` (a plain `>=0.1` would exclude `0.1.0rc1`); set it to
   `>=0.1.0,<0.2` for the real `0.1.0`.
2. Run `python -m pytest -q` and `python tools/update_behavior_ledger.py --check` locally.
3. Dry run: set the versions to `0.1.0rc1`, commit, tag `v0.1.0rc1`. The workflow publishes to **TestPyPI**. In a clean virtualenv:
   `pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ "word-extract[mcp]==0.1.0rc1"`
   (pin the version rather than using `--pre`, which would also pull pre-release dependencies
   such as a beta `lxml`)
   and run `wordextract ingest ...` on a fixture.
4. Real release: tag `v0.1.0`. The workflow tests, builds, runs `twine check`, then publishes
   the core and then `word-extract` to PyPI.
5. Verify: `pip install word-extract` in a fresh virtualenv.

If a release is wrong, fix it with a new version; do not try to reuse the number.
