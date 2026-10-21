#!/usr/bin/env python3
"""Write the API's OpenAPI schema to packages/api-client/openapi.json.

`--check` exits non-zero if the committed file is stale (used by CI).
"""

import json
import os
import sys
from pathlib import Path

# Docs routes are off unless ARGUS_API_DOCS=1; enable before importing the app
# so FastAPI still builds the schema for export (security-hardening spec).
os.environ["ARGUS_API_DOCS"] = "1"

from argus_api.main import app  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "packages/api-client/openapi.json"


def main() -> int:
    rendered = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != rendered:
            print("openapi.json is stale: run `make openapi`")
            return 1
        return 0
    OUT.write_text(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
