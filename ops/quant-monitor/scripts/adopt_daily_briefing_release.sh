#!/usr/bin/env bash
# Install an exact immutable release and switch the daily service through current.
set -euo pipefail

usage() {
  cat <<'EOF' >&2
usage: adopt_daily_briefing_release.sh --sha <40-hex-main-sha> \
  --expected-current-sha <40-hex-release-sha> --repo <git-root> \
  [--release-root <dir>] [--runtime-data <dir>] [--runtime-venv <dir>]

Installs the immutable code release, performs offline imports with the selected
runtime venv, and changes current and the daily service drop-in. It never installs
packages, starts a service or timer, reads the ledger, or sends a message.
EOF
}

die() {
  echo "[daily-adoption] status=stopped reason=$1" >&2
  exit 2
}

SHA=""
EXPECTED_CURRENT_SHA=""
REPO_ROOT=""
RELEASE_ROOT="/opt/quant-monitor/releases"
RUNTIME_DATA=""
RUNTIME_VENV=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha) SHA="${2:-}"; shift 2 ;;
    --expected-current-sha) EXPECTED_CURRENT_SHA="${2:-}"; shift 2 ;;
    --repo) REPO_ROOT="${2:-}"; shift 2 ;;
    --release-root) RELEASE_ROOT="${2:-}"; shift 2 ;;
    --runtime-data) RUNTIME_DATA="${2:-}"; shift 2 ;;
    --runtime-venv) RUNTIME_VENV="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unsupported_arguments" ;;
  esac
done

[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || die "invalid_release_sha"
[[ "$EXPECTED_CURRENT_SHA" =~ ^[0-9a-f]{40}$ ]] || die "invalid_expected_current_sha"
[[ -n "$REPO_ROOT" && ( -d "$REPO_ROOT/.git" || -f "$REPO_ROOT/.git" ) ]] \
  || die "repository_unavailable"
TEST_MODE="${AAB_DAILY_ADOPTION_TEST_MODE:-false}"
UNIT_ROOT="/etc/systemd/system"
SYSTEMCTL="/usr/bin/systemctl"
if [[ "$TEST_MODE" == "true" ]]; then
  [[ "${EUID}" -ne 0 ]] || die "test_mode_forbidden_as_root"
  [[ -n "${AAB_DAILY_ADOPTION_TEST_UNIT_ROOT:-}" && -n "${AAB_DAILY_ADOPTION_TEST_SYSTEMCTL:-}" ]] \
    || die "test_mode_paths_required"
  UNIT_ROOT="$AAB_DAILY_ADOPTION_TEST_UNIT_ROOT"
  SYSTEMCTL="$AAB_DAILY_ADOPTION_TEST_SYSTEMCTL"
elif [[ "${EUID}" -ne 0 ]]; then
  die "root_required"
fi

if [[ -z "$RUNTIME_DATA" ]]; then
  RUNTIME_DATA="$REPO_ROOT/ops/quant-monitor/data"
fi
if [[ -z "$RUNTIME_VENV" ]]; then
  RUNTIME_VENV="$REPO_ROOT/ops/quant-monitor/.venv"
fi

for path in "$REPO_ROOT" "$RELEASE_ROOT" "$RUNTIME_DATA" "$RUNTIME_VENV" "$UNIT_ROOT"; do
  [[ "$path" == /* && "$path" != *".."* ]] || die "invalid_path_configuration"
done
[[ -d "$UNIT_ROOT" && ! -L "$UNIT_ROOT" ]] || die "systemd_unit_root_unavailable"
[[ -x "$SYSTEMCTL" ]] || die "systemctl_unavailable"
[[ -d "$RUNTIME_VENV" && ! -L "$RUNTIME_VENV" && -x "$RUNTIME_VENV/bin/python" ]] \
  || die "runtime_python_unavailable"
CURRENT_ROOT="$(dirname "$RELEASE_ROOT")/current"
CURRENT_MONITOR="$CURRENT_ROOT/ops/quant-monitor"
[[ ! -e "$CURRENT_ROOT" || -L "$CURRENT_ROOT" ]] || die "current_link_unexpected"
CURRENT_BEFORE="$(readlink "$CURRENT_ROOT" 2>/dev/null || true)"

UNIT="codex-daily-briefing.service"
TIMER="codex-daily-briefing.timer"
DROPIN_DIR="$UNIT_ROOT/codex-daily-briefing.service.d"
DROPIN="$DROPIN_DIR/zzzzzzzzzz-aab-daily-release.conf"
BASE_UNIT="$UNIT_ROOT/$UNIT"
TIMER_UNIT="$UNIT_ROOT/$TIMER"
[[ -f "$BASE_UNIT" && ! -L "$BASE_UNIT" ]] || die "daily_unit_unavailable"
[[ -f "$TIMER_UNIT" && ! -L "$TIMER_UNIT" ]] || die "daily_timer_unavailable"

if [[ -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=all -- \
  src scripts ops/quant-monitor/quant_monitor_domain ops/quant-monitor/scripts \
  ops/quant-monitor/systemd ops/quant-monitor/qpk-runtime.sha \
  ops/quant-monitor/requirements-linux-py312.lock)" ]]; then
  die "runtime_source_dirty"
fi
MAIN_SHA="$(git -C "$REPO_ROOT" rev-parse --verify refs/remotes/origin/main 2>/dev/null || true)"
[[ "$MAIN_SHA" == "$SHA" ]] || die "release_sha_not_current_origin_main"
git -C "$REPO_ROOT" cat-file -e "${SHA}^{commit}" 2>/dev/null || die "release_commit_unavailable"

load_properties() {
  local unit_name="$1" output="$2"; shift 2
  local raw line key value properties parsed_count=0 expected_count=$#
  properties="$(IFS=,; printf '%s' "$*")"
  if [[ -n "$unit_name" ]]; then
    raw="$("$SYSTEMCTL" --no-pager show "$unit_name" "--property=$properties" 2>/dev/null)" || return 1
  else
    raw="$("$SYSTEMCTL" --no-pager show "--property=$properties" 2>/dev/null)" || return 1
  fi
  (( ${#raw} <= 32768 )) || return 1
  while IFS= read -r line; do
    key="${line%%=*}"
    value="${line#*=}"
    [[ "$line" == *=* ]] || continue
    case "$output:$key" in
      service:WorkingDirectory|service:ExecStart|service:ExecStartPre|service:FragmentPath|service:DropInPaths|service:ActiveState|service:SubState|service:UnitFileState|\
      timer:FragmentPath|timer:DropInPaths|timer:Unit|timer:ActiveState|timer:SubState|timer:UnitFileState|\
      health:WorkingDirectory|health:ExecStart|health:ExecStartPre|health:FragmentPath|health:DropInPaths|health:ActiveState|health:SubState|health:UnitFileState|\
      health:Environment|health:EnvironmentFiles|health:PassEnvironment|health:UnsetEnvironment|health:ExecSearchPath|health:RootDirectory|health:RootImage|manager:Environment)
        printf -v "${output}_${key}" '%s' "$value"
        parsed_count=$((parsed_count + 1)) ;;
      *) return 1 ;;
    esac
  done <<< "$raw"
  [[ "$parsed_count" -eq "$expected_count" ]]
}

read_service() {
  for key in WorkingDirectory ExecStart ExecStartPre FragmentPath DropInPaths ActiveState SubState UnitFileState; do unset "service_$key"; done
  load_properties "$UNIT" service WorkingDirectory ExecStart ExecStartPre FragmentPath DropInPaths ActiveState SubState UnitFileState \
    || return 1
}

read_timer() {
  for key in FragmentPath DropInPaths Unit ActiveState SubState UnitFileState; do unset "timer_$key"; done
  load_properties "$TIMER" timer FragmentPath DropInPaths Unit ActiveState SubState UnitFileState \
    || return 1
  [[ "$timer_Unit" == "$UNIT" ]]
}

read_health() {
  for key in WorkingDirectory ExecStart ExecStartPre FragmentPath DropInPaths ActiveState SubState UnitFileState Environment EnvironmentFiles PassEnvironment UnsetEnvironment ExecSearchPath RootDirectory RootImage; do unset "health_$key"; done
  load_properties codex-quant.service health WorkingDirectory ExecStart ExecStartPre FragmentPath DropInPaths ActiveState SubState UnitFileState Environment EnvironmentFiles PassEnvironment UnsetEnvironment ExecSearchPath RootDirectory RootImage \
    || return 1
  load_properties "" manager Environment \
    || return 1
}

health_environment_identity() {
  # Values travel over stdin, never command arguments or diagnostic output.
  # EnvironmentFile is systemd data, not shell code: never source/eval it.
  "$RUNTIME_VENV/bin/python" -I -B - "$CURRENT_ROOT" "$health_WorkingDirectory" 3<<< "$(printf '%s\n' \
    "$manager_Environment" "$health_Environment" "$health_EnvironmentFiles" \
    "$health_PassEnvironment" "$health_UnsetEnvironment" "$health_ExecSearchPath" \
    "$health_RootDirectory" "$health_RootImage" "$1" END)" <<'PY' 2>/dev/null
import hashlib
import os
import re
import shlex
import sys
from pathlib import Path

class SharedCurrent(Exception):
    pass

current, working_directory = sys.argv[1:]
digest = hashlib.sha256()
path_keys = {"ROOT", "VENV", "AAB_ROOT", "QPK_ROOT", "PATH", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "QUANT_MONITOR_ROOT",
             "QUANT_MONITOR_VENV", "AIAUDIT_BRIDGE_ROOT", "QUANT_SENTINEL_ENV_FILE",
             "BASH_ENV", "ENV"}

def inspect_path(value):
    # Check lexical paths before resolving: resolving current alone hides its
    # dependence on the mutable link. Also follow aliases one symlink at a time.
    pending = Path(os.path.abspath(value))
    for _ in range(64):
        if pending == Path(current) or Path(current) in pending.parents:
            raise SharedCurrent
        for part in (*reversed(pending.parents), pending):
            if part.is_symlink():
                target = Path(os.readlink(part))
                if not target.is_absolute():
                    target = part.parent / target
                pending = Path(os.path.abspath(target / pending.relative_to(part)))
                break
        else:
            digest.update(str(pending).encode() + b"\0")
            return
    raise ValueError("unresolved path")

def inspect_value(key, value):
    if current in value:
        raise SharedCurrent
    if key in path_keys or key.endswith(("_ROOT", "_PATH", "_VENV", "_FILE")):
        if key in {"BASH_ENV", "ENV"} and value:
            raise ValueError("unresolved shell startup environment")
        if any(char in value for char in ('$', '%', '\\', '`', '\n')):
            raise ValueError("dynamic path")
        for entry in value.split(":"):
            inspect_path(entry if entry.startswith("/") else os.path.join(working_directory, entry))

def assignments(raw):
    # systemctl quotes strv items; unsupported C escapes fail closed.
    if "\\" in raw:
        raise ValueError("escaped environment")
    values = {}
    for item in shlex.split(raw):
        key, separator, value = item.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
            raise ValueError("invalid assignment")
        inspect_value(key, value)
        values[key] = value
    return values

def read_environment_file(filename, *, shell_source=False):
    if not filename.startswith("/") or any(char in filename for char in "$%*?[]\\"):
        raise ValueError("unresolved environment file")
    inspect_path(filename)
    # Missing optional files are also uncertain: they can appear on next start.
    with open(filename, "rb") as handle:
        data = handle.read(65537)
    if len(data) > 65536:
        raise ValueError("environment file too large")
    digest.update(filename.encode() + b"\0" + data + b"\0")
    values = {}
    for line in data.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        # Only literal single-line assignments are proven safe. Multiline,
        # escapes and shell statements require operator inspection.
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key) or "\\" in value:
            raise ValueError("unsupported environment syntax")
        value = value.strip()
        if shell_source and any(char in value for char in "$`;|&()<>"):
            raise ValueError("nonliteral shell environment")
        if shell_source or value.startswith(("'", '"')):
            parsed = shlex.split(value)
            if len(parsed) > 1:
                raise ValueError("unsupported quoted value")
            value = parsed[0] if parsed else ""
        inspect_value(key, value)
        values[key] = value
    return values

try:
    with os.fdopen(3) as handle:
        raw = handle.read(32769)
    fields = raw.splitlines()
    if len(raw) > 32768 or len(fields) != 10 or fields[-1] != "END":
        raise ValueError("environment readback unavailable")
    default, environment, files, inherited, unset, search, root, image, execution = fields[:-1]
    if inherited or root or image:
        raise ValueError("unresolved inherited or rooted environment")
    digest.update(raw.encode())
    values = assignments(default)
    values.update(assignments(environment))
    # UnsetEnvironment can only remove values. Conservatively inspect all
    # sources, including overridden values, rather than waive a dependency.
    inspect_value("PATH", search)
    tokens = shlex.split(files)
    if len(tokens) % 2:
        raise ValueError("environment file readback unavailable")
    for filename, flags in zip(tokens[::2], tokens[1::2]):
        if flags not in {"(ignore_errors=yes)", "(ignore_errors=no)"}:
            raise ValueError("environment file flags unavailable")
        values.update(read_environment_file(filename))
    # ExecStart's literal /usr/bin/env assignments override unit environment.
    # Accept the known health entry point only; shell -c and env options cannot
    # establish a static source root without executing code.
    argv_text = execution.split(" ; argv[]=", 1)[1].split(" ; ignore_errors=", 1)[0]
    if "\\" in argv_text:
        raise ValueError("escaped health command")
    argv = shlex.split(argv_text)
    if argv[0] == "/usr/bin/env":
        argv.pop(0)
        while argv and re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", argv[0]):
            item = argv.pop(0)
            key, value = item.split("=", 1)
            inspect_value(key, value)
            values[key] = value
    if len(argv) == 2 and argv[0] in {"/bin/bash", "/usr/bin/bash"}:
        inspect_path(argv.pop(0))
    if len(argv) != 1 or not argv[0].startswith("/") or not argv[0].endswith("/scripts/health_check.sh"):
        raise ValueError("unresolved health entry point")
    inspect_path(argv[0])
    monitor_root = values.get("QUANT_MONITOR_ROOT") or str(Path(argv[0]).parent.parent)
    inspect_value("ROOT", monitor_root)
    inspect_path(os.path.join(monitor_root, "scripts/common_env.sh"))
    selected_venv = values.get("QUANT_MONITOR_VENV") or os.path.join(monitor_root, ".venv")
    inspect_value("VENV", selected_venv)
    for name in ("python", "python3"):
        inspect_path(os.path.join(selected_venv, "bin", name))
        for directory in (values.get("PATH") or search or "/usr/local/bin:/usr/bin:/bin").split(":"):
            inspect_path(os.path.join(directory or working_directory, name))
    # health_check also sources its Telegram environment before running Python.
    runtime_env = values.get("QUANT_SENTINEL_ENV_FILE") or "/run/quant-monitor/telegram.env"
    inspect_path(runtime_env)
    fallback_env = os.path.join(monitor_root, ".env")
    inspect_path(fallback_env)
    if Path(runtime_env).exists():
        shell_values = read_environment_file(runtime_env, shell_source=True)
    elif Path(fallback_env).exists():
        shell_values = read_environment_file(fallback_env, shell_source=True)
    else:
        shell_values = {}
    # These are live shell variables after source_telegram_env, unlike the
    # similarly named systemd environment entries overwritten by common_env.
    for key in ("ROOT", "QUANT_MONITOR_ROOT"):
        shell_root = shell_values.get(key)
        if shell_root:
            inspect_path(os.path.join(shell_root, "scripts"))
            inspect_path(os.path.join(shell_root, ".venv"))
            inspect_path(os.path.join(shell_root, ".venv/bin/python3"))
    for key in ("VENV", "QUANT_MONITOR_VENV"):
        if shell_values.get(key):
            for name in ("python", "python3"):
                inspect_path(os.path.join(shell_values[key], "bin", name))
    for directory in shell_values.get("PATH", "").split(":"):
        if directory:
            inspect_path(os.path.join(directory, "python3"))
    print(digest.hexdigest())
except SharedCurrent:
    print("shared_current")
except Exception:
    sys.exit(2)
PY
}

exec_is_release() {
  local raw="$1" sha="$2" command="${3:-/bin/bash}" script="${4:-daily_briefing_pipeline.sh}"
  local expected_root="${RELEASE_ROOT}/${sha}"
  local expected_argv="${command} ${expected_root}/ops/quant-monitor/scripts/${script}"
  if [[ "$script" == "daily_briefing_pipeline.sh" && "$command" == "/usr/bin/env" ]]; then
    expected_argv="/usr/bin/env QUANT_MONITOR_ROOT=${expected_root}/ops/quant-monitor AIAUDIT_BRIDGE_ROOT=${expected_root} /bin/bash ${expected_root}/ops/quant-monitor/scripts/${script}"
  fi
  [[ "$raw" == "{ path=${command} ; argv[]=${expected_argv} ; "* ]]
}

exec_is_prior_release() {
  local raw="$1" sha="$2"
  exec_is_release "$raw" "$sha" /bin/bash daily_briefing_pipeline.sh \
    || exec_is_release "$raw" "$sha" /usr/bin/env daily_briefing_pipeline.sh \
    || { [[ "$CURRENT_BEFORE" == "$RELEASE_ROOT/$sha" ]] && exec_is_current "$raw"; }
}

current_dropin_contents() {
  printf '%s\n' \
    '# managed-by: AIAuditBridge daily-release-adoption.v2' \
    "# release-link: $CURRENT_ROOT" \
    '[Service]' \
    "WorkingDirectory=$CURRENT_MONITOR" \
    'ExecStart=' \
    "ExecStart=/usr/bin/env QUANT_MONITOR_ROOT=$CURRENT_MONITOR AIAUDIT_BRIDGE_ROOT=$CURRENT_ROOT QUANT_MONITOR_VENV=$CURRENT_MONITOR/.venv PATH=$CURRENT_MONITOR/.venv/bin:\${PATH} /bin/bash $CURRENT_MONITOR/scripts/daily_briefing_pipeline.sh"
}

exec_is_current() {
  local expected_argv
  expected_argv="/usr/bin/env QUANT_MONITOR_ROOT=$CURRENT_MONITOR AIAUDIT_BRIDGE_ROOT=$CURRENT_ROOT QUANT_MONITOR_VENV=$CURRENT_MONITOR/.venv PATH=$CURRENT_MONITOR/.venv/bin:\${PATH} /bin/bash $CURRENT_MONITOR/scripts/daily_briefing_pipeline.sh"
  [[ "$1" == "{ path=/usr/bin/env ; argv[]=${expected_argv} ; "* ]]
}

owned_dropin_matches_expected_release() {
  local sha="$1" previous_sha="$2" monitor_root
  monitor_root="$RELEASE_ROOT/$sha/ops/quant-monitor"
  local expected
  expected="$(printf '%s\n' \
    '# managed-by: AIAuditBridge daily-release-adoption.v1' \
    "# release-sha: $sha" \
    "# prior-release-sha: $previous_sha" \
    '[Service]' \
    "WorkingDirectory=$monitor_root" \
    'ExecStart=' \
    "ExecStart=/usr/bin/env QUANT_MONITOR_ROOT=$monitor_root AIAUDIT_BRIDGE_ROOT=$RELEASE_ROOT/$sha /bin/bash $monitor_root/scripts/daily_briefing_pipeline.sh")"
  [[ "$(cat -- "$DROPIN" 2>/dev/null)" == "$expected" ]]
}

normalize_dropin_paths() {
  local paths="$1" path
  for path in $paths; do
    [[ "$path" == /* && "$path" != *".."* && "$path" != *$'\n'* ]] || return 1
    printf '%s\n' "$path"
  done | LC_ALL=C sort
}

service_is_idle() {
  [[ "${service_ActiveState}" != "active" \
    && "${service_ActiveState}" != "activating" \
    && "${service_ActiveState}" != "deactivating" \
    && "${service_ActiveState}" != "reloading" ]]
}

exec_property_identity() {
  local raw="$1" config
  if [[ "$raw" == \{* ]]; then
    # Current units each have a single command. Never silently discard a
    # second command or the configured ignore-errors flag.
    [[ "${raw#\{}" != *"{ path="* ]] || return 1
    config="${raw%% ; start_time=*}"
    [[ "$config" != "$raw" && "$raw" == *" }" && "$config" == *" ; ignore_errors="* ]] || return 1
    printf '%s }\n' "$config"
  else
    [[ -n "$raw" ]] || return 1
    printf '%s\n' "$raw"
  fi
}

health_configuration_identity() {
  local exec_start exec_start_pre environment_identity
  exec_start="$(exec_property_identity "$health_ExecStart")" || return 1
  exec_start_pre="$(exec_property_identity "$health_ExecStartPre")" || return 1
  environment_identity="$(health_environment_identity "$exec_start")" || return 1
  printf '%s\n' \
    "$health_FragmentPath" \
    "$health_DropInPaths" \
    "$health_WorkingDirectory" \
    "$exec_start" \
    "$exec_start_pre" \
    "$health_UnitFileState" \
    "$environment_identity"
}

postflight_unknown() {
  local reason="$1"
  echo "[daily-adoption] status=unknown sha=$SHA reason=$reason recovery_snapshot=preserved action=manual_readback_no_retry" >&2
  exit 3
}

read_service || die "daily_service_readback_unavailable"
read_timer || die "daily_timer_readback_unavailable"
read_health || die "health_service_readback_unavailable"
service_is_idle || die "daily_service_active"
[[ "${service_FragmentPath}" == "$BASE_UNIT" ]] || die "daily_unit_source_unexpected"
[[ "${service_WorkingDirectory}" == "$RELEASE_ROOT/$EXPECTED_CURRENT_SHA/ops/quant-monitor" \
  || ( "${service_WorkingDirectory}" == "$CURRENT_MONITOR" && "$CURRENT_BEFORE" == "$RELEASE_ROOT/$EXPECTED_CURRENT_SHA" ) ]] \
  || die "daily_working_directory_unexpected"
exec_is_prior_release "${service_ExecStart}" "$EXPECTED_CURRENT_SHA" \
  || die "daily_execstart_unexpected"
SERVICE_PRE_BEFORE="$(exec_property_identity "${service_ExecStartPre}")" \
  || die "daily_execstartpre_unavailable"
[[ -n "$SERVICE_PRE_BEFORE" ]] || die "daily_execstartpre_unavailable"
DAILY_UNIT_FILE_STATE_BEFORE="${service_UnitFileState}"
DAILY_WORKING_DIRECTORY_BEFORE="${service_WorkingDirectory}"
TIMER_BEFORE="${timer_FragmentPath}|${timer_DropInPaths}|${timer_Unit}|${timer_UnitFileState}|${timer_ActiveState}|${timer_SubState}"
HEALTH_BEFORE="$(health_configuration_identity)" || die "health_service_identity_unavailable"
[[ "$HEALTH_BEFORE" != *"$CURRENT_ROOT"* && "$HEALTH_BEFORE" != *shared_current* ]] \
  || die "current_link_shared_with_health_service"
DROPINS_BEFORE="$service_DropInPaths"

BASELINE_DROPINS=""
OWN_DROPIN_PRESENT=false
for existing_dropin in $service_DropInPaths; do
  [[ -f "$existing_dropin" && ! -L "$existing_dropin" ]] || die "daily_dropin_state_unexpected"
  if [[ "$existing_dropin" == "$DROPIN" ]]; then
    OWN_DROPIN_PRESENT=true
    continue
  fi
  BASELINE_DROPINS="$BASELINE_DROPINS $existing_dropin"
done
if [[ "$OWN_DROPIN_PRESENT" == true ]]; then
  [[ "$(wc -c < "$DROPIN")" -le 4096 ]] || die "daily_dropin_unowned_or_unexpected"
  if [[ "$(cat -- "$DROPIN")" != "$(current_dropin_contents)" ]]; then
    PRIOR_DROPIN_SHA="$(sed -n 's/^# prior-release-sha: //p' "$DROPIN" 2>/dev/null)"
    [[ "$PRIOR_DROPIN_SHA" =~ ^[0-9a-f]{40}$ ]] \
      && owned_dropin_matches_expected_release "$EXPECTED_CURRENT_SHA" "$PRIOR_DROPIN_SHA" \
      || die "daily_dropin_unowned_or_unexpected"
  fi
else
  [[ ! -e "$DROPIN" && ! -L "$DROPIN" ]] || die "daily_dropin_state_unexpected"
fi

OLD_RELEASE="$RELEASE_ROOT/$EXPECTED_CURRENT_SHA/ops/quant-monitor"
[[ -d "$OLD_RELEASE" && ! -L "$OLD_RELEASE" ]] || die "current_release_unavailable"
[[ -L "$OLD_RELEASE/data" && -L "$OLD_RELEASE/.venv" ]] || die "current_release_links_unavailable"
PERSISTENT_DATA="$(readlink -f "$OLD_RELEASE/data" 2>/dev/null || true)"
RUNTIME_PYTHON="$(readlink -f "$OLD_RELEASE/.venv" 2>/dev/null || true)"
[[ -n "$PERSISTENT_DATA" && -n "$RUNTIME_PYTHON" \
  && -d "$PERSISTENT_DATA" && -d "$RUNTIME_PYTHON" ]] || die "current_release_links_unavailable"
[[ "$(readlink -f "$RUNTIME_DATA" 2>/dev/null || true)" == "$PERSISTENT_DATA" ]] \
  || die "runtime_data_or_venv_mismatch"
[[ -d "$RUNTIME_VENV" && ! -L "$RUNTIME_VENV" && -x "$RUNTIME_VENV/bin/python" ]] \
  || die "runtime_python_unavailable"
RUNTIME_PYTHON="$RUNTIME_VENV"

INSTALLER="$REPO_ROOT/ops/quant-monitor/scripts/install_immutable_release.sh"
[[ -x "$INSTALLER" ]] || die "immutable_installer_unavailable"
if ! INSTALL_RESULT="$("$INSTALLER" --sha "$SHA" --repo "$REPO_ROOT" \
    --release-root "$RELEASE_ROOT" --runtime-data "$PERSISTENT_DATA" \
    --runtime-venv "$RUNTIME_PYTHON" 2>/dev/null)"; then
  die "immutable_release_install_failed"
fi
unset INSTALL_RESULT
NEW_ROOT="$RELEASE_ROOT/$SHA"
NEW_MONITOR="$NEW_ROOT/ops/quant-monitor"
[[ -d "$NEW_MONITOR" && -L "$NEW_MONITOR/data" && -L "$NEW_MONITOR/.venv" ]] \
  || die "immutable_release_install_unverified"
[[ "$(readlink -f "$NEW_MONITOR/data" 2>/dev/null || true)" == "$PERSISTENT_DATA" \
  && "$(readlink -f "$NEW_MONITOR/.venv" 2>/dev/null || true)" == "$RUNTIME_PYTHON" ]] \
  || die "immutable_release_link_mismatch"

file_uid() {
  local path="$1"
  stat -c '%u' -- "$path" 2>/dev/null || stat -f '%u' "$path" 2>/dev/null
}

EXPECTED_SOURCE_UID=0
if [[ "$TEST_MODE" == "true" ]]; then
  EXPECTED_SOURCE_UID="$(id -u)"
fi
for source_file in \
  "$NEW_ROOT" \
  "$NEW_MONITOR/scripts/daily_briefing_pipeline.sh" \
  "$NEW_MONITOR/scripts/daily_briefing_builder.py" \
  "$NEW_ROOT/scripts/consume_daily_briefing.py" \
  "$NEW_ROOT/ops/quant-monitor/quant_monitor_domain/runtime_digest.py" \
  "$NEW_ROOT/ops/quant-monitor/quant_monitor_domain/briefing_dispatch.py"; do
  [[ -e "$source_file" && ! -L "$source_file" ]] || die "candidate_source_unavailable"
  [[ "$(file_uid "$source_file")" == "$EXPECTED_SOURCE_UID" ]] || die "candidate_source_owner_unexpected"
done
UNOWNED_SOURCE="$(find "$NEW_ROOT" -xdev ! -uid "$EXPECTED_SOURCE_UID" -print -quit 2>/dev/null)"
[[ -z "$UNOWNED_SOURCE" ]] || die "candidate_source_owner_unexpected"
command -v gcloud >/dev/null 2>&1 || die "gcloud_unavailable"
[[ -x "$RUNTIME_PYTHON/bin/python" ]] || die "runtime_python_unavailable"

# This qualifies only the code paths used by the existing daily runtime and
# domain builder. It imports candidate source and the already-installed QPK;
# it does not read lifecycle data, access cloud services, send, or touch ledger.
if ! "$RUNTIME_PYTHON/bin/python" -I -B - "$NEW_ROOT" <<'PY' >/dev/null 2>&1
import importlib
import inspect
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))

importlib.import_module("scripts.consume_daily_briefing")
importlib.import_module("quant_monitor_domain.runtime_digest")
importlib.import_module("quant_monitor_domain.briefing_dispatch")

from quant_platform_kit.strategy_lifecycle.drift_detector import run_drift_detection
from quant_platform_kit.strategy_lifecycle.health_dashboard import build_dashboard
from quant_platform_kit.strategy_lifecycle.performance_monitor import run_monitor
from quant_platform_kit.strategy_lifecycle.strategy_health_score import compute_health_score
from quant_platform_kit.strategy_lifecycle.watch.strategy_watch import build_strategy_monitoring_finding
from quant_platform_kit.strategy_lifecycle.watch.runner import dispatch_strategy_watch_findings

assert callable(build_strategy_monitoring_finding)
assert callable(dispatch_strategy_watch_findings)

def accepts(signature, name):
    return name in signature.parameters or any(
        item.kind == inspect.Parameter.VAR_KEYWORD
        for item in signature.parameters.values()
    )

drift_signature = inspect.signature(run_drift_detection)
dashboard_signature = inspect.signature(build_dashboard)
monitor_signature = inspect.signature(run_monitor)
score_signature = inspect.signature(compute_health_score)
assert "domain" in drift_signature.parameters
assert all(accepts(dashboard_signature, key) for key in ("output_dir", "output_format", "domains"))
assert "domain" in monitor_signature.parameters
assert all(accepts(monitor_signature, key) for key in ("strategy_profile", "live_stream_id", "source_revision"))
assert accepts(score_signature, "drift")
PY
then
  die "daily_runtime_qualification_failed"
fi

# Re-read source and timer state before changing the service drop-in.
read_service || die "daily_service_readback_unavailable"
read_timer || die "daily_timer_readback_unavailable"
read_health || die "health_service_readback_unavailable"
service_is_idle || die "daily_service_active"
[[ "${service_WorkingDirectory}" == "$DAILY_WORKING_DIRECTORY_BEFORE" \
  && "${service_DropInPaths}" == "$DROPINS_BEFORE" \
  && "${service_UnitFileState}" == "$DAILY_UNIT_FILE_STATE_BEFORE" ]] \
  || die "daily_service_changed_during_adoption"
SERVICE_PRE_CURRENT="$(exec_property_identity "${service_ExecStartPre}")" \
  || die "daily_service_changed_during_adoption"
[[ "$SERVICE_PRE_CURRENT" == "$SERVICE_PRE_BEFORE" ]] || die "daily_service_changed_during_adoption"
exec_is_prior_release "${service_ExecStart}" "$EXPECTED_CURRENT_SHA" \
  || die "daily_service_changed_during_adoption"
[[ "${timer_FragmentPath}|${timer_DropInPaths}|${timer_Unit}|${timer_UnitFileState}|${timer_ActiveState}|${timer_SubState}" == "$TIMER_BEFORE" \
  ]] || die "timer_changed_during_adoption"
HEALTH_CURRENT="$(health_configuration_identity)" || die "health_service_changed_during_adoption"
[[ "$HEALTH_CURRENT" == "$HEALTH_BEFORE" ]] || die "health_service_changed_during_adoption"

mkdir -p "$DROPIN_DIR" 2>/dev/null || die "daily_dropin_directory_unavailable"
[[ ! -L "$DROPIN_DIR" && -d "$DROPIN_DIR" ]] || die "daily_dropin_directory_unavailable"
SNAPSHOT="$DROPIN_DIR/zzzzzzzzzz-aab-daily-release.rollback.${EXPECTED_CURRENT_SHA}.${SHA}"
[[ ! -e "$SNAPSHOT" && ! -L "$SNAPSHOT" ]] || die "recovery_snapshot_already_exists"
if [[ -f "$DROPIN" && ! -L "$DROPIN" ]]; then
  cp -p -- "$DROPIN" "$SNAPSHOT" 2>/dev/null || die "recovery_snapshot_failed"
else
  (umask 077; printf 'previous_dropin=absent\n' > "$SNAPSHOT") 2>/dev/null \
    || die "recovery_snapshot_failed"
fi
chmod 0600 "$SNAPSHOT" 2>/dev/null || die "recovery_snapshot_failed"
(umask 077; printf '%s\n' "$CURRENT_BEFORE" > "$SNAPSHOT.current-target") \
  || die "recovery_snapshot_failed"

STAGED="$(mktemp "$DROPIN_DIR/.daily-release.XXXXXX" 2>/dev/null)" \
  || die "daily_dropin_stage_failed"
cleanup_stage() {
  if [[ -n "${STAGED:-}" && -f "$STAGED" ]]; then rm -f -- "$STAGED" 2>/dev/null; fi
  return 0
}
trap cleanup_stage EXIT
if ! current_dropin_contents 2>/dev/null > "$STAGED"; then die "daily_dropin_stage_failed"; fi
chmod 0644 "$STAGED" 2>/dev/null || die "daily_dropin_stage_failed"
[[ "$(readlink "$CURRENT_ROOT" 2>/dev/null || true)" == "$CURRENT_BEFORE" ]] \
  || die "current_link_changed_during_adoption"
if ! "$RUNTIME_PYTHON/bin/python" -I -B - "$NEW_ROOT" "$CURRENT_ROOT" <<'PY'
import os
import sys
from pathlib import Path

target, current = sys.argv[1:]
staged = Path(current + f".daily-adoption.{os.getpid()}")
try:
    staged.symlink_to(target)
    os.replace(staged, current)
finally:
    staged.unlink(missing_ok=True)
PY
then postflight_unknown "current_link_switch_failed"; fi
mv -f -- "$STAGED" "$DROPIN" 2>/dev/null || postflight_unknown "daily_dropin_write_failed"
STAGED=""

if ! "$SYSTEMCTL" daemon-reload >/dev/null 2>&1; then
  postflight_unknown "daemon_reload_failed"
fi

read_service || postflight_unknown "daily_readback_failed"
read_timer || postflight_unknown "timer_readback_failed"
read_health || postflight_unknown "health_readback_failed"
service_is_idle || postflight_unknown "daily_service_not_idle"
[[ "${service_FragmentPath:-}" == "$BASE_UNIT" \
  && "${service_WorkingDirectory:-}" == "$CURRENT_MONITOR" \
  && "$(readlink -f "$CURRENT_ROOT" 2>/dev/null || true)" == "$NEW_ROOT" \
  && "$(normalize_dropin_paths "$service_DropInPaths" 2>/dev/null || true)" == "$(normalize_dropin_paths "$BASELINE_DROPINS $DROPIN" 2>/dev/null || true)" \
  && "${service_UnitFileState:-}" == "$DAILY_UNIT_FILE_STATE_BEFORE" ]] \
  || postflight_unknown "daily_configuration_changed"
SERVICE_PRE_CURRENT="$(exec_property_identity "${service_ExecStartPre:-}")" \
  || postflight_unknown "daily_execstartpre_unavailable"
[[ "$SERVICE_PRE_CURRENT" == "$SERVICE_PRE_BEFORE" ]] \
  || postflight_unknown "daily_execstartpre_changed"

exec_is_current "${service_ExecStart}" \
  || postflight_unknown "daily_execstart_mismatch"
[[ "${timer_Unit}" == "$UNIT" \
  && "${timer_FragmentPath}" == "$TIMER_UNIT" \
  && "${timer_FragmentPath}|${timer_DropInPaths}|${timer_Unit}|${timer_UnitFileState}|${timer_ActiveState}|${timer_SubState}" == "$TIMER_BEFORE" ]] \
  || postflight_unknown "timer_configuration_changed"
HEALTH_CURRENT="$(health_configuration_identity)" || postflight_unknown "health_identity_unavailable"
[[ "$HEALTH_CURRENT" == "$HEALTH_BEFORE" ]] || postflight_unknown "health_configuration_changed"

echo "[daily-adoption] status=prepared sha=$SHA daily_service=release timer=unchanged health=untouched send=not_run recovery_snapshot=preserved"
