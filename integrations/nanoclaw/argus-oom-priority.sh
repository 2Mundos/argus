# Sourced by harness child shells (BASH_ENV, CLAUDE_ENV_FILE); see
# docs/specs/agent-resource-hygiene.spec.md 3.1. Raise-only: writes 500 to
# oom_score_adj when the current value is lower, so a runaway command is the
# kernel's first OOM victim and the harness survives to report it. Never lowers
# the score, never fails the command, prints nothing to stdout, safe to source twice.
{
  _argus_f=${ARGUS_OOM_SCORE_FILE:-/proc/self/oom_score_adj}
  _argus_cur=
  read -r _argus_cur <"$_argus_f" || :
  case $_argus_cur in
    '' | *[!0-9-]*) ;;
    *)
      if [ "$_argus_cur" -lt 500 ]; then
        printf '500\n' >"$_argus_f" || :
      fi
      ;;
  esac
  unset _argus_f _argus_cur
} 2>/dev/null || :

# CPU-count shims (spec 3.2) ride the same entry point: BASH_ENV names one file.
_argus_here=${BASH_SOURCE[0]%/*}
if [ -r "$_argus_here/argus-cpu-count.sh" ]; then
  . "$_argus_here/argus-cpu-count.sh" || :
fi
unset _argus_here
:
