"""Access scopes (worker lifecycle 3.6)."""

from enum import StrEnum


class Scope(StrEnum):
    WORKER = "worker"
    ORCHESTRATOR = "orchestrator"
    ADMIN = "admin"
