#!/usr/bin/env -S uv --quiet run --python >=3.11 --script
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for `sc -dw` / `sc -dr` without a path, which browse for directories.

The Seatbelt policy is frozen once a session starts, so what the picker returns
is what the session gets for its whole life — a wrong grant costs a relaunch.
The key handling is therefore pinned here, driven by a scripted fzf stub, along
with the two things that are easy to get silently wrong: ../ is a way to move
and must never be mounted, and the launch recorded for `sc -H` has to name the
picked directories rather than replay a picker.

Run directly: ./test_sc_dir_picker.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

SC = Path(__file__).resolve().parent / "sc"

# One line per fzf run: "<key>\t<chosen>\t<chosen>...". An empty key is Enter.
STUB_FZF = """\
#!/usr/bin/env python3
import os, sys
step_file = os.environ["FZF_STEP"]
step = int(open(step_file).read() or 0)
open(os.environ["FZF_ARGV"], "a").write(repr(sys.argv[1:]) + "\\n")
sys.stdin.read()
lines = [l for l in open(os.environ["FZF_SCRIPT"]).read().split("\\n") if l]
key, *chosen = lines[step].split("\\t")
open(step_file, "w").write(str(step + 1))
if any(a.startswith("--expect") for a in sys.argv):
    print(key)
for c in chosen:
    print(c)
"""


def load_sc(profile_dir: str):
    os.environ["PROFILE_DIR"] = profile_dir
    loader = SourceFileLoader("sc_under_test", str(SC))
    spec = importlib.util.spec_from_loader("sc_under_test", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)  # does not run main() (guarded by __main__)
    return mod


# ---------------------------------------------------------------- unit tests

def test_bare_flags_become_the_pick_sentinel(sc) -> None:
    """A -dw with no path can no longer fail; it asks for the browser."""
    ro, rw = sc.parse_args(["-dw"])[12:14]
    assert (ro, rw) == ([], [sc.PICK]), (ro, rw)
    ro, rw = sc.parse_args(["-dr"])[12:14]
    assert (ro, rw) == ([sc.PICK], []), (ro, rw)


def test_a_path_after_the_flag_still_wins(sc) -> None:
    ro, rw = sc.parse_args(["-dr", "/a", "-dw", "/b"])[12:14]
    assert (ro, rw) == (["/a"], ["/b"]), (ro, rw)


def test_a_flag_or_dashdash_after_the_flag_is_not_a_path(sc) -> None:
    ro, rw = sc.parse_args(["-dw", "-a"])[12:14]
    assert (ro, rw) == ([], [sc.PICK]), (ro, rw)
    parsed = sc.parse_args(["-dw", "--", "-p", "hi"])
    assert parsed[13] == [sc.PICK] and parsed[-1] == ["-p", "hi"], parsed


def test_passthrough_stays_last_in_the_parse_tuple(sc) -> None:
    """Other suites index this tuple from the end; the picker must not shift it."""
    parsed = sc.parse_args(["-dw", "-e", "datadog", "--", "--help"])
    assert parsed[-1] == ["--help"], parsed
    assert parsed[-2] == ["datadog"], parsed


def test_browse_entries_has_parent_subdirs_and_jumps(sc, tmp: Path) -> None:
    (tmp / "kid").mkdir()
    (tmp / "a-file").touch()
    rows = sc.browse_entries(str(tmp))
    assert rows[0] == "../", rows
    assert "./" not in rows, rows          # a row for "here" is noise on every level
    assert "kid/" in rows, rows
    assert "a-file" not in rows, rows
    assert rows[-2:] == ["~", "/"], rows


def test_browse_entries_at_the_filesystem_root_has_no_parent(sc) -> None:
    assert "../" not in sc.browse_entries("/")


def test_subdirs_sorts_dotdirs_last_and_survives_an_unreadable_dir(sc, tmp: Path) -> None:
    for name in ("zebra", ".hidden", "Alpha"):
        (tmp / "s" / name).mkdir(parents=True)
    assert sc.subdirs(str(tmp / "s")) == ["Alpha", "zebra", ".hidden"]
    locked = tmp / "locked"
    locked.mkdir(mode=0o000)
    try:
        assert sc.subdirs(str(locked)) == []
    finally:
        locked.chmod(0o755)


def test_cursor_index_is_one_based_and_falls_back_to_the_top(sc) -> None:
    rows = ["../", "a/", "b/"]
    assert sc.cursor_index(rows, "b/") == 3
    assert sc.cursor_index(rows, "gone/") == 1
    assert sc.cursor_index(rows, "") == 1


def test_recorded_argv_names_the_picked_dirs(sc) -> None:
    """`sc -H` replays a launch verbatim, so a bare -dw must not survive into it."""
    argv = sc.argv_with_picks(["-p", "work", "-dw", "-y"], [], [["/x", "/y"]])
    assert argv == ["-p", "work", "-dw", "/x", "-dw", "/y", "-y"], argv


def test_recorded_argv_leaves_typed_paths_and_passthrough_alone(sc) -> None:
    argv = sc.argv_with_picks(["-dw", "/typed", "-dr", "--", "-dw"], [["/ro"]], [])
    assert argv == ["-dw", "/typed", "-dr", "/ro", "--", "-dw"], argv


def test_recorded_argv_uses_home_relative_paths(sc) -> None:
    """History syncs across machines, where $HOME differs."""
    picked = str(Path.home() / "scratch")
    assert sc.argv_with_picks(["-dw"], [], [[picked]]) == ["-dw", "~/scratch"]


# ------------------------------------------------------------------ e2e tests

def _stub_fzf(tmp_dir: Path, steps: list[str]) -> dict[str, str]:
    """An fzf that replays `steps` and logs the argv of each run."""
    path = tmp_dir / "fzf"
    path.write_text(STUB_FZF)
    path.chmod(0o755)
    (tmp_dir / "fzf-script").write_text("\n".join(steps) + "\n")
    (tmp_dir / "fzf-step").write_text("0")
    (tmp_dir / "fzf-argv").write_text("")
    return {
        "SC_FZF_BIN": str(path),
        "FZF_SCRIPT": str(tmp_dir / "fzf-script"),
        "FZF_STEP": str(tmp_dir / "fzf-step"),
        "FZF_ARGV": str(tmp_dir / "fzf-argv"),
    }


def _stub_safehouse(tmp_dir: Path, argv_file: Path) -> str:
    """A safehouse that answers --version, records the argv of a real launch,
    and exits 99 so a launch is distinguishable from a gate abort."""
    path = tmp_dir / "safehouse"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "Agent Safehouse 0.11.1"; exit 0; fi\n'
        f'for a in "$@"; do echo "$a" >> "{argv_file}"; done\n'
        "exit 99\n"
    )
    path.chmod(0o755)
    return str(path)


def _launch(home: Path, cwd: str, args: list[str], steps: list[str]):
    """Run the real sc against stub fzf and safehouse. Returns the process, the
    safehouse argv, and one argv list per fzf run."""
    argv_file = home / "argv"
    argv_file.unlink(missing_ok=True)
    profile_dir = home / "config"
    profile_dir.mkdir(parents=True, exist_ok=True)
    sc_tmp = home / "tmp"
    sc_tmp.mkdir(exist_ok=True)
    env = {
        **os.environ,
        **_stub_fzf(home, steps),
        "SAFEHOUSE_BIN": _stub_safehouse(home, argv_file),
        "PROFILE_DIR": str(profile_dir),
        "TMPDIR": str(sc_tmp),
    }
    proc = subprocess.run(
        [str(SC), "--no-profile", *args, "--", "--version"],
        capture_output=True, text=True, env=env, cwd=cwd, timeout=120,
    )
    launched = argv_file.read_text().splitlines() if argv_file.exists() else []
    runs = [eval(l) for l in (home / "fzf-argv").read_text().split("\n") if l]
    return proc, launched, runs


def _granted(argv: list[str], flag: str) -> list[str]:
    for a in argv:
        if a.startswith(flag):
            return a.removeprefix(flag).split(":")
    return []


def test_e2e_picked_dir_reaches_safehouse_rw(tmp: Path, home: Path) -> None:
    (tmp / "src" / "monorepo").mkdir(parents=True)
    proc, argv, _ = _launch(home, str(tmp), ["-dw"], ["\tsrc/", "ctrl-s\tmonorepo/"])
    assert proc.returncode == 99, f"sc did not reach exec: {proc.stderr}"
    want = os.path.realpath(tmp / "src" / "monorepo")
    assert want in _granted(argv, "--add-dirs="), argv


def test_e2e_dr_grants_read_only(tmp: Path, home: Path) -> None:
    (tmp / "refs").mkdir(parents=True)
    proc, argv, _ = _launch(home, str(tmp), ["-dr"], ["ctrl-s\trefs/"])
    assert proc.returncode == 99, proc.stderr
    want = os.path.realpath(tmp / "refs")
    assert want in _granted(argv, "--add-dirs-ro="), argv
    assert want not in _granted(argv, "--add-dirs="), argv


def test_e2e_ctrl_a_collects_across_trees(tmp: Path, home: Path) -> None:
    for name in ("a", "b", "far"):
        (tmp / name).mkdir(parents=True)
    proc, argv, _ = _launch(
        home, str(tmp), ["-dw"],
        ["ctrl-a\ta/\tb/",      # take two here, keep the picker open
         "\tfar/",              # enter walks in
         "\t../",               # and back out
         "ctrl-s\tfar/"],       # take it, launch
    )
    assert proc.returncode == 99, proc.stderr
    granted = _granted(argv, "--add-dirs=")
    for name in ("a", "b", "far"):
        assert os.path.realpath(tmp / name) in granted, (name, granted)


def test_e2e_going_up_lands_the_cursor_on_the_directory_just_left(tmp: Path, home: Path) -> None:
    """Taking the directory you are in is enter on ../ then ctrl-s."""
    for name in ("aaa", "deep", "zzz"):
        (tmp / "up" / name).mkdir(parents=True)
    start = tmp / "up" / "deep"
    proc, argv, runs = _launch(home, str(start), ["-dw"], ["\t../", "ctrl-s\tdeep/"])
    assert proc.returncode == 99, proc.stderr
    assert os.path.realpath(start) in _granted(argv, "--add-dirs="), argv
    # ../ aaa/ deep/ zzz/ ~ / — deep/ is the third row.
    assert "--bind=load:pos(3)" in runs[1], runs[1]
    assert not any(a.startswith("--bind=load:pos") for a in runs[0]), runs[0]


def test_e2e_parent_row_is_never_mounted(tmp: Path, home: Path) -> None:
    """../ moves; mounting it would quietly grant a whole tree above the target."""
    (tmp / "deep" / "a").mkdir(parents=True)
    proc, argv, _ = _launch(home, str(tmp / "deep"), ["-dw"], ["ctrl-s\t../\ta/"])
    assert proc.returncode == 99, proc.stderr
    granted = _granted(argv, "--add-dirs=")
    assert os.path.realpath(tmp / "deep" / "a") in granted, granted
    assert os.path.realpath(tmp) not in granted, granted


def test_e2e_cancelling_the_picker_stops_the_launch(tmp: Path, home: Path) -> None:
    proc, argv, _ = _launch(home, str(tmp), ["-dw"], ["ctrl-s\t"])
    assert proc.returncode == 1, proc.stdout
    assert argv == [], argv
    assert "Nothing selected." in proc.stderr


def test_e2e_history_records_the_picked_dir(tmp: Path, home: Path) -> None:
    (tmp / "hist").mkdir(parents=True)
    proc, _, _ = _launch(home, str(tmp), ["-dw"], ["ctrl-s\thist/"])
    assert proc.returncode == 99, proc.stderr
    entry = json.loads((home / "config" / "history.jsonl").read_text().splitlines()[0])
    assert "-dw" in entry["args"], entry
    assert entry["args"][entry["args"].index("-dw") + 1].endswith("/hist"), entry


def main() -> None:
    with tempfile.TemporaryDirectory() as profile_dir, tempfile.TemporaryDirectory() as work:
        sc = load_sc(profile_dir)
        tmp = Path(work)
        test_bare_flags_become_the_pick_sentinel(sc)
        test_a_path_after_the_flag_still_wins(sc)
        test_a_flag_or_dashdash_after_the_flag_is_not_a_path(sc)
        test_passthrough_stays_last_in_the_parse_tuple(sc)
        test_browse_entries_has_parent_subdirs_and_jumps(sc, tmp)
        test_browse_entries_at_the_filesystem_root_has_no_parent(sc)
        test_subdirs_sorts_dotdirs_last_and_survives_an_unreadable_dir(sc, tmp)
        test_cursor_index_is_one_based_and_falls_back_to_the_top(sc)
        test_recorded_argv_names_the_picked_dirs(sc)
        test_recorded_argv_leaves_typed_paths_and_passthrough_alone(sc)
        test_recorded_argv_uses_home_relative_paths(sc)

        home = Path(profile_dir)
        for i, t in enumerate([
            test_e2e_picked_dir_reaches_safehouse_rw,
            test_e2e_dr_grants_read_only,
            test_e2e_ctrl_a_collects_across_trees,
            test_e2e_going_up_lands_the_cursor_on_the_directory_just_left,
            test_e2e_parent_row_is_never_mounted,
            test_e2e_cancelling_the_picker_stops_the_launch,
            test_e2e_history_records_the_picked_dir,
        ]):
            case = tmp / f"e2e{i}"
            case.mkdir()
            t(case, home)
    print("OK")


if __name__ == "__main__":
    main()
