# Sourced from argus-oom-priority.sh; see docs/specs/agent-resource-hygiene.spec.md 3.2.
# When the container has a CPU quota (cgroup cpu.max, else CONTAINER_CPU_LIMIT),
# make CPU-count defaults report max(1, ceil(quota / period)) instead of the host:
# OMP_NUM_THREADS and PYTHON_CPU_COUNT only if unset, a Node --require preload on
# NODE_OPTIONS, a sitecustomize on PYTHONPATH. Without a quota it sets nothing.
# Never fails the command, prints nothing to stdout, safe to source twice.
# ARGUS_LIB_DIR (default /usr/local/lib/argus) holds argus-cpu-count.cjs and
# pycpu/sitecustomize.py; ARGUS_CPU_MAX_FILE overrides /sys/fs/cgroup/cpu.max.
{
  _argus_n=
  _argus_file=${ARGUS_CPU_MAX_FILE:-/sys/fs/cgroup/cpu.max}
  if [ -r "$_argus_file" ]; then
    _argus_quota=
    _argus_period=
    read -r _argus_quota _argus_period <"$_argus_file" || :
    case $_argus_quota$_argus_period in
      '' | *[!0-9]*) ;; # "max <period>" or garbage: no quota
      *)
        if [ "$_argus_period" -gt 0 ]; then
          _argus_n=$(((_argus_quota + _argus_period - 1) / _argus_period))
        fi
        ;;
    esac
  elif [ -n "${CONTAINER_CPU_LIMIT:-}" ]; then
    _argus_n=$(awk -v v="$CONTAINER_CPU_LIMIT" 'BEGIN { if (v ~ /^[0-9]+(\.[0-9]+)?$/ && v > 0) { n = int(v); if (n < v) n++; print n } }') || :
  fi

  if [ -n "$_argus_n" ]; then
    [ "$_argus_n" -ge 1 ] || _argus_n=1
    _argus_lib=${ARGUS_LIB_DIR:-/usr/local/lib/argus}
    export ARGUS_CPU_COUNT=$_argus_n
    export OMP_NUM_THREADS=${OMP_NUM_THREADS:-$_argus_n}
    export PYTHON_CPU_COUNT=${PYTHON_CPU_COUNT:-$_argus_n}

    _argus_req="--require=$_argus_lib/argus-cpu-count.cjs"
    case " ${NODE_OPTIONS:-} " in
      *" $_argus_req "*) ;;
      *)
        # Already set by an earlier run of this shim, yet the preload is gone:
        # a project replaced NODE_OPTIONS. Say so once, restore it, run on.
        [ -z "${ARGUS_CPU_SHIM:-}" ] || _argus_warn="argus: NODE_OPTIONS was replaced; restoring $_argus_req to keep os.cpus() at the CPU quota ($_argus_n)"
        NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }$_argus_req"
        export NODE_OPTIONS
        ;;
    esac
    case ":${PYTHONPATH:-}:" in
      *":$_argus_lib/pycpu:"*) ;;
      *) export PYTHONPATH="$_argus_lib/pycpu${PYTHONPATH:+:$PYTHONPATH}" ;;
    esac
    export ARGUS_CPU_SHIM=1
  fi
  unset _argus_n _argus_file _argus_quota _argus_period _argus_lib _argus_req
} 2>/dev/null || :
[ -z "${_argus_warn:-}" ] || echo "$_argus_warn" >&2 || :
unset _argus_warn
:
