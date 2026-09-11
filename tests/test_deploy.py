"""Forced-command и возврат предыдущего выпуска без настоящего Docker и без root."""

import json
import os
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
OLD = "a" * 40
NEW = "b" * 40


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "mktlink.env").touch()
    script = tmp_path / "deploy"
    script.write_text(
        (DEPLOY / "mktlink-deploy").read_text()
        .replace("[[ $EUID == 0 ]]", "[[ 0 == 0 ]]")
        .replace("ROOT=/opt/mktlink", f"ROOT={root}")
        .replace("/run/lock/pilchai-deploy.lock", str(tmp_path / "lock"))
        .replace("/usr/local/bin/mktlink-deploy", str(tmp_path / "installed"))
    )
    binary = tmp_path / "docker"
    binary.write_text('''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ["TEST_ROOT"])
with open(os.environ["TEST_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if args[:2] == ["network", "inspect"]:
    print("true")
elif args[0] == "create":
    print("temporary-container")
elif args[0] == "cp":
    shutil.copytree(os.environ["TEST_DEPLOY"], args[-1])
elif args[0] == "compose" and "config" not in args:
    image = (root / ".image").read_text()
    failure = os.environ.get("FAIL_AT")
    if failure in args and "b" * 40 in image:
        sys.exit(1)
''')
    binary.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
           "TEST_ROOT": str(root), "TEST_LOG": str(tmp_path / "calls"),
           "TEST_DEPLOY": str(DEPLOY), "SSH_ORIGINAL_COMMAND": ""}
    return root, script, env


def run(deployment, command, fail_at=""):
    _, script, env = deployment
    return subprocess.run(["bash", str(script)],
                          env={**env, "SSH_ORIGINAL_COMMAND": command, "FAIL_AT": fail_at},
                          capture_output=True, text=True, check=False)


@pytest.mark.parametrize("command", ["", "bash", "deploy latest", "deploy abc",
                                     f"deploy {NEW};id", f"local {NEW} extra", "status extra"])
def test_only_fixed_commands_and_complete_sha_are_accepted(deployment, command):
    assert run(deployment, command).returncode != 0
    assert not Path(deployment[2]["TEST_LOG"]).exists()


def test_success_preserves_previous_tag_and_configuration(deployment):
    root, _, env = deployment
    (root / ".image").write_text(f"MKTLINK_IMAGE=ghcr.io/mist3s/market_parser:{OLD}\n")
    (root / "docker-compose.yml").write_text("old topology")
    result = run(deployment, f"local {NEW}")
    assert result.returncode == 0, result.stderr
    assert NEW in (root / ".image").read_text()
    assert OLD in (root / ".image.previous").read_text()
    assert (root / "docker-compose.yml.previous").read_text() == "old topology"
    calls = [json.loads(line) for line in Path(env["TEST_LOG"]).read_text().splitlines()]
    assert not any("pull" in call or "login" in call for call in calls)
    assert not list(root.glob(".deploy.*"))


@pytest.mark.parametrize("fail_at", ["run", "up"])
def test_failed_schema_or_start_restores_tag_and_topology(deployment, fail_at):
    root, _, _ = deployment
    (root / ".image").write_text(f"MKTLINK_IMAGE=ghcr.io/mist3s/market_parser:{OLD}\n")
    (root / "docker-compose.yml").write_text("old topology")
    result = run(deployment, f"local {NEW}", fail_at)
    assert result.returncode != 0
    assert OLD in (root / ".image").read_text()
    assert (root / "docker-compose.yml").read_text() == "old topology"
    assert "новый образ не готов" in result.stderr


def test_first_failure_stops_service_and_does_not_claim_a_release(deployment):
    root, _, env = deployment
    assert run(deployment, f"local {NEW}", "up").returncode != 0
    assert not (root / ".image").exists()
    assert '"stop", "mktlink"' in Path(env["TEST_LOG"]).read_text()
