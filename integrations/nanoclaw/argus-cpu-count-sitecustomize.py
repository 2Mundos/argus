"""Installed as pycpu/sitecustomize.py on PYTHONPATH (argus-cpu-count.sh).

Python before 3.13 ignores PYTHON_CPU_COUNT, so os.cpu_count() would report every
host CPU inside a CPU quota. This makes it report PYTHON_CPU_COUNT instead
(docs/specs/agent-resource-hygiene.spec.md 3.2). Newer Python honours the
variable itself. Anything but a positive integer leaves os.cpu_count() alone.
"""

import os
import sys

_raw = os.environ.get("PYTHON_CPU_COUNT", "")
if sys.version_info < (3, 13) and _raw.isascii() and _raw.isdigit() and int(_raw) >= 1:
    _count = int(_raw)
    os.cpu_count = lambda: _count
