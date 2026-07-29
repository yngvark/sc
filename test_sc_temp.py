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
import shutil
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


def test_make_temp_dir_creates_real_dir(sc):
    d = sc.make_temp_dir()
    try:
        assert Path(d).is_dir(), d
        # mktemp -d makes a private, empty dir
        assert os.listdir(d) == [], d
    finally:
        shutil.rmtree(d, ignore_errors=True)

    d2 = sc.make_temp_dir()
    try:
        assert d2 != d, "each call must create a distinct dir"
    finally:
        shutil.rmtree(d2, ignore_errors=True)


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


def test_seed_claude_trust_new_file(sc, tmp):
    state = tmp / "claude-new.json"
    target = tmp / "workspace"
    target.mkdir()
    sc.seed_claude_trust(str(target), state_file=state)
    data = json.loads(state.read_text())
    entry = data["projects"][os.path.realpath(str(target))]
    assert entry["hasTrustDialogAccepted"] is True, data


def test_seed_claude_trust_preserves_existing(sc, tmp):
    state = tmp / "claude-existing.json"
    state.write_text(json.dumps({
        "numStartups": 7,
        "projects": {"/some/other": {"hasTrustDialogAccepted": True, "allowedTools": ["Bash"]}},
    }))
    target = tmp / "workspace2"
    target.mkdir()
    sc.seed_claude_trust(str(target), state_file=state)
    data = json.loads(state.read_text())
    assert data["numStartups"] == 7, data
    assert data["projects"]["/some/other"]["allowedTools"] == ["Bash"], data
    assert data["projects"][os.path.realpath(str(target))]["hasTrustDialogAccepted"] is True, data


def test_seed_claude_trust_uses_realpath(sc, tmp):
    """Claude Code keys projects by realpath; a symlinked temp dir must be
    recorded under its resolved path, like /var/folders -> /private/var/..."""
    state = tmp / "claude-symlink.json"
    real = tmp / "real"
    real.mkdir()
    link = tmp / "link"
    link.symlink_to(real)
    sc.seed_claude_trust(str(link), state_file=state)
    data = json.loads(state.read_text())
    assert os.path.realpath(str(real)) in data["projects"], data


def test_seed_claude_trust_skips_corrupt_file(sc, tmp):
    state = tmp / "claude-corrupt.json"
    state.write_text("{not json")
    target = tmp / "workspace3"
    target.mkdir()
    sc.seed_claude_trust(str(target), state_file=state)  # must not raise
    assert state.read_text() == "{not json", "corrupt file must be left untouched"


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
        test_make_temp_dir_creates_real_dir(sc)
        test_record_launch_uses_given_cwd(sc, tmp)
        test_codex_flag_off_by_default(sc)
        test_codex_flag_on(sc)
        test_codex_does_not_shift_temp_or_passthrough(sc)
        test_agent_spec_claude(sc)
        test_agent_spec_codex(sc)
        test_profile_env_pass(sc)
        test_seed_claude_trust_new_file(sc, tmp)
        test_seed_claude_trust_preserves_existing(sc, tmp)
        test_seed_claude_trust_uses_realpath(sc, tmp)
        test_seed_claude_trust_skips_corrupt_file(sc, tmp)
        test_profile_dirs_expands_env_vars(sc, tmp)
    print("OK")


if __name__ == "__main__":
    main()
