import hashlib
import secrets

# Prefixes make leaked tokens recognizable (and greppable by secret scanners).
BOOTSTRAP_PREFIX = "argus_boot_"
AGENT_PREFIX = "argus_agent_"
ADMIN_PREFIX = "argus_admin_"


def new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    # Tokens carry 256 bits of entropy, so a fast unsalted hash is sufficient.
    return hashlib.sha256(token.encode()).hexdigest()
