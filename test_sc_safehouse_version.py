#!/usr/bin/env -S uv --quiet run --python >=3.11 --script
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for the minimum-safehouse-version gate.

sc depends on safehouse allowing the launchd ssh-agent socket under /var/run
(MIN_SAFEHOUSE_VERSION). On an older safehouse the breakage surfaces as a git
passphrase prompt nowhere near its cause, so sc refuses to launch instead.

Run directly: ./test_sc_safehouse_version.py

The end-to-end checks drive the real `sc` against a stub safehouse via
$SAFEHOUSE_BIN, so they need no particular safehouse installed.
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


def test_parses_safehouse_version_output(sc):
    assert sc.parse_version("Agent Safehouse 0.11.1\n") == (0, 11, 1)
    assert sc.parse_version("0.12") == (0, 12)
    assert sc.parse_version("Agent Safehouse 1.0.0-rc1") == (1, 0, 0)
    assert sc.parse_version("no version here") is None


def test_versions_below_floor_are_rejected(sc):
    assert sc.MIN_SAFEHOUSE_VERSION == (0, 11, 1), "floor moved; update the cases below"
    # The release that fixed the socket path, and anything after it, passes.
    for ok in ("Agent Safehouse 0.11.1", "Agent Safehouse 0.11.2",
               "Agent Safehouse 0.12.0", "Agent Safehouse 1.0.0"):
        assert sc.safehouse_version_too_old(ok) is None, ok
    # Everything before it is blocked, including the shorter 0.11 (no patch).
    for old in ("Agent Safehouse 0.11.0", "Agent Safehouse 0.11",
                "Agent Safehouse 0.10.4", "Agent Safehouse 0.9"):
        assert sc.safehouse_version_too_old(old) is not None, old


def test_unrecognized_output_fails_open(sc):
    """A build whose --version says something unexpected must not brick sc."""
    assert sc.safehouse_version_too_old("built from source") is None
    assert sc.safehouse_version_too_old("") is None


def test_unrunnable_binary_fails_open(sc):
    """Never abort just because the version probe itself failed."""
    sc.require_safehouse_version("/nonexistent/safehouse")  # must not raise or exit


def test_installed_safehouse_meets_floor(sc):
    """The safehouse on this machine must satisfy the gate, else sc cannot run."""
    out = subprocess.run(
        ["safehouse", "--version"], capture_output=True, text=True, check=True,
    ).stdout
    assert sc.safehouse_version_too_old(out) is None, (
        f"installed safehouse is below the gate: {out.strip()}"
    )


def _stub_safehouse(tmp_dir: str, version_line: str) -> str:
    """A safehouse that answers --version and exits 99 for anything else, so a
    launch that gets past the gate is distinguishable from one blocked by it."""
    path = Path(tmp_dir) / "safehouse"
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "{version_line}"; exit 0; fi\n'
        "exit 99\n"
    )
    path.chmod(0o755)
    return str(path)


def _run_sc(tmp_dir: str, version_line: str) -> subprocess.CompletedProcess:
    # PROFILE_DIR redirects sc's config/history writes away from the real ~;
    # TMPDIR redirects the --append-profile fragments, which a live sc session
    # write-protects in the real temp dir (safehouse locks loaded profiles).
    sc_tmp = Path(tmp_dir) / "tmp"
    sc_tmp.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "SAFEHOUSE_BIN": _stub_safehouse(tmp_dir, version_line),
        "PROFILE_DIR": str(Path(tmp_dir) / "config"),
        "TMPDIR": str(sc_tmp),
    }
    return subprocess.run(
        [str(SC), "--no-profile", "--", "--version"],
        capture_output=True, text=True, env=env, timeout=120,
    )


def test_e2e_old_safehouse_blocks_launch(tmp_dir: str) -> None:
    r = _run_sc(tmp_dir, "Agent Safehouse 0.11.0")
    assert r.returncode == 1, f"expected the gate to abort: {r.returncode}\n{r.stderr}"
    assert "too old" in r.stderr, r.stderr
    # The message must name the version found, the floor, and the way out.
    assert "0.11.0" in r.stderr and "0.11.1" in r.stderr, r.stderr
    assert "brew upgrade agent-safehouse" in r.stderr, r.stderr
    # Blocked before the sandbox is ever built, so nothing was launched.
    assert "Launching:" not in r.stderr, r.stderr


def test_e2e_current_safehouse_passes_gate(tmp_dir: str) -> None:
    """A new-enough safehouse must reach exec — exit 99 is the stub, meaning
    the gate let sc through rather than aborting at it."""
    r = _run_sc(tmp_dir, "Agent Safehouse 0.11.1")
    assert r.returncode == 99, f"gate did not let sc through: {r.returncode}\n{r.stderr}"
    assert "too old" not in r.stderr, r.stderr


def main() -> None:
    with tempfile.TemporaryDirectory() as profile_dir, tempfile.TemporaryDirectory() as tmp_dir:
        sc = load_sc(profile_dir)
        test_parses_safehouse_version_output(sc)
        test_versions_below_floor_are_rejected(sc)
        test_unrecognized_output_fails_open(sc)
        test_unrunnable_binary_fails_open(sc)

        if shutil.which("safehouse"):
            test_installed_safehouse_meets_floor(sc)
            print("installed safehouse meets the gate")
        else:
            print("SKIP installed-version check: safehouse not installed")

        test_e2e_old_safehouse_blocks_launch(tmp_dir)
        test_e2e_current_safehouse_passes_gate(tmp_dir)
    print("OK")


if __name__ == "__main__":
    main()
