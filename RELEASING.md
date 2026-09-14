# Releasing

Publishing runs on [trusted publishing](https://docs.pypi.org/trusted-publishers/),
so there is no API token to create, store or rotate. PyPI verifies a short-lived
OIDC token that GitHub mints for this specific workflow in this specific repo.

## One-time setup (needs your PyPI account)

This cannot be done from CI — someone has to do it signed in to PyPI.

1. Go to <https://pypi.org/manage/account/publishing/> and add a **pending
   publisher** (the name `aiodahua` is unregistered, so this is the path that
   works before a first upload exists):

   | Field | Value |
   |---|---|
   | PyPI Project Name | `aiodahua` |
   | Owner | `brianegge` |
   | Repository name | `aiodahua` |
   | Workflow name | `workflow.yml` |
   | Environment name | `pypi` |

2. In the GitHub repo, create an environment named `pypi`
   (Settings → Environments → New environment). It needs no secrets. Add
   required reviewers there if you want a manual gate before each upload.

3. Optional, for rehearsing against TestPyPI: repeat step 1 at
   <https://test.pypi.org/manage/account/publishing/> and create a `testpypi`
   environment.

## Cutting a release

> The **Workflow name** must match the filename in `.github/workflows/`
> exactly. PyPI checks it against the OIDC claim GitHub mints, so a mismatch
> fails at the upload step with an unhelpful error after everything else has
> already passed. This repo uses `workflow.yml`.

1. Bump `version` in `pyproject.toml` **and** `__version__` in
   `src/aiodahua/__init__.py`. They are asserted equal by the test suite.
2. Merge that to `main` with CI green.
3. Publish a GitHub Release tagged `v<version>` — e.g. `v0.4.0` for version
   `0.4.0`. The workflow refuses to upload if the tag and the packaged version
   disagree, or if that version is already on PyPI, because **a PyPI version
   can never be reused or overwritten**.

## Rehearsing first

Actions → Release → Run workflow → `testpypi`. That runs the identical build
and upload path against TestPyPI, which is the cheap way to find a packaging
problem given a real release is unrepeatable.

## Versioning

`aiodahua` is pre-1.0 and the API is still moving. Home Assistant pins an exact
version in `manifest.json`, so any release that changes behaviour needs a
matching bump there too — see
[brianegge/dahua#64](https://github.com/brianegge/dahua/pull/64).
