#!/usr/bin/env -S uv --quiet run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for the sc `-t/--temp` feature.

Loads the `sc` script as a module with PROFILE_DIR pointed at a temp dir, so
the pure parsing helpers can be exercised without launching anything.
Run directly: ./test_sc_temp.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

# Indices into the parse_args() return tuple. Keep in sync with sc.
TEMP_IDX = 8
CODEX_IDX = 9


def load_sc(profile_dir: str):
    os.environ["PROFILE_DIR"] = profile_dir
    sc_path = Path(__file__).resolve().parent / "sc"
    loader = SourceFileLoader("sc_under_test", str(sc_path))
    spec = importlib.util.spec_from_loader("sc_under_test", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)  # does not run main() (guarded by __main__)
    return mod


def test_flag_off_by_default(sc):
    assert sc.parse_args([])[TEMP_IDX] is False
    assert sc.parse_args(["-a", "-y"])[TEMP_IDX] is False


def test_short_and_long_flag(sc):
    assert sc.parse_args(["-t"])[TEMP_IDX] is True
    assert sc.parse_args(["--temp"])[TEMP_IDX] is True


def test_temp_combines_with_passthrough(sc):
    parsed = sc.parse_args(["-t", "--", "-c"])
    assert parsed[TEMP_IDX] is True
    assert parsed[-1] == ["-c"], parsed  # passthrough is last in the tuple


def test_make_temp_dir_creates_real_dir_under_parent(sc, tmp):
    orig_parent = sc.TEMP_PARENT
    sc.TEMP_PARENT = tmp / "temp-parent"  # not pre-created: make_temp_dir must mkdir it
    try:
        d = sc.make_temp_dir()
        assert Path(d).is_dir(), d
        assert Path(d).parent == sc.TEMP_PARENT, d
        assert os.listdir(d) == [], d
        d2 = sc.make_temp_dir()
        assert d2 != d, "each call must create a distinct dir"
    finally:
        sc.TEMP_PARENT = orig_parent


def _trust_state(tmp, name: str, projects: dict) -> Path:
    state = tmp / name
    state.write_text(json.dumps({"projects": projects}))
    return state


def test_trusted_exact_dir(sc, tmp):
    target = tmp / "trust-exact"
    target.mkdir()
    state = _trust_state(tmp, "s1.json", {os.path.realpath(str(target)): {"hasTrustDialogAccepted": True}})
    assert sc.is_claude_trusted(str(target), state_file=state) is True


def test_trusted_via_ancestor(sc, tmp):
    """The design depends on this: trusting the temp parent covers each
    fresh temp dir created inside it (claude walks up from cwd)."""
    parent = tmp / "trust-parent"
    child = parent / "a" / "b"
    child.mkdir(parents=True)
    state = _trust_state(tmp, "s2.json", {os.path.realpath(str(parent)): {"hasTrustDialogAccepted": True}})
    assert sc.is_claude_trusted(str(child), state_file=state) is True


def test_untrusted_dir(sc, tmp):
    target = tmp / "trust-none"
    target.mkdir()
    state = _trust_state(tmp, "s3.json", {"/some/other": {"hasTrustDialogAccepted": True}})
    assert sc.is_claude_trusted(str(target), state_file=state) is False
    # accepted=False must not count as trusted either
    state2 = _trust_state(tmp, "s4.json", {os.path.realpath(str(target)): {"hasTrustDialogAccepted": False}})
    assert sc.is_claude_trusted(str(target), state_file=state2) is False


def test_missing_or_corrupt_state_is_untrusted(sc, tmp):
    """Fail closed: if we can't read ~/.claude.json we assume untrusted."""
    target = tmp / "trust-fail-closed"
    target.mkdir()
    assert sc.is_claude_trusted(str(target), state_file=tmp / "nope.json") is False
    corrupt = tmp / "corrupt.json"
    corrupt.write_text("{not json")
    assert sc.is_claude_trusted(str(target), state_file=corrupt) is False


def test_trusted_resolves_realpath(sc, tmp):
    """claude keys trust by realpath (/var/folders -> /private/var/...);
    a symlinked path must match its resolved target's record."""
    real = tmp / "trust-real"
    real.mkdir()
    link = tmp / "trust-link"
    link.symlink_to(real)
    state = _trust_state(tmp, "s5.json", {os.path.realpath(str(real)): {"hasTrustDialogAccepted": True}})
    assert sc.is_claude_trusted(str(link), state_file=state) is True


def test_record_launch_uses_given_cwd(sc, tmp):
    """With -t, history must store the invocation dir, not the temp dir."""
    sc.HISTORY_FILE.unlink(missing_ok=True)
    proj = tmp / "proj"
    proj.mkdir()
    # Simulate: invoked from proj, then chdir'd into an ephemeral temp dir.
    os.chdir(tmp)  # pretend cwd already moved away
    sc.record_launch(["-t"], cwd=str(proj))
    hist = sc.load_history()
    assert len(hist) == 1, hist
    assert hist[0]["dir"] == str(proj), hist
    assert hist[0]["args"] == ["-t"], hist


def test_codex_flag_off_by_default(sc):
    assert sc.parse_args([])[CODEX_IDX] is False
    assert sc.parse_args(["-t", "-y"])[CODEX_IDX] is False


def test_codex_flag_on(sc):
    assert sc.parse_args(["--codex"])[CODEX_IDX] is True


def test_codex_does_not_shift_temp_or_passthrough(sc):
    """Adding --codex must not move the temp field or push passthrough off [-1]."""
    parsed = sc.parse_args(["--codex", "-t", "--", "-c"])
    assert parsed[TEMP_IDX] is True, parsed
    assert parsed[CODEX_IDX] is True, parsed
    assert parsed[-1] == ["-c"], parsed  # passthrough stays last


def test_agent_spec_claude(sc):
    binary, cfg, yolo = sc.agent_spec(False)
    assert binary == "claude", binary
    assert cfg.endswith("/.claude"), cfg
    assert yolo == "--dangerously-skip-permissions", yolo


def test_agent_spec_codex(sc):
    binary, cfg, yolo = sc.agent_spec(True)
    assert binary == "codex", binary
    assert cfg.endswith("/.codex"), cfg
    assert yolo == "--dangerously-bypass-approvals-and-sandbox", yolo


def test_profile_env_pass(sc):
    assert sc.profile_env_pass({}) == []
    assert sc.profile_env_pass({"env": {"pass": ["FOO", "BAR"]}}) == ["FOO", "BAR"]


def test_profile_dirs_expands_env_vars(sc, tmp):
    target = tmp / "notes"
    target.mkdir()
    os.environ["SC_TEST_NOTES_DIR"] = str(target)
    try:
        ro, rw = sc.profile_dirs({"dirs": {"rw": ["$SC_TEST_NOTES_DIR"]}})
        assert rw == [str(target)], rw
        assert ro == [], ro
    finally:
        del os.environ["SC_TEST_NOTES_DIR"]


def main() -> None:
    with tempfile.TemporaryDirectory() as profile_dir, tempfile.TemporaryDirectory() as work:
        sc = load_sc(profile_dir)
        tmp = Path(work)
        test_flag_off_by_default(sc)
        test_short_and_long_flag(sc)
        test_temp_combines_with_passthrough(sc)
        test_make_temp_dir_creates_real_dir_under_parent(sc, tmp)
        test_trusted_exact_dir(sc, tmp)
        test_trusted_via_ancestor(sc, tmp)
        test_untrusted_dir(sc, tmp)
        test_missing_or_corrupt_state_is_untrusted(sc, tmp)
        test_trusted_resolves_realpath(sc, tmp)
        test_record_launch_uses_given_cwd(sc, tmp)
        test_codex_flag_off_by_default(sc)
        test_codex_flag_on(sc)
        test_codex_does_not_shift_temp_or_passthrough(sc)
        test_agent_spec_claude(sc)
        test_agent_spec_codex(sc)
        test_profile_env_pass(sc)
        test_profile_dirs_expands_env_vars(sc, tmp)
    print("OK")


if __name__ == "__main__":
    main()
