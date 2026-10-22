"""Synthetic-only checks for the daily-only immutable release adopter."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADOPTER = ROOT / "scripts" / "adopt_daily_briefing_release.sh"
INSTALLER = ROOT / "scripts" / "install_immutable_release.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _command(root: str, script: str) -> str:
    return (
        "{ path=/bin/bash ; argv[]=/bin/bash "
        f"{root}/ops/quant-monitor/scripts/{script}"
        " ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
    )


def _env_command(release_root: Path, sha: str) -> str:
    root = release_root.parent / "current"
    monitor = root / "ops/quant-monitor"
    return (
        "{ path=/usr/bin/env ; argv[]=/usr/bin/env "
        f"QUANT_MONITOR_ROOT={monitor} AIAUDIT_BRIDGE_ROOT={root} QUANT_MONITOR_VENV={monitor}/.venv PATH={monitor}/.venv/bin:${{PATH}} "
        f"/bin/bash {monitor}/scripts/daily_briefing_pipeline.sh"
        " ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
    )


@pytest.fixture
def adoption_case(tmp_path: Path) -> dict[str, Path | str]:
    if os.geteuid() == 0:
        pytest.skip("synthetic test mode is intentionally unavailable as root")

    repo = tmp_path / "repo"
    monitor = repo / "ops/quant-monitor"
    scripts = monitor / "scripts"
    scripts.mkdir(parents=True)
    (monitor / "AGENTS.md").write_text("synthetic\n", encoding="utf-8")
    (monitor / ".gitignore").write_text("data/\n.venv/\n", encoding="utf-8")
    (scripts / "common_env.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts / "health_check.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts / "daily_briefing_pipeline.sh").write_text(
        "#!/usr/bin/env bash\n", encoding="utf-8"
    )
    (scripts / "daily_briefing_builder.py").write_text("# offline qualification target\n", encoding="utf-8")
    (repo / "scripts").mkdir()
    (repo / "scripts/consume_daily_briefing.py").write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops/quant-monitor'))\n"
        "from quant_monitor_domain import runtime_digest, briefing_dispatch\n",
        encoding="utf-8",
    )
    domain = monitor / "quant_monitor_domain"
    domain.mkdir()
    (domain / "__init__.py").write_text("\n", encoding="utf-8")
    for module in ("runtime_digest", "briefing_dispatch"):
        (domain / f"{module}.py").write_text("\n", encoding="utf-8")
    shutil.copy2(ADOPTER, scripts / ADOPTER.name)
    shutil.copy2(INSTALLER, scripts / INSTALLER.name)
    (scripts / INSTALLER.name).chmod(0o755)
    (scripts / ADOPTER.name).chmod(0o755)

    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "synthetic daily adopter")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", sha)

    old_sha = "a" * 40
    release_root = tmp_path / "releases"
    old_monitor = release_root / old_sha / "ops/quant-monitor"
    old_monitor.mkdir(parents=True)
    (release_root.parent / "current").symlink_to(release_root / old_sha)
    runtime_data = tmp_path / "runtime/data"
    runtime_venv = tmp_path / "runtime/.venv"
    runtime_data.mkdir(parents=True)
    venv.EnvBuilder(with_pip=False).create(runtime_venv)
    site_packages = runtime_venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    qpk = site_packages / "quant_platform_kit/strategy_lifecycle"
    qpk.mkdir(parents=True)
    (site_packages / "quant_platform_kit/__init__.py").write_text("\n", encoding="utf-8")
    (qpk.parent / "__init__.py").write_text("\n", encoding="utf-8")
    (qpk / "__init__.py").write_text("\n", encoding="utf-8")
    (qpk / "drift_detector.py").write_text(
        "def run_drift_detection(domain): return []\n", encoding="utf-8"
    )
    (qpk / "health_dashboard.py").write_text(
        "def build_dashboard(*, output_dir, output_format, domains=()): return None\n",
        encoding="utf-8",
    )
    # Deliberately models the installed older QPK API: qualification checks
    # required imports/basic signatures, not the newer crypto-window kwargs.
    (qpk / "performance_monitor.py").write_text(
        "def run_monitor(domain, *, strategy_profile=None, live_stream_id=None, source_revision=None): return []\n",
        encoding="utf-8",
    )
    (qpk / "strategy_health_score.py").write_text(
        "def compute_health_score(snapshot, *, drift=None): return None\n", encoding="utf-8"
    )
    watch = qpk / "watch"
    watch.mkdir()
    (watch / "__init__.py").write_text("\n", encoding="utf-8")
    (watch / "strategy_watch.py").write_text(
        "def build_strategy_monitoring_finding(**kwargs): return kwargs\n", encoding="utf-8"
    )
    (watch / "runner.py").write_text(
        "def dispatch_strategy_watch_findings(*args, **kwargs): return {}\n", encoding="utf-8"
    )
    (old_monitor / "data").symlink_to(runtime_data)
    (old_monitor / ".venv").symlink_to(runtime_venv)

    unit_root = tmp_path / "systemd"
    unit_root.mkdir()
    base_unit = unit_root / "codex-daily-briefing.service"
    base_unit.write_text("[Service]\nExecStartPre=/bin/true\n", encoding="utf-8")
    (unit_root / "codex-daily-briefing.timer").write_text("[Timer]\n", encoding="utf-8")
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$TEST_SYSTEMCTL_LOG"
if [[ "${1:-}" == daemon-reload ]]; then touch "$TEST_RELOADED"; exit 0; fi
[[ "${1:-}" == --no-pager && "${2:-}" == show ]] || exit 9
unit="$3"
case "$unit" in
  codex-daily-briefing.service)
    root="$TEST_RELEASE_ROOT/$TEST_OLD_SHA"
    if [[ -e "$TEST_RELOADED" ]]; then
      if [[ "${TEST_FAIL_READBACK:-false}" == true ]]; then root="$TEST_RELEASE_ROOT/$TEST_OLD_SHA"; fi
      current="$(dirname "$TEST_RELEASE_ROOT")/current"
      wd="$current/ops/quant-monitor"
      if [[ ! -e "$TEST_RELOADED" || "${TEST_FAIL_READBACK:-false}" == true ]]; then wd="$root/ops/quant-monitor"; fi
      if [[ "${TEST_FAIL_READBACK:-false}" == true ]]; then execstart="$TEST_OLD_EXECSTART"; else
        execstart="{ path=/usr/bin/env ; argv[]=/usr/bin/env QUANT_MONITOR_ROOT=$wd AIAUDIT_BRIDGE_ROOT=$current QUANT_MONITOR_VENV=$wd/.venv PATH=$wd/.venv/bin:\\${PATH} /bin/bash $wd/scripts/daily_briefing_pipeline.sh ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
      fi
      dropin=""
    else
      wd="${TEST_OLD_WD:-$root/ops/quant-monitor}"
      execstart="$TEST_OLD_EXECSTART"
      dropin=""
    fi
    dropin_dir="$TEST_UNIT_ROOT/codex-daily-briefing.service.d"
    for candidate in "$dropin_dir"/*.conf; do
      [[ -e "$candidate" ]] || continue
      dropin="${dropin}${dropin:+ }$candidate"
    done
    execstartpre="$TEST_EXECSTARTPRE"
    if [[ -e "$TEST_RELOADED" ]]; then execstartpre="$TEST_EXECSTARTPRE_POST"; fi
    printf 'WorkingDirectory=%s\\nExecStart=%s\\nExecStartPre=%s\\nFragmentPath=%s\\nDropInPaths=%s\\nActiveState=inactive\\nSubState=dead\\nUnitFileState=static\\n' \\
      "$wd" "$execstart" "$execstartpre" "$TEST_UNIT_ROOT/codex-daily-briefing.service" "$dropin"
    ;;
  codex-daily-briefing.timer)
    printf 'FragmentPath=%s\\nDropInPaths=\\nUnit=codex-daily-briefing.service\\nActiveState=active\\nSubState=waiting\\nUnitFileState=enabled\\n' "$TEST_UNIT_ROOT/codex-daily-briefing.timer"
    ;;
  codex-quant.service)
    if [[ -n "${TEST_HEALTH_MUTATION_FILE:-}" ]]; then
      count_file="$TEST_RELOADED.health-reads"
      count=0
      if [[ -f "$count_file" ]]; then count="$(cat "$count_file")"; fi
      count=$((count + 1)); printf '%s' "$count" > "$count_file"
      if [[ "$count" == "${TEST_HEALTH_MUTATION_READ:-2}" ]]; then
        printf 'QUANT_MONITOR_ROOT=%s\\n' "$TEST_HEALTH_MUTATED_ROOT" > "$TEST_HEALTH_MUTATION_FILE"
      fi
    fi
    if [[ "$4" == *Environment* ]]; then
      printf 'Environment=%s\\nEnvironmentFiles=%s\\nPassEnvironment=%s\\nUnsetEnvironment=\\nExecSearchPath=%s\\nRootDirectory=\\nRootImage=\\n' \\
        "${TEST_HEALTH_ENVIRONMENT:-}" "${TEST_HEALTH_ENV_FILES:-}" "${TEST_HEALTH_PASS_ENV:-}" "${TEST_HEALTH_EXEC_SEARCH_PATH:-}"
    fi
    health_wd="${TEST_HEALTH_CURRENT_ROOT:-/health}"
    health_state=failed
    health_substate=failed
    health_exec='{ path=/bin/bash ; argv[]=/bin/bash /health/scripts/health_check.sh ; ignore_errors=no ; start_time=[old] ; stop_time=[old] ; pid=101 ; code=(exited) ; status=0/0 }'
    if [[ -n "${TEST_HEALTH_EXECSTART:-}" ]]; then health_exec="$TEST_HEALTH_EXECSTART"; fi
    health_pre='{ path=/bin/true ; argv[]=/bin/true ; ignore_errors=no ; start_time=[old] ; stop_time=[old] ; pid=102 ; code=(exited) ; status=0/0 }'
    if [[ -e "$TEST_RELOADED" ]]; then
      health_state=active
      health_substate=running
      health_exec='{ path=/bin/bash ; argv[]=/bin/bash /health/scripts/health_check.sh ; ignore_errors=no ; start_time=[new] ; stop_time=[n/a] ; pid=201 ; code=(null) ; status=0/0 }'
      if [[ -n "${TEST_HEALTH_EXECSTART:-}" ]]; then health_exec="$TEST_HEALTH_EXECSTART"; fi
      health_pre='{ path=/bin/true ; argv[]=/bin/true ; ignore_errors=no ; start_time=[new] ; stop_time=[n/a] ; pid=202 ; code=(null) ; status=0/0 }'
      if [[ "${TEST_HEALTH_CONFIG_CHANGE:-false}" == true ]]; then health_wd=/changed-health-root; fi
    fi
    printf 'WorkingDirectory=%s\\nExecStart=%s\\nExecStartPre=%s\\nFragmentPath=/etc/systemd/system/codex-quant.service\\nDropInPaths=\\nActiveState=%s\\nSubState=%s\\nUnitFileState=static\\n' \\
      "$health_wd" "$health_exec" "$health_pre" "$health_state" "$health_substate"
    ;;
  --property=Environment)
    printf 'Environment=%s\\n' "${TEST_MANAGER_ENVIRONMENT:-}"
    ;;
  *) exit 9 ;;
esac
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (bin_dir / "gcloud").chmod(0o755)

    old_root = str(release_root / old_sha)
    old_exec = _command(old_root, "daily_briefing_pipeline.sh")
    return {
        "repo": repo,
        "sha": sha,
        "old_sha": old_sha,
        "release_root": release_root,
        "runtime_data": runtime_data,
        "runtime_venv": runtime_venv,
        "site_packages": site_packages,
        "path_prefix": bin_dir,
        "unit_root": unit_root,
        "systemctl": systemctl,
        "log": tmp_path / "systemctl.log",
        "reloaded": tmp_path / "reloaded",
        "old_exec": old_exec,
        "execstartpre": "{ path=/bin/bash ; argv[]=/bin/bash /old/scripts/load_telegram_env.sh ; ignore_errors=no ; start_time=[old] ; stop_time=[n/a] ; pid=301 ; code=(null) ; status=0/0 }",
        "execstartpre_post": "{ path=/bin/bash ; argv[]=/bin/bash /old/scripts/load_telegram_env.sh ; ignore_errors=no ; start_time=[new] ; stop_time=[n/a] ; pid=302 ; code=(null) ; status=0/0 }",
    }


def _run(case: dict[str, Path | str], *, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("QUANT_MONITOR_V2_RUNTIME_READY", None)
    env.update(
        {
            "AAB_DAILY_ADOPTION_TEST_MODE": "true",
            "AAB_DAILY_ADOPTION_TEST_UNIT_ROOT": str(case["unit_root"]),
            "AAB_DAILY_ADOPTION_TEST_SYSTEMCTL": str(case["systemctl"]),
            "TEST_SYSTEMCTL_LOG": str(case["log"]),
            "TEST_RELOADED": str(case["reloaded"]),
            "TEST_RELEASE_ROOT": str(case["release_root"]),
            "TEST_OLD_SHA": str(case["old_sha"]),
            "TEST_NEW_SHA": str(case["sha"]),
            "TEST_OLD_EXECSTART": str(case["old_exec"]),
            "TEST_OLD_WD": str(case.get("old_wd", "")),
            "TEST_EXECSTARTPRE": str(case["execstartpre"]),
            "TEST_EXECSTARTPRE_POST": str(case["execstartpre_post"]),
            "TEST_UNIT_ROOT": str(case["unit_root"]),
            "PATH": str(case["path_prefix"]) + os.pathsep + os.environ.get("PATH", ""),
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            "bash",
            str(ADOPTER),
            "--sha",
            str(case["sha"]),
            "--expected-current-sha",
            str(case["old_sha"]),
            "--repo",
            str(case["repo"]),
            "--release-root",
            str(case["release_root"]),
            "--runtime-data",
            str(case["runtime_data"]),
            "--runtime-venv",
            str(case["runtime_venv"]),
        ],
        capture_output=True,
        text=True,
        env=env,
    )


def test_adopts_only_daily_unit_and_preserves_data_venv_and_prestart(adoption_case: dict[str, Path | str]) -> None:
    case = adoption_case
    dropin_dir = Path(str(case["unit_root"])) / "codex-daily-briefing.service.d"
    dropin_dir.mkdir()
    preserved = dropin_dir / "10-site-policy.conf"
    preserved.write_text("[Service]\nEnvironment=EXAMPLE=unchanged\n", encoding="utf-8")
    result = _run(case)
    assert result.returncode == 0, result.stderr
    assert "status=prepared" in result.stdout

    new_monitor = Path(str(case["release_root"])) / str(case["sha"]) / "ops/quant-monitor"
    assert new_monitor.is_dir()
    assert not list(new_monitor.parent.parent.rglob("__pycache__"))
    assert new_monitor.joinpath("data").resolve() == Path(str(case["runtime_data"]))
    assert new_monitor.joinpath(".venv").resolve() == Path(str(case["runtime_venv"]))
    dropin = Path(str(case["unit_root"])) / "codex-daily-briefing.service.d/zzzzzzzzzz-aab-daily-release.conf"
    content = dropin.read_text(encoding="utf-8")
    current = Path(str(case["release_root"])).parent / "current"
    assert current.resolve() == new_monitor.parents[1]
    assert f"WorkingDirectory={current}/ops/quant-monitor" in content
    assert f"QUANT_MONITOR_ROOT={current}/ops/quant-monitor" in content
    assert f"AIAUDIT_BRIDGE_ROOT={current}" in content
    assert str(case["sha"]) not in content
    assert "ExecStartPre=" not in content
    assert preserved.read_text(encoding="utf-8") == "[Service]\nEnvironment=EXAMPLE=unchanged\n"

    invocations = Path(str(case["log"])).read_text(encoding="utf-8").splitlines()
    assert invocations.count("daemon-reload") == 1
    assert all(" start " not in f" {line} " and " restart " not in f" {line} " for line in invocations)
    assert not any("codex-quant.timer" in line or "codex-quant.service" in line and "show" not in line for line in invocations)
    assert any("show codex-daily-briefing.timer" in line for line in invocations)


def test_foreign_file_colliding_with_owned_dropin_is_rejected(adoption_case: dict[str, Path | str]) -> None:
    case = adoption_case
    dropin_dir = Path(str(case["unit_root"])) / "codex-daily-briefing.service.d"
    dropin_dir.mkdir()
    dropin = dropin_dir / "zzzzzzzzzz-aab-daily-release.conf"
    dropin.write_text("[Service]\nEnvironment=FOREIGN=true\n", encoding="utf-8")
    result = _run(case)
    assert result.returncode == 2
    assert "daily_dropin_unowned_or_unexpected" in result.stderr
    assert not (Path(str(case["release_root"])) / str(case["sha"])).exists()


def test_daily_runtime_qualification_failure_does_not_switch_unit(adoption_case: dict[str, Path | str]) -> None:
    case = adoption_case
    (Path(str(case["site_packages"])) / "quant_platform_kit/strategy_lifecycle/health_dashboard.py").unlink()
    result = _run(case)
    assert result.returncode == 2
    assert "daily_runtime_qualification_failed" in result.stderr
    assert Path(str(case["reloaded"])).exists() is False
    invocations = Path(str(case["log"])).read_text(encoding="utf-8").splitlines()
    assert not any("daemon-reload" in line for line in invocations)
    assert not (Path(str(case["unit_root"])) / "codex-daily-briefing.service.d/zzzzzzzzzz-aab-daily-release.conf").exists()


def test_exact_prior_helper_dropin_is_snapshotted_and_replaced(adoption_case: dict[str, Path | str]) -> None:
    case = adoption_case
    dropin_dir = Path(str(case["unit_root"])) / "codex-daily-briefing.service.d"
    dropin_dir.mkdir()
    dropin = dropin_dir / "zzzzzzzzzz-aab-daily-release.conf"
    old_monitor = Path(str(case["release_root"])) / str(case["old_sha"]) / "ops/quant-monitor"
    previous_sha = "b" * 40
    prior_contents = (
        "# managed-by: AIAuditBridge daily-release-adoption.v1\n"
        f"# release-sha: {case['old_sha']}\n"
        f"# prior-release-sha: {previous_sha}\n"
        "[Service]\n"
        f"WorkingDirectory={old_monitor}\n"
        "ExecStart=\n"
        f"ExecStart=/usr/bin/env QUANT_MONITOR_ROOT={old_monitor} AIAUDIT_BRIDGE_ROOT={Path(str(case['release_root'])) / str(case['old_sha'])} /bin/bash {old_monitor}/scripts/daily_briefing_pipeline.sh\n"
    )
    dropin.write_text(prior_contents, encoding="utf-8")
    result = _run(case)
    assert result.returncode == 0, result.stderr
    snapshot = dropin_dir / f"zzzzzzzzzz-aab-daily-release.rollback.{case['old_sha']}.{case['sha']}"
    assert snapshot.read_text(encoding="utf-8") == prior_contents
    assert "daily-release-adoption.v2" in dropin.read_text(encoding="utf-8")


def test_two_consecutive_adoptions_accept_exact_helper_execstart(adoption_case: dict[str, Path | str]) -> None:
    case = adoption_case
    first = _run(case)
    assert first.returncode == 0, first.stderr
    first_sha = str(case["sha"])
    first_contents = (Path(str(case["unit_root"])) / "codex-daily-briefing.service.d/zzzzzzzzzz-aab-daily-release.conf").read_text()

    repo = Path(str(case["repo"]))
    (repo / "ops/quant-monitor/README.md").write_text("second synthetic release\n", encoding="utf-8")
    _git(repo, "add", "ops/quant-monitor/README.md")
    _git(repo, "commit", "-m", "synthetic second release")
    second_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", second_sha)
    Path(str(case["reloaded"])).unlink()
    case["old_sha"] = first_sha
    case["sha"] = second_sha
    case["old_exec"] = _env_command(Path(str(case["release_root"])), first_sha)
    case["old_wd"] = str(Path(str(case["release_root"])).parent / "current/ops/quant-monitor")

    second = _run(case)
    assert second.returncode == 0, second.stderr
    dropin_dir = Path(str(case["unit_root"])) / "codex-daily-briefing.service.d"
    dropin = dropin_dir / "zzzzzzzzzz-aab-daily-release.conf"
    snapshot = dropin_dir / f"zzzzzzzzzz-aab-daily-release.rollback.{first_sha}.{second_sha}"
    assert snapshot.read_text(encoding="utf-8") == first_contents
    assert dropin.read_text(encoding="utf-8") == first_contents
    assert (Path(str(case["release_root"])).parent / "current").resolve().name == second_sha


def test_failed_readback_keeps_recovery_snapshot_and_does_not_retry(adoption_case: dict[str, Path | str]) -> None:
    case = adoption_case
    result = _run(case, extra_env={"TEST_FAIL_READBACK": "true"})
    assert result.returncode == 3
    assert "status=unknown" in result.stderr
    assert "reason=daily_configuration_changed" in result.stderr
    assert "manual_readback_no_retry" in result.stderr
    dropin_dir = Path(str(case["unit_root"])) / "codex-daily-briefing.service.d"
    snapshots = [path for path in dropin_dir.glob("*.rollback.*") if not path.name.endswith(".current-target")]
    assert len(snapshots) == 1
    assert snapshots[0].read_text(encoding="utf-8") == "previous_dropin=absent\n"
    invocations = Path(str(case["log"])).read_text(encoding="utf-8").splitlines()
    assert invocations.count("daemon-reload") == 1


@pytest.mark.parametrize("module", ["strategy_watch.py", "runner.py"])
def test_missing_lazy_watcher_import_does_not_switch_release(adoption_case, module):
    case = adoption_case
    missing = Path(str(case["site_packages"])) / "quant_platform_kit/strategy_lifecycle/watch" / module
    missing.unlink()
    result = _run(case)
    assert result.returncode == 2
    assert "daily_runtime_qualification_failed" in result.stderr
    assert (Path(str(case["release_root"])).parent / "current").resolve().name == case["old_sha"]
    assert not Path(str(case["reloaded"])).exists()


def test_daily_can_use_separate_qualified_venv_without_changing_old_link(adoption_case):
    case = adoption_case
    original = Path(str(case["runtime_venv"]))
    replacement = original.with_name("daily-venv")
    shutil.copytree(original, replacement, symlinks=True)
    case["runtime_venv"] = replacement
    result = _run(case)
    assert result.returncode == 0, result.stderr
    old_monitor = Path(str(case["release_root"])) / str(case["old_sha"]) / "ops/quant-monitor"
    assert (old_monitor / ".venv").resolve() == original
    assert (Path(str(case["release_root"])) / str(case["sha"]) / "ops/quant-monitor/.venv").resolve() == replacement


def test_current_link_shared_with_health_is_not_switched(adoption_case):
    case = adoption_case
    current = Path(str(case["release_root"])).parent / "current"
    result = _run(case, extra_env={"TEST_HEALTH_CURRENT_ROOT": str(current / "ops/quant-monitor")})
    assert result.returncode == 2
    assert "current_link_shared_with_health_service" in result.stderr
    assert current.resolve().name == case["old_sha"]
    assert not Path(str(case["reloaded"])).exists()


@pytest.mark.parametrize("key", ["QUANT_MONITOR_ROOT", "PATH", "QUANT_MONITOR_VENV", "PYTHONPATH", "AIAUDIT_BRIDGE_ROOT"])
@pytest.mark.parametrize("source", ["unit", "file", "manager"])
def test_health_environment_only_current_dependency_is_rejected(adoption_case, key, source):
    case = adoption_case
    current = Path(str(case["release_root"])).parent / "current"
    value = f"{key}={current}/ops/quant-monitor"
    if source == "file":
        env_file = Path(str(case["unit_root"])) / "health.env"
        env_file.write_text(f"# synthetic private settings\nSYNTHETIC_SECRET=do-not-print-me\n{value}\n")
        extra = {"TEST_HEALTH_ENV_FILES": f"{env_file} (ignore_errors=no)"}
    else:
        extra = {"TEST_HEALTH_ENVIRONMENT" if source == "unit" else "TEST_MANAGER_ENVIRONMENT": value}
    result = _run(case, extra_env=extra)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "current_link_shared_with_health_service" in result.stderr
    assert "do-not-print-me" not in result.stdout + result.stderr
    assert str(current) not in result.stdout + result.stderr
    assert current.resolve().name == case["old_sha"]
    assert not Path(str(case["reloaded"])).exists()
    assert not (Path(str(case["release_root"])) / str(case["sha"])).exists()


@pytest.mark.parametrize("source", ["unit", "file", "runtime_file", "exec_search"])
def test_health_quoted_normalized_or_aliased_current_path_is_rejected(adoption_case, source):
    case = adoption_case
    current = Path(str(case["release_root"])).parent / "current"
    alias = current.with_name("health-alias")
    alias.symlink_to(current, target_is_directory=True)
    env_file = Path(str(case["unit_root"])) / "health.env"
    env_file.write_text(f"QUANT_MONITOR_VENV='{alias}/ops/quant-monitor/.venv'\n")
    extra = {
        "unit": {"TEST_HEALTH_ENVIRONMENT": f'"QUANT_MONITOR_ROOT={current.parent}/other/../current/ops/quant-monitor"'},
        "file": {"TEST_HEALTH_ENV_FILES": f"{env_file} (ignore_errors=no)"},
        "runtime_file": {"TEST_HEALTH_ENVIRONMENT": f"QUANT_SENTINEL_ENV_FILE={env_file}"},
        "exec_search": {"TEST_HEALTH_EXEC_SEARCH_PATH": f"{alias}/bin:/usr/bin"},
    }[source]
    result = _run(case, extra_env=extra)
    assert result.returncode == 2
    assert "current_link_shared_with_health_service" in result.stderr
    assert current.resolve().name == case["old_sha"]
    assert not Path(str(case["reloaded"])).exists()


@pytest.mark.parametrize("config", ["missing_file", "optional_missing", "glob_file", "dynamic_path", "escaped_path", "pass_environment"])
def test_unresolved_health_environment_fails_before_switch(adoption_case, config):
    case = adoption_case
    missing = Path(str(case["unit_root"])) / "missing.env"
    extra = {
        "missing_file": {"TEST_HEALTH_ENV_FILES": f"{missing} (ignore_errors=no)"},
        "optional_missing": {"TEST_HEALTH_ENV_FILES": f"{missing} (ignore_errors=yes)"},
        "glob_file": {"TEST_HEALTH_ENV_FILES": f"{missing}* (ignore_errors=no)"},
        "dynamic_path": {"TEST_HEALTH_ENVIRONMENT": "QUANT_MONITOR_ROOT=$OTHER_ROOT"},
        "escaped_path": {"TEST_HEALTH_ENVIRONMENT": r"QUANT_MONITOR_ROOT=/opt/quant-monitor/cur\x72ent"},
        "pass_environment": {"TEST_HEALTH_PASS_ENV": "QUANT_MONITOR_ROOT PATH"},
    }[config]
    result = _run(case, extra_env=extra)
    assert result.returncode == 2
    assert "health_service_identity_unavailable" in result.stderr
    current = Path(str(case["release_root"])).parent / "current"
    assert current.resolve().name == case["old_sha"]
    assert not Path(str(case["reloaded"])).exists()


@pytest.mark.parametrize("change_at", [None, "2", "3"])
def test_health_environment_file_is_checked_again_before_and_after_switch(adoption_case, change_at):
    case = adoption_case
    env_file = Path(str(case["unit_root"])) / "health.env"
    env_file.write_text("QUANT_MONITOR_ROOT='/fixed-health-root'\nSYNTHETIC_SECRET=do-not-print-me\n")
    extra = {"TEST_HEALTH_ENV_FILES": f"{env_file} (ignore_errors=no)"}
    if change_at:
        extra.update({
            "TEST_HEALTH_MUTATION_FILE": str(env_file),
            "TEST_HEALTH_MUTATED_ROOT": "/different-fixed-health-root",
            "TEST_HEALTH_MUTATION_READ": change_at,
        })
    result = _run(case, extra_env=extra)
    assert result.returncode == {None: 0, "2": 2, "3": 3}[change_at], result.stderr
    assert "do-not-print-me" not in result.stdout + result.stderr
    if change_at == "2":
        assert "health_service_changed_during_adoption" in result.stderr
        current = Path(str(case["release_root"])).parent / "current"
        assert current.resolve().name == case["old_sha"]
        assert not Path(str(case["reloaded"])).exists()
    elif change_at == "3":
        assert "health_configuration_changed" in result.stderr
        assert "manual_readback_no_retry" in result.stderr


@pytest.mark.parametrize("dependency", ["ROOT", "VENV", "default_venv", "python3_link", "inline_root", "inferred_root"])
def test_health_effective_shell_root_and_default_interpreter_cannot_follow_current(adoption_case, dependency):
    case = adoption_case
    current = Path(str(case["release_root"])).parent / "current"
    alias = current.with_name("health-alias")
    alias.symlink_to(current, target_is_directory=True)
    health_root = Path(str(case["unit_root"])) / "fixed-health"
    health_root.mkdir()
    extra = {"TEST_HEALTH_ENVIRONMENT": f"QUANT_MONITOR_ROOT={health_root}"}
    if dependency in {"ROOT", "VENV"}:
        env_file = health_root / "runtime.env"
        env_file.write_text(f"{dependency}={alias}/ops/quant-monitor\n")
        extra["TEST_HEALTH_ENVIRONMENT"] += f" QUANT_SENTINEL_ENV_FILE={env_file}"
    elif dependency == "python3_link":
        bin_dir = health_root / ".venv/bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "python3").symlink_to(alias / "python3")
    else:
        (health_root / ".venv").symlink_to(alias / "venv", target_is_directory=True)
        if dependency == "inline_root":
            extra["TEST_HEALTH_EXECSTART"] = (
                "{ path=/usr/bin/env ; argv[]=/usr/bin/env "
                f"QUANT_MONITOR_ROOT={alias}/ops/quant-monitor /bin/bash /health/scripts/health_check.sh"
                " ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
            )
        elif dependency == "inferred_root":
            extra["TEST_HEALTH_ENVIRONMENT"] = ""
            extra["TEST_HEALTH_EXECSTART"] = _command(str(health_root.parents[1]), "health_check.sh").replace(
                str(health_root.parents[1]) + "/ops/quant-monitor", str(health_root)
            )
    result = _run(case, extra_env=extra)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "current_link_shared_with_health_service" in result.stderr
    assert current.resolve().name == case["old_sha"]
    assert not Path(str(case["reloaded"])).exists()


def test_runtime_shell_environment_is_never_executed(adoption_case):
    case = adoption_case
    env_file = Path(str(case["unit_root"])) / "runtime.env"
    marker = env_file.with_name("must-not-exist")
    env_file.write_text(f'UNRELATED=$(touch "{marker}")\n')
    result = _run(case, extra_env={"TEST_HEALTH_ENVIRONMENT": f"QUANT_SENTINEL_ENV_FILE={env_file}"})
    assert result.returncode == 2
    assert "health_service_identity_unavailable" in result.stderr
    assert not marker.exists()


def test_health_runtime_state_and_exec_times_do_not_block_adoption(adoption_case: dict[str, Path | str]) -> None:
    result = _run(adoption_case)
    assert result.returncode == 0, result.stderr
    assert "status=prepared" in result.stdout


def test_health_configuration_change_is_reported_after_switch(adoption_case: dict[str, Path | str]) -> None:
    result = _run(adoption_case, extra_env={"TEST_HEALTH_CONFIG_CHANGE": "true"})
    assert result.returncode == 3
    assert "status=unknown" in result.stderr
    assert "reason=health_configuration_changed" in result.stderr
    assert "manual_readback_no_retry" in result.stderr


def test_changed_prestart_command_is_not_treated_as_a_timestamp(adoption_case: dict[str, Path | str]) -> None:
    result = _run(adoption_case, extra_env={
        "TEST_EXECSTARTPRE_POST": str(adoption_case["execstartpre_post"]).replace("load_telegram_env.sh", "different_prestart.sh"),
    })
    assert result.returncode == 3
    assert "reason=daily_execstartpre_changed" in result.stderr


@pytest.mark.parametrize("separator", [" ", " ; "])
def test_additional_prestart_command_is_not_silently_discarded(adoption_case: dict[str, Path | str], separator: str) -> None:
    result = _run(adoption_case, extra_env={
        "TEST_EXECSTARTPRE_POST": str(adoption_case["execstartpre_post"]) + separator + "{ path=/bin/true ; argv[]=/bin/true ; ignore_errors=no ; start_time=[new] ; stop_time=[n/a] ; pid=303 ; code=(null) ; status=0/0 }",
    })
    assert result.returncode == 3
    assert "reason=daily_execstartpre_unavailable" in result.stderr
