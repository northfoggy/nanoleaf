# Releasing

This document is for project maintainers. Users can install published releases
with `python -m pip install nanoleaf-ctl`.

## Publishing model

Releases are published to PyPI by `.github/workflows/publish.yml` using PyPI
Trusted Publishing. The workflow does not use or store a long-lived PyPI API
token. It runs only when a GitHub release is published, and the release tag must
match `v<project.version>` from `pyproject.toml`.

The GitHub `pypi` environment is the approval boundary for the publish job.
Keep at least one required reviewer on that environment. On PyPI, configure the
Trusted Publisher with the repository owner, repository name, workflow filename
`publish.yml`, and environment name `pypi`.

Publishing a version to PyPI is effectively permanent. A file can be yanked,
but an uploaded version number cannot be reused.

## Prepare a release pull request

1. Choose the next semantic version and update `project.version` in
   `pyproject.toml`.
2. Move the release's user-visible changes into `CHANGELOG.md`.
3. Update documentation and tests for changed behavior.
4. Run:

   ```bash
   python -m pip install -e ".[dev,release]"
   python -m pytest -q
   python -m build
   python -m twine check dist/*
   git diff --check
   ```

5. Install the wheel from `dist/` into a new virtual environment and confirm
   `nanoleaf-ctl --help` works.
6. Open a pull request and wait for both the Python test matrix and package
   build job to pass.
7. Review the complete diff and merge the pull request.

## Publish

1. Confirm `main` is green and `pyproject.toml` contains the intended version.
2. Create a GitHub release with tag `v<project.version>` and release notes from
   `CHANGELOG.md`.
3. Publish the GitHub release. This starts the package workflow.
4. Approve the protected `pypi` environment deployment after checking the tag,
   commit, and workflow run.
5. Confirm the workflow succeeds and verify the version and files on PyPI.
6. Install the published package into a clean virtual environment and run
   `nanoleaf-ctl --help`.

Do not manually upload local `dist/` files except as part of a documented
recovery procedure. Do not add a PyPI password or API token to repository or
GitHub secrets when Trusted Publishing is available.

## Raspberry Pi deployment

Publishing to PyPI does not update an existing source checkout on the Raspberry
Pi. Follow `docs/INSTALLATION.md` to deploy the merged commit, run tests, and
restart the service only when runtime files changed.
