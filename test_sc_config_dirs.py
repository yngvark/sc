#!/usr/bin/env -S uv --quiet run --python >=3.11 --script
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for the [when] per-launch-dir grants in ~/.config/sc/config.toml.

The Seatbelt policy is frozen once a session starts, so a grant that silently
fails to match is only discovered as an `Operation not permitted` mid-session,
after which the only fix is a relaunch. The matching rules are therefore pinned
here: prefix matching on realpaths, unioned across every matching key, and no
match for a sibling that merely shares a name prefix.

Run directly: ./test_sc_config_dirs.py
"""

from __future__ import annotations

import importlib.util
import os
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


def mkdirs(root: Path, *names: str) -> list[str]:
    out = []
    for n in names:
        (root / n).mkdir(parents=True, exist_ok=True)
        out.append(str(root / n))
    return out


def test_exact_dir_match(sc, tmp: Path) -> None:
    work, shared = mkdirs(tmp, "work", "shared")
    ro, rw = sc.config_dirs_for({"when": {work: {"rw": [shared]}}}, work)
    assert rw == [shared], rw
    assert ro == [], ro


def test_matches_from_a_subdirectory(sc, tmp: Path) -> None:
    work, shared = mkdirs(tmp, "work", "shared")
    deep, = mkdirs(tmp, "work/a/b/c")
    _, rw = sc.config_dirs_for({"when": {work: {"rw": [shared]}}}, deep)
    assert rw == [shared], rw


def test_broad_and_narrow_keys_union(sc, tmp: Path) -> None:
    """A key for all repos and a key for one repo both apply; grants add up."""
    git, repo, all_ro, one_rw = mkdirs(tmp, "git", "git/proj", "refs", "scratch")
    config = {"when": {git: {"ro": [all_ro]}, repo: {"rw": [one_rw]}}}
    ro, rw = sc.config_dirs_for(config, repo)
    assert ro == [all_ro], ro
    assert rw == [one_rw], rw


def test_name_prefix_is_not_a_path_prefix(sc, tmp: Path) -> None:
    """~/src-other must not match a key of ~/src."""
    git, other, shared = mkdirs(tmp, "git", "git-other", "shared")
    ro, rw = sc.config_dirs_for({"when": {git: {"rw": [shared]}}}, other)
    assert (ro, rw) == ([], []), (ro, rw)


def test_parent_dir_does_not_match(sc, tmp: Path) -> None:
    """Matching goes downwards only: a key deeper than cwd grants nothing."""
    parent, child, shared = mkdirs(tmp, "p", "p/child", "shared")
    ro, rw = sc.config_dirs_for({"when": {child: {"rw": [shared]}}}, parent)
    assert (ro, rw) == ([], []), (ro, rw)


def test_ro_and_rw_land_in_the_right_list(sc, tmp: Path) -> None:
    work, r, w = mkdirs(tmp, "work", "readable", "writable")
    ro, rw = sc.config_dirs_for({"when": {work: {"ro": [r], "rw": [w]}}}, work)
    assert ro == [r] and rw == [w], (ro, rw)


def test_expansion_of_home_and_vars(sc, tmp: Path) -> None:
    """Both keys and values expand ~ and $VARS, like profile [dirs]."""
    work, shared = mkdirs(tmp, "work", "shared")
    os.environ["SC_TEST_WORK"] = work
    os.environ["SC_TEST_SHARED"] = shared
    _, rw = sc.config_dirs_for({"when": {"$SC_TEST_WORK": {"rw": ["$SC_TEST_SHARED"]}}}, work)
    assert rw == [shared], rw
    # ~ resolves too: a key of "~" covers any cwd under $HOME.
    home_cwd = str(Path.home())
    _, rw2 = sc.config_dirs_for({"when": {"~": {"rw": [shared]}}}, home_cwd)
    assert rw2 == [shared], rw2


def test_symlinked_cwd_matches_realpath_key(sc, tmp: Path) -> None:
    """safehouse resolves paths to realpaths, so matching must too."""
    real, shared = mkdirs(tmp, "real", "shared")
    link = tmp / "link"
    link.symlink_to(real)
    _, rw = sc.config_dirs_for({"when": {real: {"rw": [shared]}}}, str(link))
    assert rw == [shared], "symlinked cwd did not match its realpath key"
    _, rw = sc.config_dirs_for({"when": {str(link): {"rw": [shared]}}}, real)
    assert rw == [shared], "symlinked key did not match the real cwd"


def test_missing_dir_is_skipped_not_fatal(sc, tmp: Path) -> None:
    """safehouse rejects a missing --add-dirs entry, so a stale line must not
    block the launch — it is dropped with a warning."""
    work, shared = mkdirs(tmp, "work", "shared")
    config = {"when": {work: {"rw": [str(tmp / "gone"), shared]}}}
    _, rw = sc.config_dirs_for(config, work)
    assert rw == [shared], rw


def test_empty_and_malformed_entries(sc, tmp: Path) -> None:
    work, = mkdirs(tmp, "work")
    assert sc.config_dirs_for({}, work) == ([], [])
    assert sc.config_dirs_for({"when": {}}, work) == ([], [])
    assert sc.config_dirs_for({"when": {work: {}}}, work) == ([], [])
    # a non-table value is ignored rather than crashing the launch
    assert sc.config_dirs_for({"when": {work: "nope"}}, work) == ([], [])


def test_load_config_absent_and_malformed(sc) -> None:
    assert not sc.CONFIG_FILE.exists()
    assert sc.load_config() == {}
    sc.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    sc.CONFIG_FILE.write_text('[when."~/x"]\nrw = ["unclosed\n')
    try:
        sc.load_config()
    except SystemExit as e:
        assert e.code == 1, e.code
    else:
        raise AssertionError("malformed config.toml did not abort")
    sc.CONFIG_FILE.unlink()
    # a well-formed file round-trips
    sc.CONFIG_FILE.write_text('[when."~/x"]\nrw = ["~/y"]\n')
    assert sc.load_config() == {"when": {"~/x": {"rw": ["~/y"]}}}
    sc.CONFIG_FILE.unlink()


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


def _launch(tmp_dir: Path, cwd: str, config_text: str | None) -> list[str]:
    """Run the real sc in CWD against a stub safehouse; return its argv."""
    argv_file = tmp_dir / "argv"
    argv_file.unlink(missing_ok=True)
    profile_dir = tmp_dir / "config"
    profile_dir.mkdir(parents=True, exist_ok=True)
    config = profile_dir / "config.toml"
    if config_text is None:
        config.unlink(missing_ok=True)
    else:
        config.write_text(config_text)
    sc_tmp = tmp_dir / "tmp"
    sc_tmp.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "SAFEHOUSE_BIN": _stub_safehouse(tmp_dir, argv_file),
        "PROFILE_DIR": str(profile_dir),
        "TMPDIR": str(sc_tmp),
    }
    r = subprocess.run(
        [str(SC), "--no-profile", "--", "--version"],
        capture_output=True, text=True, env=env, cwd=cwd, timeout=120,
    )
    assert r.returncode == 99, f"sc did not reach exec: {r.returncode}\n{r.stderr}"
    return argv_file.read_text().splitlines()


def _granted_rw(argv: list[str]) -> list[str]:
    """The paths in the --add-dirs= flag (colon-joined into a single arg)."""
    for a in argv:
        if a.startswith("--add-dirs="):
            return a.removeprefix("--add-dirs=").split(":")
    return []


def test_e2e_config_dirs_reach_safehouse(tmp: Path, stub_home: Path) -> None:
    work, shared = mkdirs(tmp, "e2e/work", "e2e/shared")
    config_text = f'[when."{work}"]\nrw = ["{shared}"]\n'

    argv = _launch(stub_home, work, config_text)
    assert shared in _granted_rw(argv), f"{shared} not granted rw: {argv}"

    # from a subdirectory too
    sub, = mkdirs(tmp, "e2e/work/deep/er")
    argv = _launch(stub_home, sub, config_text)
    assert shared in _granted_rw(argv), f"not granted from a subdir: {argv}"

    # and not from an unmapped dir
    elsewhere, = mkdirs(tmp, "e2e/elsewhere")
    argv = _launch(stub_home, elsewhere, config_text)
    assert not any(shared in a for a in argv), f"{shared} granted without a match: {argv}"

    # no config.toml at all still launches
    argv = _launch(stub_home, work, None)
    assert not any(shared in a for a in argv), argv


def main() -> None:
    with tempfile.TemporaryDirectory() as profile_dir, tempfile.TemporaryDirectory() as work:
        sc = load_sc(profile_dir)
        tmp = Path(work)
        test_exact_dir_match(sc, tmp)
        test_matches_from_a_subdirectory(sc, tmp)
        test_broad_and_narrow_keys_union(sc, tmp)
        test_name_prefix_is_not_a_path_prefix(sc, tmp)
        test_parent_dir_does_not_match(sc, tmp)
        test_ro_and_rw_land_in_the_right_list(sc, tmp)
        test_expansion_of_home_and_vars(sc, tmp)
        test_symlinked_cwd_matches_realpath_key(sc, tmp)
        test_missing_dir_is_skipped_not_fatal(sc, tmp)
        test_empty_and_malformed_entries(sc, tmp)
        test_load_config_absent_and_malformed(sc)
        test_e2e_config_dirs_reach_safehouse(tmp, Path(profile_dir))
    print("OK")


if __name__ == "__main__":
    main()
