#!/usr/bin/env -S uv --quiet run --python >=3.11 --script
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for the safehouse features sc enables on every launch.

SAFEHOUSE_FEATURES is the single list of `--enable=` flags sc passes. A missing
or renamed feature fails as a permission denial deep inside the session (docker
reporting "operation not permitted", ssh-agent asking for a passphrase), far
from its cause, so the list is checked against the installed safehouse here.

Run directly: ./test_sc_safehouse_features.py

The end-to-end check drives the real `sc` against a stub safehouse via
$SAFEHOUSE_BIN, so it needs no particular safehouse installed. The checks that
generate real policy text are skipped when safehouse is absent.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

SC = Path(__file__).resolve().parent / "sc"


def load_sc(profile_dir: str):
    os.environ["PROFILE_DIR"] = profile_dir
    loader = SourceFileLoader("sc_under_test", str(SC))
    spec = importlib.util.spec_from_loader("sc_under_test", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)  # does not run main() (guarded by __main__)
    return mod


def test_expected_features_are_enabled(sc):
    for feature in ("ssh", "playwright-chrome", "docker"):
        assert feature in sc.SAFEHOUSE_FEATURES, feature


def test_features_are_data_driven(sc):
    """The launch flags must come from the list, not from inline literals."""
    source = SC.read_text()
    assert 'f"--enable={' in source, "flags are no longer built from SAFEHOUSE_FEATURES"
    for feature in sc.SAFEHOUSE_FEATURES:
        assert f'"--enable={feature}"' not in source, (
            f"{feature} is hardcoded as a flag literal as well as listed"
        )


def test_installed_safehouse_accepts_every_feature(sc) -> None:
    """safehouse exits 1 on an unknown --enable value, so a renamed or
    misspelled feature would abort every launch."""
    for feature in sc.SAFEHOUSE_FEATURES:
        r = subprocess.run(
            ["safehouse", f"--enable={feature}", "--stdout"],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, f"safehouse rejected --enable={feature}: {r.stderr}"


DOCKER_SOCKET = '(path-literal "/var/run/docker.sock")'


def _last_verdict(policy: str, needle: str) -> str | None:
    """The directive of the last rule block mentioning `needle`. Seatbelt applies
    the last matching rule, so this is the effective verdict for that path."""
    verdict = None
    current = None
    for line in policy.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("(allow "):
            current = "allow"
        elif stripped.startswith("(deny "):
            current = "deny"
        if needle in line:
            verdict = current
    return verdict


def _policy(*features: str) -> str:
    cmd = ["safehouse", "--stdout"]
    cmd += [f"--enable={f}" for f in features]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def test_docker_feature_opens_the_daemon_socket(sc) -> None:
    """Guards against the feature surviving in name only: the base policy denies
    connect() on the daemon socket, and --enable=docker must re-allow it."""
    assert "docker" in sc.SAFEHOUSE_FEATURES
    assert _last_verdict(_policy(), DOCKER_SOCKET) == "deny", (
        "base policy no longer denies the docker socket; this test proves nothing"
    )
    assert _last_verdict(_policy("docker"), DOCKER_SOCKET) == "allow", (
        "--enable=docker no longer opens the docker daemon socket"
    )
    # Every feature sc enables must compose, not shadow the docker re-allow.
    assert _last_verdict(_policy(*sc.SAFEHOUSE_FEATURES), DOCKER_SOCKET) == "allow"


def _stub_safehouse(tmp_dir: str, argv_file: Path) -> str:
    """A safehouse that answers --version, records the argv of a real launch,
    and exits 99 so a launch is distinguishable from a gate abort."""
    path = Path(tmp_dir) / "safehouse"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "Agent Safehouse 0.11.1"; exit 0; fi\n'
        f'for a in "$@"; do echo "$a" >> "{argv_file}"; done\n'
        "exit 99\n"
    )
    path.chmod(0o755)
    return str(path)


def test_e2e_launch_passes_every_feature(tmp_dir: str) -> None:
    argv_file = Path(tmp_dir) / "argv"
    sc_tmp = Path(tmp_dir) / "tmp"
    sc_tmp.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "SAFEHOUSE_BIN": _stub_safehouse(tmp_dir, argv_file),
        "PROFILE_DIR": str(Path(tmp_dir) / "config"),
        "TMPDIR": str(sc_tmp),
    }
    r = subprocess.run(
        [str(SC), "--no-profile", "--", "--version"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert r.returncode == 99, f"sc did not reach exec: {r.returncode}\n{r.stderr}"
    argv = argv_file.read_text().splitlines()
    assert "--enable=docker" in argv, argv
    assert "--enable=ssh" in argv, argv
    assert "--enable=playwright-chrome" in argv, argv


def main() -> None:
    with tempfile.TemporaryDirectory() as profile_dir, tempfile.TemporaryDirectory() as tmp_dir:
        sc = load_sc(profile_dir)
        test_expected_features_are_enabled(sc)
        test_features_are_data_driven(sc)

        if shutil.which("safehouse"):
            test_installed_safehouse_accepts_every_feature(sc)
            test_docker_feature_opens_the_daemon_socket(sc)
            print("installed safehouse accepts every enabled feature")
        else:
            print("SKIP policy checks: safehouse not installed")

        test_e2e_launch_passes_every_feature(tmp_dir)
    print("OK")


if __name__ == "__main__":
    main()
