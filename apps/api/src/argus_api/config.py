import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    admin_token: str | None = None
    artifact_max_bytes: int = 100 * 1024 * 1024
    # Policy gate (3.10): tasks one agent may create per hour before further
    # tasks wait for human approval. 0 disables the gate.
    max_tasks_per_agent_hour: int = 100
    dashboard_dir: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "argus.db"

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
            dashboard_dir=(
                Path(d) if (d := os.environ.get("ARGUS_DASHBOARD_DIR")) else None
            ),
        )
