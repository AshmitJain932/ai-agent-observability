# Releasing to PyPI

Both packages, `aobs` (the SDK) and `aobs-server` (collector and tools), are released together with
the same version number. Publishing runs in GitHub Actions ([`release.yml`](https://github.com/AshmitJain932/ai-agent-observability/blob/master/.github/workflows/release.yml))
through PyPI Trusted Publishing, so no API token or password is stored anywhere.

## One-time setup (maintainer, on pypi.org)

1. Sign in to <https://pypi.org> (create an account and turn on two-factor authentication if needed).
2. Go to **Your account → Publishing → Add a new pending publisher** and add a GitHub publisher
   **twice**, once per package:

   | Field             | First entry              | Second entry             |
   |-------------------|--------------------------|--------------------------|
   | PyPI project name | `aobs`                   | `aobs-server`            |
   | Owner             | `AshmitJain932`          | `AshmitJain932`          |
   | Repository name   | `ai-agent-observability` | `ai-agent-observability` |
   | Workflow name     | `release.yml`            | `release.yml`            |
   | Environment name  | `pypi`                   | `pypi-server`            |

3. On GitHub, open **Settings → Environments** and create two environments, `pypi` and
   `pypi-server`. PyPI refuses two pending publishers with the same environment, which is why
   each package has its own. Optionally add yourself as a required reviewer on both so every
   release waits for your click.

## Each release

1. Set the same version in `sdk/python/pyproject.toml`, `sdk/python/src/aobs/_version.py` and
   `server/pyproject.toml`, run `uv lock`, and add a `CHANGELOG.md` entry.
2. Merge to `master`, then tag and push:

   ```bash
   git tag v0.1.0a1
   git push origin v0.1.0a1
   ```

3. The **Release** workflow checks that the tag matches the versions, builds, runs `twine check`,
   installs the wheels in a clean environment, and publishes both packages.

PyPI never accepts the same version twice: if something is wrong after publishing, bump the version
(for example `0.1.0a2`) rather than re-tagging.
