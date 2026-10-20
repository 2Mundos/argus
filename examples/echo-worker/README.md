# Echo worker

The smallest worker that speaks the Argus API (the `plain` runtime contract):
register with the bootstrap token from `ARGUS_BOOTSTRAP_TOKENS`, heartbeat
(reporting its `runtime`, which pool inspection shows as self-reported), claim,
complete, and leave when drained or stopped. Standard library only.

```bash
docker build -t argus-echo-worker examples/echo-worker
```

Used by the Docker integration test (`make test-docker`) and as a reference for
real workers.
