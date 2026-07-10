#!/usr/bin/env -S uv --quiet run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for the ssh-agent /var/run socket workaround (macOS 26 Tahoe).

sc appends a sandbox profile fragment that re-allows the launchd-managed
ssh-agent socket under /var/run, which safehouse's --enable=ssh policy misses.
Run directly: ./test_sc_ssh_agent.py
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path


def load_sc(profile_dir: str):
    os.environ["PROFILE_DIR"] = profile_dir
    sc_path = Path(__file__).resolve().parent / "sc"
    loader = SourceFileLoader("sc_under_test", str(sc_path))
    spec = importlib.util.spec_from_loader("sc_under_test", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)  # does not run main() (guarded by __main__)
    return mod


def test_needs_allow_for_var_run_socket(sc):
    # macOS 26 Tahoe path (/var -> /private/var); realpath keeps it if it exists,
    # but the decision is prefix-based on the resolved value.
    assert sc.ssh_agent_needs_var_run_allow(
        "/private/var/run/com.apple.launchd.ABC/Listeners"
    ) is True


def test_no_allow_for_tmp_socket(sc):
    # Older macOS / already covered by safehouse.
    assert sc.ssh_agent_needs_var_run_allow(
        "/private/tmp/com.apple.launchd.ABC/Listeners"
    ) is False
    assert sc.ssh_agent_needs_var_run_allow(
        "/tmp/com.apple.launchd.ABC/Listeners"
    ) is False


def test_no_allow_for_empty(sc):
    assert sc.ssh_agent_needs_var_run_allow("") is False


def test_fragment_targets_var_run_launchd_class(sc):
    frag = sc.SSH_AGENT_VAR_RUN_PROFILE
    # Both filesystem and socket connect must be allowed for the /var/run class.
    assert "network-outbound" in frag, frag
    assert "unix-socket" in frag, frag
    assert r"/private/var/run/com\.apple\.launchd\.[^/]+/Listeners" in frag, frag
    # The escaped dots must survive as literal backslash-dot in the .sb file
    # (raw string, so no accidental Python de-escaping).
    assert "\\." in frag, "regex dots must be escaped for Seatbelt"


def test_write_profile_writes_stable_file_when_needed(sc, tmp_dir: str):
    # Redirect the fragment away from the real temp dir: in a live sc session
    # safehouse write-protects loaded --append-profile files, so the stable
    # path is not writable from inside the sandbox.
    sc.tempfile.gettempdir = lambda: tmp_dir
    real = "/private/var/run/com.apple.launchd.ZZZ/Listeners"
    prev_sock = os.environ.get("SSH_AUTH_SOCK")
    prev_platform = sc.sys.platform
    os.environ["SSH_AUTH_SOCK"] = real
    try:
        # os.path.realpath won't change an already-/private path, so the
        # decision holds regardless of whether the socket exists on disk.
        path = sc.write_ssh_agent_profile()
        if prev_platform == "darwin":
            assert path is not None, "expected a fragment path on darwin"
            assert Path(path).is_file(), path
            assert Path(path).read_text() == sc.SSH_AGENT_VAR_RUN_PROFILE
            # Stable path: a second call overwrites the same file, no litter.
            assert sc.write_ssh_agent_profile() == path
        else:
            assert path is None, "non-darwin must not append a macOS profile"
    finally:
        if prev_sock is None:
            os.environ.pop("SSH_AUTH_SOCK", None)
        else:
            os.environ["SSH_AUTH_SOCK"] = prev_sock


def test_write_profile_skips_tmp_socket(sc):
    prev_sock = os.environ.get("SSH_AUTH_SOCK")
    os.environ["SSH_AUTH_SOCK"] = "/private/tmp/com.apple.launchd.ZZZ/Listeners"
    try:
        assert sc.write_ssh_agent_profile() is None
    finally:
        if prev_sock is None:
            os.environ.pop("SSH_AUTH_SOCK", None)
        else:
            os.environ["SSH_AUTH_SOCK"] = prev_sock


def main() -> None:
    with tempfile.TemporaryDirectory() as profile_dir, tempfile.TemporaryDirectory() as tmp_dir:
        sc = load_sc(profile_dir)
        test_needs_allow_for_var_run_socket(sc)
        test_no_allow_for_tmp_socket(sc)
        test_no_allow_for_empty(sc)
        test_fragment_targets_var_run_launchd_class(sc)
        test_write_profile_writes_stable_file_when_needed(sc, tmp_dir)
        test_write_profile_skips_tmp_socket(sc)
    print("OK")


if __name__ == "__main__":
    main()
