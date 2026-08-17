#!/usr/bin/env -S uv --quiet run --python >=3.11 --script
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for the sc `-m/--mode` flag.

`-m auto` forwards `--permission-mode auto` to claude, overriding
permissions.defaultMode in settings.json for that launch. It is claude-only
and mutually exclusive with -y.
Run directly: ./test_sc_permission_mode.py
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

# Index into the parse_args() return tuple. Keep in sync with sc.
MODE_IDX = 14


def load_sc(profile_dir: str):
    os.environ["PROFILE_DIR"] = profile_dir
    sc_path = Path(__file__).resolve().parent / "sc"
    loader = SourceFileLoader("sc_under_test", str(sc_path))
    spec = importlib.util.spec_from_loader("sc_under_test", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)  # does not run main() (guarded by __main__)
    return mod


def expect_fail(sc, argv: list[str], needle: str) -> None:
    try:
        sc.parse_args(argv)
    except SystemExit as exc:
        assert exc.code == 1, exc.code
        return
    raise AssertionError(f"expected {argv} to fail with {needle!r}")


def test_mode_empty_by_default(sc):
    assert sc.parse_args([])[MODE_IDX] == ""
    assert sc.parse_args(["-y"])[MODE_IDX] == ""


def test_short_and_long_flag(sc):
    assert sc.parse_args(["-m", "auto"])[MODE_IDX] == "auto"
    assert sc.parse_args(["--mode", "plan"])[MODE_IDX] == "plan"


def test_value_is_not_validated(sc):
    # claude owns the list of valid modes; sc must not encode its own copy.
    assert sc.parse_args(["-m", "someFutureMode"])[MODE_IDX] == "someFutureMode"


def test_passthrough_stays_last(sc):
    parsed = sc.parse_args(["-m", "auto", "--", "-c"])
    assert parsed[MODE_IDX] == "auto"
    assert parsed[-1] == ["-c"], parsed


def test_missing_value_is_rejected(sc):
    expect_fail(sc, ["-m"], "requires a mode")


def test_mode_with_yes_is_rejected(sc):
    expect_fail(sc, ["-m", "auto", "-y"], "mutually exclusive")
    expect_fail(sc, ["-y", "--mode", "auto"], "mutually exclusive")


def test_mode_with_codex_is_rejected(sc):
    expect_fail(sc, ["--codex", "-m", "auto"], "claude-only")


def test_permission_flags(sc):
    _, _, yolo = sc.agent_spec(codex=False)
    assert sc.permission_flags(False, "", yolo) == []
    assert sc.permission_flags(False, "auto", yolo) == ["--permission-mode", "auto"]
    assert sc.permission_flags(True, "", yolo) == ["--dangerously-skip-permissions"]


def main() -> None:
    with tempfile.TemporaryDirectory() as profile_dir:
        sc = load_sc(profile_dir)
        test_mode_empty_by_default(sc)
        test_short_and_long_flag(sc)
        test_value_is_not_validated(sc)
        test_passthrough_stays_last(sc)
        test_missing_value_is_rejected(sc)
        test_mode_with_yes_is_rejected(sc)
        test_mode_with_codex_is_rejected(sc)
        test_permission_flags(sc)
    print("OK")


if __name__ == "__main__":
    main()
