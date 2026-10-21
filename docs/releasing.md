# Releasing to PyPI

Two packages are published separately: `aobs` (the SDK, `sdk/python/`) and `aobs-server`
(`server/`). Nothing is published automatically. These steps need a PyPI account that owns (or
will create) both projects.

## Before a release

1. CI is green on `master` (lint, types, the full suite on Python 3.11, 3.12 and 3.13).
2. Set the version in both `pyproject.toml` files. Use a pre-release version while the API may
   still change, e.g. `0.1.0a1` (PEP 440). `aobs-server` and `aobs` are versioned together.
3. Build and check:

   ```bash
   rm -rf dist
   uv build --out-dir dist sdk/python
   uv build --out-dir dist server
   uvx twine check dist/*
   ```

4. Install the built wheels in a fresh environment and try them:

   ```bash
   uv venv /tmp/aobs-release && uv pip install --python /tmp/aobs-release dist/*.whl
   /tmp/aobs-release/bin/python -c "import aobs, aobs.safety; print(aobs.__version__)"
   /tmp/aobs-release/bin/aobs-server --help
   ```

## Publish

Upload to TestPyPI first, check the project page and an install from it, then to PyPI:

```bash
uv publish --publish-url https://test.pypi.org/legacy/ dist/*   # TestPyPI
uv publish dist/*                                                # PyPI
```

`uv publish` asks for a token (or uses `UV_PUBLISH_TOKEN`). Prefer a token scoped to the two
projects, or set up PyPI trusted publishing from a GitHub Actions workflow so no token is stored.
Never commit a token.

## Notes

- A version can be uploaded only once; a mistake needs a new version number.
- Package names are not reserved until the first upload, and PyPI may refuse a name it considers
  too similar to an existing project.
- The dashboard's sample datasets are read from the repository's `examples/` directory, so an
  installed `aobs-server` starts without them (it says so); `--data-dir` and stored traces work.
