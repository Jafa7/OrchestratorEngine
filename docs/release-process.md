# Release Process

OrchestratorEngine publishes immutable Git tags and GitHub Release assets. The
repository release workflow handles deterministic packaging and publication;
it does not decide when a release should exist, change versions, create tags or
modify source code.

## Maintainer boundary

Before creating a tag:

1. Update `pyproject.toml`, `src/orchestrator_engine/__init__.py`, `uv.lock`,
   `CHANGELOG.md`, the setup guide and the upgrade guide to the same version.
2. Run the complete release-candidate gate once on the final source tree.
3. Commit and push the release candidate to `main`.
4. Require the repository `CI` workflow for that exact commit SHA to complete
   successfully. `ci watch --expected-head-sha FULL_SHA --workflow-name CI`
   can wait and wake the dispatching chat without model polling.
5. Create an annotated `vX.Y.Z` tag on that exact commit and push the tag.

The tag is the explicit human/agent authorization boundary. Configure a GitHub
ruleset that prevents deletion or force-update of `v*` tags. The workflow never
creates or moves a tag and never publishes from a commit outside `main`.

## Automated publication

`.github/workflows/release.yml` starts for a `v*.*.*` tag and fails closed
unless all of these conditions hold:

- the tag is exactly `v` plus the version verified by
  `tools/check_release_consistency.py`;
- the remote tag object is annotated, targets the workflow event SHA and the
  checkout HEAD exactly, as verified by `tools/validate_release_source.py`;
- the tagged commit is reachable from `origin/main`;
- the latest matching `CI` push run for the exact SHA, workflow and branch is
  `completed` with conclusion `success`;
- wheel and sdist names match the version;
- an installed-wheel version check and full clean-fixture conformance pass;
- uploaded asset names, sizes and GitHub SHA-256 digests match the local build.

The workflow creates or reuses a draft release, uploads the wheel, sdist and
`SHA256SUMS`, verifies them through a readback, and only then publishes the
release as latest. Builds use `SOURCE_DATE_EPOCH` from the tagged commit. A
retry may replace assets only after checking that the release is still a
draft. An already published release is never modified automatically: the
workflow succeeds only if its existing assets exactly match the rebuilt bundle.

Release notes are generated from the matching version section in
`CHANGELOG.md`; unrelated historical or unreleased sections are excluded.
Source provenance is emitted as bounded JSON after fetching the remote tag and
main branch into temporary internal refs. This deliberately does not trust or
modify refs produced by `actions/checkout`; both internal refs are removed
after validation.
GitHub-hosted `gh` is used only inside the repository workflow. Adopters do not
need GitHub CLI to install a tagged release; local `gh` remains an optional
prerequisite only for `ci watch` and `pr watch`.

## Failure recovery

Do not move or recreate a published tag to repair a release. Inspect the failed
workflow step:

- before draft creation, fix source metadata or wait for exact-SHA CI, then
  create a new patch version when the tagged source itself is wrong;
- while a draft exists, rerun after correcting a transient GitHub or packaging
  environment failure; draft assets may be replaced and verified;
- after publication, keep the release immutable. A digest or source defect
  requires a new version and changelog entry.

The workflow stores complete command output in GitHub Actions. Host agents
should start from the compact run conclusion and read only the failed step when
diagnosis is required.
