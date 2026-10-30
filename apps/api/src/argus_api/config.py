import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from argus_core.difficulty import Difficulty

# Defaults for the lost-spike rule (agent-hygiene.spec.md 3); the rule and its
# configuration read them here so they cannot disagree.
DEFAULT_LOST_SPIKE_WINDOW_HOURS = 24
DEFAULT_LOST_SPIKE_MIN_COUNT = 5
DEFAULT_LOST_SPIKE_FACTOR = 2.0


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    admin_token: str | None = None
    artifact_max_bytes: int = 100 * 1024 * 1024
    # Sum of artifact sizes one agent may own; default 2 GiB.
    artifact_agent_quota_bytes: int = 2 * 1024 * 1024 * 1024
    # Orchestrator attachments one task may hold (no worker lease, no agent quota).
    attachment_max_per_task: int = 50
    # Absolute lifetime of an agent bearer token; heartbeats slide it forward.
    agent_token_ttl_s: int = 7 * 24 * 3600
    # Wall-clock cap per task attempt unless the task or its kind sets one;
    # 0 disables. Heartbeats keep a lease alive, only this ends a stuck attempt.
    task_timeout_s: int = 6 * 3600
    # Policy gate (3.10): tasks one agent may create per hour before further
    # tasks wait for human approval. 0 disables the gate.
    max_tasks_per_agent_hour: int = 100
    dashboard_dir: Path | None = None
    # Workers either propose knowledge changes for review, or publish directly.
    knowledge_worker_writes: Literal["propose", "publish"] = "propose"
    knowledge_stale_days: int = 30
    secrets_file: Path | None = None  # default: <data_dir>/secrets.sops.yaml
    sops_bin: str = "sops"
    account_cooldown_minutes: int = 60  # when a provider gives no reset time
    # How containers Argus starts reach it, e.g. http://host.docker.internal:8000.
    public_url: str | None = None
    reconcile_interval_s: float = 5.0
    # Worker templates (worker launch 3). Nothing here has a personal default: a
    # template that needs a value is simply not ready until it is set.
    nanoclaw_install_dir: str | None = None
    nanoclaw_instances_root: str | None = None
    nanoclaw_agent_image: str | None = None
    nanoclaw_host_image: str = "argus-nanoclaw-host:dev"
    nanoclaw_docker_runtime: str | None = None
    nanoclaw_agent_image_archive: str | None = None
    nanoclaw_allow_docker_socket: bool = False
    nanoclaw_docker_socket: str = "/var/run/docker.sock"
    nanoclaw_user: str | None = None
    nanoclaw_group_add: tuple[str, ...] = ()
    # What a guided NanoClaw worker inherits so it joins the operator's gateway
    # and account policy; unset keeps the pool defaults (no accounts, default URL).
    nanoclaw_onecli_url: str | None = None
    nanoclaw_network: str | None = None  # Docker network that reaches the gateway
    worker_images: tuple[str, ...] = ()  # trusted `argus-worker` images
    # Project runtime images (project runtime spec): derived per project, built on
    # this node's Docker daemon. Off: workers are told so and run on their base image.
    project_runtime: bool = True
    runtime_image_repo: str = "argus-project-runtime"
    runtime_build_timeout_s: int = 1800
    docker_hosts: tuple[str, ...] = ()  # extra Docker nodes the guided flow may use
    # Local model (engines spec). Off unless a URL is set; one allowed model.
    local_model_url: str | None = None
    local_model: str | None = None
    local_max_input_fraction: float = 0.5  # of the served context
    local_output_reserve: int = 2048
    local_chat_template_overhead: int = 512
    local_timeout_s: float = 120.0
    engines_ledger: Path | None = None  # default: <data_dir>/engines-ledger.json
    # Executor order per difficulty level, over the defaults in engines/router.py
    # (ARGUS_DIFFICULTY_EXECUTORS, JSON: {"standard": ["codex", "claude"]}).
    difficulty_executors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Dedicated Codex login (codex accounts spec 3): the pinned official client,
    # either as an image (run with docker) or a binary path. Off when neither is set.
    codex_login_image: str | None = None
    codex_bin: str | None = None
    # Dedicated Claude login (claude accounts spec 3): `claude setup-token`, same shape.
    claude_login_image: str | None = None
    claude_bin: str | None = None
    # Price table for the cost view (cost view 3.2); default is the shipped one.
    pricing_file: Path | None = None
    # Agent hygiene (agent-hygiene.spec.md): default age before an admin may
    # archive lost/gone identities. Unset disables archive until the caller
    # passes older_than_days explicitly.
    agent_archive_after_days: int | None = None
    # Abnormal jump in newly lost agents against the preceding window.
    lost_spike_window_hours: int = DEFAULT_LOST_SPIKE_WINDOW_HOURS
    lost_spike_min_count: int = DEFAULT_LOST_SPIKE_MIN_COUNT
    lost_spike_factor: float = DEFAULT_LOST_SPIKE_FACTOR
    # Claimed task with no progress / live log / activity_at for this long is
    # flagged quiet (inbox + rail); never expires the lease (stall-visibility).
    quiet_claim_s: int = 1200
    # A claim already flagged quiet that stays silent this long is expired as
    # `stalled` and retried. 0 disables (stall-visibility 3.6).
    stall_fail_s: int = 3600
    # Workspace cleanup (workspace-cleanup.spec.md 3.1): off, and dry-run when on.
    workspace_cleanup_enabled: bool = False
    workspace_cleanup_dry_run: bool = True
    workspace_cleanup_archive_root: str | None = None
    workspace_cleanup_min_age_hours: int = 168
    workspace_cleanup_interval_s: float = 3600.0
    workspace_cleanup_run_timeout_s: float = 1800.0
    workspace_cleanup_max_instances: int = 20
    workspace_cleanup_max_archive_bytes: int = 2 * 1024 * 1024 * 1024
    workspace_cleanup_max_attempts: int = 3
    workspace_cleanup_low_disk_bytes: int = 10 * 1024 * 1024 * 1024
    workspace_cleanup_git_timeout_s: float = 300.0
    # Disk guard admission (operations.spec.md 3.5): warn/refuse hysteresis.
    disk_guard_enabled: bool = True
    disk_guard_warn_bytes: int = 5 * 1024 * 1024 * 1024
    disk_guard_warn_clear_bytes: int = 6 * 1024 * 1024 * 1024
    disk_guard_refuse_bytes: int = 2 * 1024 * 1024 * 1024
    disk_guard_resume_bytes: int = int(2.5 * 1024 * 1024 * 1024)
    disk_guard_interval_s: float = 5.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "argus.db"

    @property
    def secrets_path(self) -> Path:
        return self.secrets_file or self.data_dir / "secrets.sops.yaml"

    @property
    def model_policy_path(self) -> Path:
        return self.data_dir / "model-policy.json"

    @property
    def engines_ledger_path(self) -> Path:
        return self.engines_ledger or self.data_dir / "engines-ledger.json"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            data_dir=Path(os.environ.get("ARGUS_DATA_DIR", "data")),
            admin_token=os.environ.get("ARGUS_ADMIN_TOKEN") or None,
            artifact_max_bytes=int(
                os.environ.get("ARGUS_ARTIFACT_MAX_BYTES", 100 * 1024 * 1024)
            ),
            artifact_agent_quota_bytes=int(
                os.environ.get(
                    "ARGUS_ARTIFACT_AGENT_QUOTA_BYTES", 2 * 1024 * 1024 * 1024
                )
            ),
            attachment_max_per_task=int(
                os.environ.get("ARGUS_ATTACHMENT_MAX_PER_TASK", 50)
            ),
            agent_token_ttl_s=int(
                os.environ.get("ARGUS_AGENT_TOKEN_TTL_S", 7 * 24 * 3600)
            ),
            task_timeout_s=int(os.environ.get("ARGUS_TASK_TIMEOUT_S", 6 * 3600)),
            max_tasks_per_agent_hour=int(
                os.environ.get("ARGUS_MAX_TASKS_PER_AGENT_HOUR", 100)
            ),
            knowledge_worker_writes=(
                "publish"
                if os.environ.get("ARGUS_KNOWLEDGE_WORKER_WRITES") == "publish"
                else "propose"
            ),
            knowledge_stale_days=int(os.environ.get("ARGUS_KNOWLEDGE_STALE_DAYS", 30)),
            secrets_file=(
                Path(f) if (f := os.environ.get("ARGUS_SECRETS_FILE")) else None
            ),
            sops_bin=os.environ.get("ARGUS_SOPS_BIN", "sops"),
            account_cooldown_minutes=int(
                os.environ.get("ARGUS_ACCOUNT_COOLDOWN_MINUTES", 60)
            ),
            public_url=os.environ.get("ARGUS_PUBLIC_URL") or None,
            reconcile_interval_s=float(os.environ.get("ARGUS_RECONCILE_INTERVAL_S", 5)),
            nanoclaw_install_dir=os.environ.get("ARGUS_NANOCLAW_INSTALL_DIR") or None,
            nanoclaw_instances_root=os.environ.get("ARGUS_NANOCLAW_INSTANCES_ROOT")
            or None,
            nanoclaw_agent_image=os.environ.get("ARGUS_NANOCLAW_AGENT_IMAGE") or None,
            nanoclaw_host_image=os.environ.get("ARGUS_NANOCLAW_HOST_IMAGE")
            or "argus-nanoclaw-host:dev",
            nanoclaw_docker_runtime=os.environ.get("ARGUS_NANOCLAW_DOCKER_RUNTIME")
            or None,
            nanoclaw_agent_image_archive=os.environ.get(
                "ARGUS_NANOCLAW_AGENT_IMAGE_ARCHIVE"
            )
            or None,
            nanoclaw_allow_docker_socket=os.environ.get(
                "ARGUS_NANOCLAW_ALLOW_DOCKER_SOCKET"
            )
            == "true",
            nanoclaw_docker_socket=os.environ.get("ARGUS_NANOCLAW_DOCKER_SOCKET")
            or "/var/run/docker.sock",
            nanoclaw_user=os.environ.get("ARGUS_NANOCLAW_USER") or None,
            nanoclaw_group_add=_csv("ARGUS_NANOCLAW_GROUP_ADD"),
            nanoclaw_onecli_url=os.environ.get("ARGUS_NANOCLAW_ONECLI_URL") or None,
            nanoclaw_network=os.environ.get("ARGUS_NANOCLAW_NETWORK") or None,
            worker_images=_csv("ARGUS_WORKER_IMAGES"),
            project_runtime=os.environ.get("ARGUS_PROJECT_RUNTIME", "on") != "off",
            runtime_image_repo=os.environ.get("ARGUS_RUNTIME_IMAGE_REPO")
            or "argus-project-runtime",
            runtime_build_timeout_s=int(
                os.environ.get("ARGUS_RUNTIME_BUILD_TIMEOUT_S", 1800)
            ),
            docker_hosts=_csv("ARGUS_FLEET_DOCKER_HOSTS"),
            local_model_url=os.environ.get("ARGUS_LOCAL_MODEL_URL") or None,
            local_model=os.environ.get("ARGUS_LOCAL_MODEL") or None,
            local_max_input_fraction=float(
                os.environ.get("ARGUS_LOCAL_MAX_INPUT_FRACTION", 0.5)
            ),
            local_output_reserve=int(
                os.environ.get("ARGUS_LOCAL_OUTPUT_RESERVE", 2048)
            ),
            local_chat_template_overhead=int(
                os.environ.get("ARGUS_LOCAL_CHAT_TEMPLATE_OVERHEAD", 512)
            ),
            local_timeout_s=float(os.environ.get("ARGUS_LOCAL_TIMEOUT_S", 120)),
            engines_ledger=(
                Path(f) if (f := os.environ.get("ARGUS_ENGINES_LEDGER")) else None
            ),
            difficulty_executors=_difficulty_executors(),
            codex_login_image=os.environ.get("ARGUS_CODEX_LOGIN_IMAGE") or None,
            codex_bin=os.environ.get("ARGUS_CODEX_BIN") or None,
            claude_login_image=os.environ.get("ARGUS_CLAUDE_LOGIN_IMAGE") or None,
            claude_bin=os.environ.get("ARGUS_CLAUDE_BIN") or None,
            pricing_file=(
                Path(f) if (f := os.environ.get("ARGUS_PRICING_FILE")) else None
            ),
            dashboard_dir=(
                Path(d) if (d := os.environ.get("ARGUS_DASHBOARD_DIR")) else None
            ),
            agent_archive_after_days=(
                int(v)
                if (v := os.environ.get("ARGUS_AGENT_ARCHIVE_AFTER_DAYS") or None)
                else None
            ),
            lost_spike_window_hours=int(
                os.environ.get(
                    "ARGUS_LOST_SPIKE_WINDOW_HOURS", DEFAULT_LOST_SPIKE_WINDOW_HOURS
                )
            ),
            lost_spike_min_count=int(
                os.environ.get(
                    "ARGUS_LOST_SPIKE_MIN_COUNT", DEFAULT_LOST_SPIKE_MIN_COUNT
                )
            ),
            lost_spike_factor=float(
                os.environ.get("ARGUS_LOST_SPIKE_FACTOR", DEFAULT_LOST_SPIKE_FACTOR)
            ),
            quiet_claim_s=int(os.environ.get("ARGUS_QUIET_CLAIM_S", 1200)),
            stall_fail_s=int(os.environ.get("ARGUS_STALL_FAIL_S", 3600)),
            workspace_cleanup_enabled=os.environ.get("ARGUS_WORKSPACE_CLEANUP_ENABLED")
            == "true",
            workspace_cleanup_dry_run=os.environ.get("ARGUS_WORKSPACE_CLEANUP_DRY_RUN")
            != "false",
            workspace_cleanup_archive_root=os.environ.get(
                "ARGUS_WORKSPACE_CLEANUP_ARCHIVE_ROOT"
            )
            or None,
            workspace_cleanup_min_age_hours=int(
                os.environ.get("ARGUS_WORKSPACE_CLEANUP_MIN_AGE_HOURS", 168)
            ),
            workspace_cleanup_interval_s=float(
                os.environ.get("ARGUS_WORKSPACE_CLEANUP_INTERVAL_S", 3600)
            ),
            workspace_cleanup_run_timeout_s=float(
                os.environ.get("ARGUS_WORKSPACE_CLEANUP_RUN_TIMEOUT_S", 1800)
            ),
            workspace_cleanup_max_instances=int(
                os.environ.get("ARGUS_WORKSPACE_CLEANUP_MAX_INSTANCES", 20)
            ),
            workspace_cleanup_max_archive_bytes=int(
                os.environ.get(
                    "ARGUS_WORKSPACE_CLEANUP_MAX_ARCHIVE_BYTES", 2 * 1024 * 1024 * 1024
                )
            ),
            workspace_cleanup_max_attempts=int(
                os.environ.get("ARGUS_WORKSPACE_CLEANUP_MAX_ATTEMPTS", 3)
            ),
            workspace_cleanup_low_disk_bytes=int(
                os.environ.get(
                    "ARGUS_WORKSPACE_CLEANUP_LOW_DISK_BYTES", 10 * 1024 * 1024 * 1024
                )
            ),
            workspace_cleanup_git_timeout_s=float(
                os.environ.get("ARGUS_WORKSPACE_CLEANUP_GIT_TIMEOUT_S", 300)
            ),
            disk_guard_enabled=_env_bool("ARGUS_DISK_GUARD_ENABLED", "true"),
            disk_guard_warn_bytes=int(
                os.environ.get("ARGUS_DISK_GUARD_WARN_BYTES", 5 * 1024 * 1024 * 1024)
            ),
            disk_guard_warn_clear_bytes=int(
                os.environ.get(
                    "ARGUS_DISK_GUARD_WARN_CLEAR_BYTES", 6 * 1024 * 1024 * 1024
                )
            ),
            disk_guard_refuse_bytes=int(
                os.environ.get("ARGUS_DISK_GUARD_REFUSE_BYTES", 2 * 1024 * 1024 * 1024)
            ),
            disk_guard_resume_bytes=int(
                os.environ.get(
                    "ARGUS_DISK_GUARD_RESUME_BYTES", int(2.5 * 1024 * 1024 * 1024)
                )
            ),
            disk_guard_interval_s=float(
                os.environ.get("ARGUS_DISK_GUARD_INTERVAL_S", 5)
            ),
        )
        settings._validate_disk_guard()
        return settings

    def _validate_disk_guard(self) -> None:
        """Refuse to start when hysteresis thresholds are mis-ordered."""
        refuse = self.disk_guard_refuse_bytes
        resume = self.disk_guard_resume_bytes
        warn = self.disk_guard_warn_bytes
        clear = self.disk_guard_warn_clear_bytes
        interval = self.disk_guard_interval_s
        if refuse < 0:
            raise ValueError(
                f"ARGUS_DISK_GUARD_REFUSE_BYTES must be >= 0 (got {refuse})"
            )
        if not resume > refuse:
            raise ValueError(
                "ARGUS_DISK_GUARD_RESUME_BYTES must be > ARGUS_DISK_GUARD_REFUSE_BYTES "
                f"(got resume={resume}, refuse={refuse})"
            )
        if not warn > resume:
            raise ValueError(
                "ARGUS_DISK_GUARD_WARN_BYTES must be > ARGUS_DISK_GUARD_RESUME_BYTES "
                f"(got warn={warn}, resume={resume})"
            )
        if not clear > warn:
            raise ValueError(
                "ARGUS_DISK_GUARD_WARN_CLEAR_BYTES must be > ARGUS_DISK_GUARD_WARN_BYTES "
                f"(got clear={clear}, warn={warn})"
            )
        if not interval > 0:
            raise ValueError(
                f"ARGUS_DISK_GUARD_INTERVAL_S must be > 0 (got {interval})"
            )


def _difficulty_executors() -> dict[str, tuple[str, ...]]:
    """ARGUS_DIFFICULTY_EXECUTORS, validated at startup: a bad value must not route."""
    raw = os.environ.get("ARGUS_DIFFICULTY_EXECUTORS")
    chains = json.loads(raw) if raw else {}
    valid = {"claude", "codex", "opencode", "agent"}
    if not isinstance(chains, dict) or any(
        k not in set(Difficulty)
        or not isinstance(v, list)
        or not v
        or not set(v) <= valid
        for k, v in chains.items()
    ):
        raise ValueError(
            "ARGUS_DIFFICULTY_EXECUTORS: a JSON object of difficulty -> non-empty "
            f"list of executors ({', '.join(sorted(valid))})"
        )
    return {k: tuple(v) for k, v in chains.items()}


def _csv(name: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.environ.get(name, "").split(",") if x.strip())


def _env_bool(name: str, default: str) -> bool:
    """Parse a boolean env var; reject anything outside the known vocabulary."""
    raw = os.environ.get(name, default).strip().lower()
    if raw in ("true", "1", "yes", "on"):
        return True
    if raw in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"{name} must be true/false (or 1/0, yes/no, on/off); got {raw!r}")
