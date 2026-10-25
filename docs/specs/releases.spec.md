# Releases

# 1. Purpose and Scope

A release is a SemVer tag `vX.Y.Z` on `main`, a notes document committed with
it, and one container image built from that tag. One command cuts it; pushing
the tag builds and publishes everything else.

- `make release` (`scripts/release.py`) infers the next version from the
  history since the last tag, writes `docs/releases/YYYY-MM-DD.vX-Y-Z.ref.md`,
  commits it, and creates an annotated tag on that commit. `ARGS=--push` pushes
  `main` and the tag in one atomic push; `ARGS=--dry-run` only prints.
- `.gitea/workflows/release.yaml` runs on a `v*` tag push (or by hand for an
  existing tag). It builds `deploy/Dockerfile.api` with the version baked in,
  pushes `<registry>/<owner>/argus:vX.Y.Z` to the Gitea container registry, and
  creates the Gitea release with the notes as its body. Quality CI also builds
  (without publishing) the NanoClaw host and echo-worker images so those
  Dockerfiles cannot rot; only the control-plane image is a release artifact.
- The running process reports that version: `argus_build_info{version}`,
  `GET /api/system`, and the System tab ([operations](operations.spec.md)).

# 2. Non-Goals

- No deploy step. Argus is single-node and self-hosted: the operator pulls the
  image and restarts. The notes say whether that is reversible.
- No floating `latest` or `vX.Y` image tags. A tag names one build.
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

Work lands on `main` as Gitea PR merges plus the occasional direct commit. The
notes list first-parent entries only: a PR appears once, under its title, with
`(#N)`; the commits inside it (formatting fixes, review rounds) do not. Other
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
without reading the diff.

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

# 4. Open Questions

1. Multi-arch images. The Dockerfile already selects `sops` per architecture,
   but the runner has not been verified with QEMU/buildx; the workflow builds
   `linux/amd64` only.
2. Whether the automatic workflow token may create Gitea releases on this
   instance, or a dedicated `RELEASE_TOKEN` secret is needed.

# 5. Constraints

- `scripts/release.py` uses the standard library only, like
  `scripts/validate_docs.py`, so it runs without `make setup`.
- The release commit touches only the new notes file; the tag points at it, so
  the image contains its own notes.
- Registry credentials come from the `REGISTRY_USER` and `REGISTRY_WRITE_TOKEN`
  secrets; nothing else in the repository changes per release.

# 6. Implementation Consequences

- `scripts/release.py`, `scripts/test_release.py` (unit tests plus one run
  against a throwaway repository with a bare remote), `make release`.
- `.gitea/workflows/release.yaml`.
- `deploy/Dockerfile.api`: `ARG`/`ENV ARGUS_VERSION` and OCI labels.
- `argus_api.ops.VERSION` reads `ARGUS_VERSION`.
- `scripts/validate_docs.py`: the `releases/` naming rule and series link.
