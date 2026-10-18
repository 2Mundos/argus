# Radar project layer for the NanoClaw agent image: what a worker needs to run the
# Radar gates (requires-python >= 3.12; uv.lock pins wheels for numpy, lxml, pillow,
# opencv-python-headless, psycopg[binary], onnxruntime, ...). The base carries only
# python3 3.11 from Debian, so the interpreter is installed by uv, as the `node`
# user, into ~/.local/share/uv/python, where `uv run` finds it with no network.
# Built by build-project-image.sh on top of an image that already has the tools layer.
ARG BASE
FROM ${BASE}
USER root
# Radar AGENTS.md expects graft for codebase navigation; pin matches the Hermes host.
# Prebuilt tree is staged by build-project-image.sh (npm install needs native compilers in slim).
COPY .graft-stage/graft /usr/local/lib/graft
RUN printf '%s\n' '#!/bin/sh' 'exec node /usr/local/lib/graft/dist/cli.js "$@"' > /usr/local/bin/graft \
 && chmod 0755 /usr/local/bin/graft \
 && graft --version
# Refresh the Gitea helper when the project layer is rebuilt (no full agent rebuild).
COPY --chmod=0755 gitea-api.mjs /usr/local/bin/argus-gitea-api
# Runtime libraries only, no -dev: every compiled dependency of the lock ships a
# manylinux wheel. The exception is weasyprint (pure Python), which loads pango,
# harfbuzz and fontconfig-backed fonts at import time; this is the subset of the
# list in radar/apps/api/Dockerfile.base that weasyprint >= 63 uses (no cairo).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b fonts-dejavu-core shared-mime-info \
 && rm -rf /var/lib/apt/lists/*
USER node
# Codex engine turn budget: delegate (e.g. 1800s) + gates + CI; see engines.spec.md.
ENV ARGUS_CODEX_TURN_TIMEOUT_MS=7200000
# Pinned to the minor Radar's host venv uses; the lock's requires-python is only a floor.
RUN uv python install 3.13 \
 && uv python find 3.13
LABEL dev.argus.project-layer="radar python3.13 pango codex-turn-2h"
