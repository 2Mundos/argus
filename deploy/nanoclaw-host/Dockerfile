# The Argus `nanoclaw` runtime image: a NanoClaw host (with the Argus channel)
# that runs from a prepared NanoClaw checkout bind-mounted from the Docker host,
# and spawns its agent containers on that host's Docker daemon.
#
# NanoClaw hands bind-mount paths to Docker, which resolves them on the daemon's
# filesystem, so the checkout and the per-instance working directories are
# mounted at *identical* paths (see integrations/nanoclaw/README.md). Node 22 and
# a glibc at least as new as a typical host's keep its native modules working.
FROM node:22-trixie-slim

# The docker CLI (static binary) — NanoClaw's Docker driver shells out to it.
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
# docker.io (dockerd, containerd, runc) and iptables are for the sandboxed mode:
# under a runtime such as sysbox the instance runs its own daemon instead of
# using the Docker host's socket.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
        docker.io iptables \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/argus-nanoclaw-entrypoint
RUN chmod 755 /usr/local/bin/argus-nanoclaw-entrypoint

ENTRYPOINT ["argus-nanoclaw-entrypoint"]
