import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    admin_token: str | None = None
    artifact_max_bytes: int = 100 * 1024 * 1024
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

    @property
    def db_path(self) -> Path:
        return self.data_dir / "argus.db"

    @property
    def secrets_path(self) -> Path:
        return self.secrets_file or self.data_dir / "secrets.sops.yaml"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("ARGUS_DATA_DIR", "data")),
            admin_token=os.environ.get("ARGUS_ADMIN_TOKEN") or None,
            artifact_max_bytes=int(
                os.environ.get("ARGUS_ARTIFACT_MAX_BYTES", 100 * 1024 * 1024)
            ),
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
            dashboard_dir=(
                Path(d) if (d := os.environ.get("ARGUS_DASHBOARD_DIR")) else None
            ),
        )
