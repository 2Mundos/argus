---
status: implemented
created: 2026-10-18
updated: 2026-11-11
---

# Releases

# 1. Purpose and Scope

A release is a SemVer tag `vX.Y.Z` on `main`, a notes document committed with
it, and one container image (`linux/amd64` and `linux/arm64`) built from that tag. One command cuts it; pushing
the tag builds and publishes everything else.

- `make release` (`scripts/release.py`) infers the next version from the
  history since the last tag, writes `docs/releases/YYYY-MM-DD.vX-Y-Z.ref.md`,
  commits it, and creates an annotated tag on that commit. `ARGS=--push` pushes
  `main` and the tag in one atomic push; `ARGS=--dry-run` only prints.
- `.github/workflows/release.yaml` runs on a `v*` tag push (or by hand for an
  existing tag). It builds `deploy/Dockerfile.api` with the version baked in,
  pushes `ghcr.io/<owner>/argus:vX.Y.Z` to the GitHub container registry, and,
  once the worker images are verified (3.9), moves `latest` (3.7) and
  creates the GitHub release with the notes as its body and `deploy/install.sh`
  as its one asset ([launcher](launcher.spec.md) 3.1); for a release that
  already exists it leaves the notes alone and re-attaches that asset.
- Quality CI ignores pushes and PRs whose diff is only under `docs/releases/`
  (3.8): the notes commit that accompanies the tag does not re-run the suite.
  On every other change, Quality still builds (without publishing) the NanoClaw
  host and echo-worker images so those Dockerfiles cannot rot; the echo worker
  is never published.
- Further jobs build and push the worker images
  `ghcr.io/<owner>/argus-nanoclaw-host:vX.Y.Z` and `…/argus-nanoclaw-agent:vX.Y.Z`
  ([local workers](local-workers.spec.md) 3.1), with the same `latest` rule. Both
  are multi-arch: each architecture builds natively on its own runner (the
  ~6 GB agent image is impractical under QEMU) and pushes `vX.Y.Z-amd64` /
  `vX.Y.Z-arm64`; a manifest job joins them into the `vX.Y.Z` manifests and
  verifies them (3.9). The per-architecture tags remain in the registry.
- The image carries the host launcher (`/app/bin/argus`) and every release's
  notes (`/app/releases/`), so `argus update` installs the matching launcher
  and shows the Upgrading sections it skips over ([launcher](launcher.spec.md)).
- The running process reports that version: `argus_build_info{version}`,
  `GET /api/system`, and the Home System widgets ([operations](operations.spec.md)).

# 2. Non-Goals

- No deploy step. Argus is single-node and self-hosted: the operator pulls the
  image and restarts. The notes say whether that is reversible.
- No floating `vX.Y` image tags. `vX.Y.Z` names one build; `latest` is the only
  moving tag (3.7).
- No version numbers in `pyproject.toml` or `package.json`. The tag is the only
  source of the version; nothing in the tree is bumped.
- No release branches or backports. A fix ships as the next patch from `main`.

# 3. Decisions Made

## 3.1 Version inference

The release range is `<previous tag>..HEAD` (the whole history for the first
release). Every commit is scanned for a breaking marker (`type!:` or a
`BREAKING CHANGE:` footer); the first-parent history decides the rest:

| Range contains | `0.y.z` | `≥ 1.0.0` |
| --- | --- | --- |
| a breaking marker | minor | major |
| a `feat` change | minor | minor |
| anything else | patch | patch |

Before 1.0 a breaking change bumps the minor version, as SemVer allows; 1.0.0 is
cut deliberately with `--version v1.0.0`. `--version` always overrides inference.

## 3.2 Notes are built from the first-parent history

Work lands on `main` as PR merge commits plus the occasional direct commit. A
merge commit carries the PR title in its subject (Gitea) or as the first body
line (GitHub). The notes list first-parent entries only: a PR appears once,
under its title, with `(#N)`; the commits inside it (formatting fixes, review rounds) do not. Other
merge commits (`Merge main into …`) are skipped. Entries are grouped by
Conventional Commit type into Features (`feat`), Fixes (`fix`) and Maintenance
(everything else), with the scope kept as a prefix (`accounts: …`).

## 3.3 Notes say whether an upgrade can be rolled back

The migration runner refuses a database newer than the build
([single-node](single-node.spec.md)), so an upgrade that adds a migration cannot
be undone by running the previous image. Every release's notes open with an
**Upgrading** section that lists the migrations added since the previous tag
and, when there are any, tells the operator to back up the database (or confirm
the Litestream replica is current) first and that rolling back means restoring
that backup. With no migrations it says rolling back is safe. Specs added or
changed in the range are linked there too, so behaviour changes are found
without reading the diff. Those links are absolute and pinned to the tag
(`https://github.com/2Mundos/argus/blob/vX.Y.Z/docs/specs/<name>`): the same
text is the GitHub release body, where a relative link resolves against the
release page and 404s, and a tag-pinned link shows the spec as it shipped.

## 3.4 Guards

The script refuses, before changing anything, when: the branch is not `main`;
the worktree is dirty (except `--dry-run`); `HEAD` differs from `origin/main`
after a fetch (a release is cut from what is published, nothing more and nothing
stale); the range is empty; or the tag already exists. The workflow refuses a
tag that is not `vX.Y.Z`, whose commit is not on `origin/main`, or that has no
notes file.

## 3.5 The version reaches the binary through the image

`deploy/Dockerfile.api` takes `ARG ARGUS_VERSION` and sets it as an environment
variable in the final image; `argus_api.ops.VERSION` reads it. Builds without the
argument (local, CI) report `0.0.0+dev`. The image also carries the OCI
`version`, `revision` and `source` labels.

## 3.6 Notes are indexed as a series

`docs/README.md` links `releases/` once instead of every file. The docs check
requires files there to be named `YYYY-MM-DD.vX-Y-Z.ref.md`.

## 3.7 `latest` follows the newest release

The `finalize` job (3.9) tags the API image and both worker images `latest`
when the tag it builds is the highest
`vX.Y.Z` in the repository, so a dispatch rebuild of an older tag never moves
`latest` backwards. `latest` is for trying Argus; a deployment pins `vX.Y.Z`,
because pulling `latest` can apply migrations that make the upgrade one-way (3.3)
without the operator reading the notes first. The README names no version: its
`docker run` uses `latest` and says to replace it with a `vX.Y.Z` tag to pin.

## 3.8 Notes-only commits skip Quality

`make release` commits only the new notes file and pushes `main` with the tag
atomically. Quality's `on.push` / `on.pull_request` use
`paths-ignore: docs/releases/**`, so that notes commit does not start Quality;
only the Release workflow runs for the tag. A commit that touches anything
outside `docs/releases/` still runs Quality as usual. CodeQL default setup is
configured in the repository's Advanced Security settings (not in this tree) and
still runs on the notes push unless that setup is given a path filter or moved
to an advanced workflow with the same ignore.

## 3.9 A release is public only when it is ready

A release is **ready** when `ghcr.io/<owner>/argus:vX.Y.Z` and
`ghcr.io/<owner>/argus-nanoclaw-{host,agent}:vX.Y.Z` all exist with `linux/amd64`
and `linux/arm64` variants labelled `org.opencontainers.image.version=vX.Y.Z`.
Only a ready release may get a public GitHub release or move `latest`, because
`install.sh` and the launcher pick the newest GitHub release and then need the
same version of every image ([local workers](local-workers.spec.md) 3.5).

The jobs run in this order, each `needs:` the one before:

1. `publish`: checks the tag (3.1) and pushes the API image `vX.Y.Z` only.
2. `worker-images`: builds and pushes `vX.Y.Z-amd64` and `vX.Y.Z-arm64` of both
   worker images, one runner per architecture.
3. `worker-manifests`: joins them into the `vX.Y.Z` manifests, then inspects all
   three images and fails unless both platforms and the version label are there.
4. `finalize`: moves `latest` for all three images when the tag is the newest (3.7),
   then creates the GitHub release (or re-attaches `install.sh` to an existing one).
   It is the only job that does either.

If any build or the manifest job fails, `finalize` does not run: the versioned
images may exist but no release is public and `latest` stays where it was. Re-run
the workflow (or dispatch the tag): every step is repeatable. Images and
manifests are rebuilt under the same tag, `latest` is set to the same manifests,
`install.sh` is uploaded with `--clobber`, and an existing release keeps its
notes. Every job takes the tag from `publish`, so one run never mixes versions.

# 4. Open Questions

None.

# 5. Constraints

- `scripts/release.py` uses the standard library only, like
  `scripts/validate_docs.py`, so it runs without `make setup`.
- The release commit touches only the new notes file; the tag points at it, so
  the image contains its own notes.
- The workflow's own `GITHUB_TOKEN` (`contents: write`, `packages: write`)
  pushes the image and creates the release; no repository secrets are needed,
  and nothing else in the repository changes per release.

# 6. Implementation Consequences

- `scripts/release.py`, `scripts/test_release.py` (unit tests plus one run
  against a throwaway repository with a bare remote), `make release`.
- `.github/workflows/release.yaml`, `scripts/test_release_workflow.py` (job ordering
  and permissions, with negative cases).
- `.github/workflows/quality.yaml`: `paths-ignore` for `docs/releases/**` (3.8).
- `deploy/Dockerfile.api`: `ARG`/`ENV ARGUS_VERSION` and OCI labels.
- `argus_api.ops.VERSION` reads `ARGUS_VERSION`.
- `scripts/validate_docs.py`: the `releases/` naming rule and series link.
