from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_SOURCE = PROJECT_ROOT / "scripts" / "volcengine_agent_plan_launcher.m"
LAUNCHER_COMMAND = (
    PROJECT_ROOT / "scripts" / "start-volcengine-agent-plan-macos.command"
)
POLICY_SCRIPT = PROJECT_ROOT / "scripts" / "check_repository_policy.py"


def test_launcher_command_is_private_input_only() -> None:
    source = LAUNCHER_COMMAND.read_text(encoding="utf-8")

    assert LAUNCHER_COMMAND.stat().st_mode & stat.S_IXUSR
    subprocess.run(["bash", "-n", str(LAUNCHER_COMMAND)], check=True)
    assert "umask 077" in source
    assert "/usr/bin/clang" in source
    assert "-fobjc-arc" in source
    assert '"$launcher_binary" "$repository_root"' in source
    assert 'exec "$launcher_binary"' not in source
    assert "trap cleanup_build EXIT HUP INT TERM" in source
    for forbidden in (
        "osascript",
        "pbcopy",
        "pbpaste",
        "read -",
        "curl",
        "/agent/retrieval/analyze",
        "SEARCH_VOLCENGINE_AGENT_PLAN_API_KEY",
    ):
        assert forbidden not in source


def test_native_launcher_has_fixed_secret_and_process_boundaries() -> None:
    source = LAUNCHER_SOURCE.read_text(encoding="utf-8")

    for required in (
        "NSSecureTextField",
        "execve(",
        "RLIMIT_CORE",
        "umask(0077)",
        "mkdtemp(",
        "chmod(created, 0700)",
        '@"--host"',
        '@"127.0.0.1"',
        '@"--port"',
        '@"--no-access-log"',
        '@"SEARCH_AGENT_PLANNER=llm"',
        '@"SEARCH_LLM_PROVIDER="',
        '@"volcengine_agent_plan"',
        '@"doubao-seed-2.1-turbo"',
        '@"SEARCH_LLM_MAX_OUTPUT_TOKENS=128"',
        '@"SEARCH_VOLCENGINE_AGENT_PLAN_API_KEY="',
        '@"launcher_dialog"',
        '@"launcher_backend"',
    ):
        assert required in source

    for forbidden in (
        "osascript",
        "NSPasteboard",
        "Keychain",
        "NSUserDefaults",
        "ProcessInfo.processInfo.environment",
        "do shell script",
        "/bin/sh",
        "bash -c",
        "SEARCH_CODE_REVISION",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "DYLD_",
        "LD_PRELOAD",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "OPENAI_API_KEY",
        "SEARCH_LLM_API_KEY",
        "/agent/retrieval/analyze",
        "RemoveEphemeralLauncherBinary",
        "unlink(executable.fileSystemRepresentation)",
    ):
        assert forbidden not in source

    arguments = source.split("NSArray<NSString *> *arguments = @[", maxsplit=1)[
        1
    ].split("];", maxsplit=1)[0]
    assert "apiKey" not in arguments
    assert "SEARCH_" not in arguments


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS AppKit launcher")
def test_native_launcher_typechecks_with_clang() -> None:
    subprocess.run(
        [
            "/usr/bin/clang",
            "-fobjc-arc",
            "-fsyntax-only",
            str(LAUNCHER_SOURCE),
        ],
        check=True,
        cwd=PROJECT_ROOT,
    )


def test_repository_policy_detects_volcengine_key(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("repository_policy", POLICY_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    sentinel = "ark" + "-" + ("A" * 24)
    leaked = tmp_path / "leaked.txt"
    leaked.write_text(sentinel, encoding="utf-8")
    original_root = module.PROJECT_ROOT
    try:
        module.PROJECT_ROOT = tmp_path
        violations = module.find_violations([leaked])
    finally:
        module.PROJECT_ROOT = original_root

    assert violations == ["Volcengine Ark key detected: leaked.txt"]
    assert sentinel not in LAUNCHER_SOURCE.read_text(encoding="utf-8")


def test_launcher_file_permissions_do_not_grant_group_write() -> None:
    mode = os.stat(LAUNCHER_COMMAND).st_mode
    assert not mode & stat.S_IWGRP
    assert not mode & stat.S_IWOTH
