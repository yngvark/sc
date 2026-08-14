#!/usr/bin/env -S uv --quiet run --python >=3.11 --script
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for the [group] dir groups in ~/.config/sc/config.toml.

The Seatbelt policy is frozen once a session starts, so a grant that silently
fails to match is only discovered as an `Operation not permitted` mid-session,
after which the only fix is a relaunch. The group rules are therefore pinned
here: a launch in any member grants every member, matching is prefix-on-realpath,
groups union, and no chaining between groups that share a member.

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
    """Create dirs under ROOT and return their realpaths — what sc grants, since
    a link path mounted alone leaves the contents unreachable in the sandbox."""
    out = []
    for n in names:
        (root / n).mkdir(parents=True, exist_ok=True)
        out.append(os.path.realpath(root / n))
    return out


def test_any_member_grants_the_whole_group(sc, tmp: Path) -> None:
    """The point of a group: enter from any side, get all of it."""
    work, scratch, notes = mkdirs(tmp, "work", "scratch", "notes")
    config = {"group": {"g": {"rw": [work, scratch, notes]}}}
    for cwd in (work, scratch, notes):
        ro, rw = sc.config_dirs_for(config, cwd)
        assert rw == [work, scratch, notes], (cwd, rw)
        assert ro == [], ro


def test_matches_from_a_subdirectory(sc, tmp: Path) -> None:
    work, shared = mkdirs(tmp, "work", "shared")
    deep, = mkdirs(tmp, "work/a/b/c")
    _, rw = sc.config_dirs_for({"group": {"g": {"rw": [work, shared]}}}, deep)
    assert rw == [work, shared], rw


def test_single_member_group(sc, tmp: Path) -> None:
    """The broad case — every repo readable while working in any of them —
    names the dir once, not once as a key and again as a value."""
    git, = mkdirs(tmp, "git")
    repo, = mkdirs(tmp, "git/proj")
    ro, rw = sc.config_dirs_for({"group": {"repos": {"ro": [git]}}}, repo)
    assert ro == [git], ro
    assert rw == [], rw


def test_groups_union(sc, tmp: Path) -> None:
    """A broad group and a narrow one both activate; grants add up."""
    git, repo, all_ro, one_rw = mkdirs(tmp, "git", "git/proj", "refs", "scratch")
    config = {
        "group": {
            "repos": {"ro": [git, all_ro]},
            "proj": {"rw": [repo, one_rw]},
        }
    }
    ro, rw = sc.config_dirs_for(config, repo)
    assert ro == [git, all_ro], ro
    assert rw == [repo, one_rw], rw


def test_groups_do_not_chain(sc, tmp: Path) -> None:
    """Sharing a member does not merge two groups: launching in A grants A and
    B, not C. What a launch gets stays readable off the file."""
    a, b, c = mkdirs(tmp, "a", "b", "c")
    config = {"group": {"ab": {"rw": [a, b]}, "bc": {"rw": [b, c]}}}
    _, rw = sc.config_dirs_for(config, a)
    assert rw == [a, b], rw
    # entering the shared member activates both groups, so b is granted twice
    _, rw = sc.config_dirs_for(config, b)
    assert set(rw) == {a, b, c}, rw


def test_name_prefix_is_not_a_path_prefix(sc, tmp: Path) -> None:
    """~/src-other must not match a member of ~/src."""
    git, other, shared = mkdirs(tmp, "git", "git-other", "shared")
    ro, rw = sc.config_dirs_for({"group": {"g": {"rw": [git, shared]}}}, other)
    assert (ro, rw) == ([], []), (ro, rw)


def test_parent_dir_does_not_match(sc, tmp: Path) -> None:
    """Matching goes downwards only: a member deeper than cwd grants nothing."""
    parent, child, shared = mkdirs(tmp, "p", "p/child", "shared")
    ro, rw = sc.config_dirs_for({"group": {"g": {"rw": [child, shared]}}}, parent)
    assert (ro, rw) == ([], []), (ro, rw)


def test_ro_and_rw_land_in_the_right_list(sc, tmp: Path) -> None:
    work, r, w = mkdirs(tmp, "work", "readable", "writable")
    config = {"group": {"a": {"ro": [work, r]}, "b": {"rw": [work, w]}}}
    ro, rw = sc.config_dirs_for(config, work)
    assert ro == [work, r] and rw == [work, w], (ro, rw)


def test_both_access_levels_in_one_group_aborts(sc, tmp: Path) -> None:
    """A group is one set of dirs at one access level; mixing them would make
    'in any member, get all members' mean two different things at once."""
    work, other = mkdirs(tmp, "work", "other")
    config = {"group": {"g": {"ro": [work], "rw": [other]}}}
    try:
        sc.config_dirs_for(config, work)
    except SystemExit as e:
        assert e.code == 1, e.code
    else:
        raise AssertionError("a group with both ro and rw did not abort")


def test_expansion_of_home_and_vars(sc, tmp: Path) -> None:
    """Members expand ~ and $VARS, like profile [dirs]."""
    work, shared = mkdirs(tmp, "work", "shared")
    os.environ["SC_TEST_WORK"] = work
    os.environ["SC_TEST_SHARED"] = shared
    config = {"group": {"g": {"rw": ["$SC_TEST_WORK", "$SC_TEST_SHARED"]}}}
    _, rw = sc.config_dirs_for(config, work)
    assert rw == [work, shared], rw
    # ~ resolves too: a member of "~" covers any cwd under $HOME.
    home = os.path.realpath(Path.home())
    _, rw2 = sc.config_dirs_for({"group": {"g": {"rw": ["~", shared]}}}, home)
    assert rw2 == [home, shared], rw2


def test_symlinks_are_resolved_on_both_sides(sc, tmp: Path) -> None:
    """Seatbelt matches the resolved path of the file being opened, so both the
    cwd match and the granted path go through realpath: a symlinked member must
    match a real cwd, and must be granted at its target."""
    real, shared = mkdirs(tmp, "real", "shared")
    link = Path(tmp) / "link"
    if not link.exists():
        link.symlink_to(real)
    _, rw = sc.config_dirs_for({"group": {"g": {"rw": [real, shared]}}}, str(link))
    assert rw == [real, shared], "symlinked cwd did not match its realpath member"
    _, rw = sc.config_dirs_for({"group": {"g": {"rw": [str(link), shared]}}}, real)
    assert rw == [real, shared], "symlinked member did not match/resolve to the real dir"


def test_missing_dir_is_skipped_not_fatal(sc, tmp: Path) -> None:
    """safehouse rejects a missing --add-dirs entry, so a stale line must not
    block the launch — it is dropped with a warning."""
    work, shared = mkdirs(tmp, "work", "shared")
    config = {"group": {"g": {"rw": [work, str(tmp / "gone"), shared]}}}
    _, rw = sc.config_dirs_for(config, work)
    assert rw == [work, shared], rw


def test_empty_and_malformed_entries(sc, tmp: Path) -> None:
    work, = mkdirs(tmp, "work")
    assert sc.config_dirs_for({}, work) == ([], [])
    assert sc.config_dirs_for({"group": {}}, work) == ([], [])
    assert sc.config_dirs_for({"group": {"g": {}}}, work) == ([], [])
    # a non-table value is ignored rather than crashing the launch
    assert sc.config_dirs_for({"group": {"g": "nope"}}, work) == ([], [])


def test_load_config_absent_and_malformed(sc) -> None:
    assert not sc.CONFIG_FILE.exists()
    assert sc.load_config() == {}
    sc.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    sc.CONFIG_FILE.write_text('[group.g]\nrw = ["unclosed\n')
    try:
        sc.load_config()
    except SystemExit as e:
        assert e.code == 1, e.code
    else:
        raise AssertionError("malformed config.toml did not abort")
    sc.CONFIG_FILE.unlink()
    # a well-formed file round-trips
    sc.CONFIG_FILE.write_text('[group.g]\nrw = ["~/x", "~/y"]\n')
    assert sc.load_config() == {"group": {"g": {"rw": ["~/x", "~/y"]}}}
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


def test_e2e_group_dirs_reach_safehouse(tmp: Path, stub_home: Path) -> None:
    work, shared = mkdirs(tmp, "e2e/work", "e2e/shared")
    config_text = f'[group.e2e]\nrw = ["{work}", "{shared}"]\n'

    argv = _launch(stub_home, work, config_text)
    assert shared in _granted_rw(argv), f"{shared} not granted rw: {argv}"

    # from the other member: the group is circular
    argv = _launch(stub_home, shared, config_text)
    assert work in _granted_rw(argv), f"{work} not granted from the other member: {argv}"

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
        test_any_member_grants_the_whole_group(sc, tmp)
        test_matches_from_a_subdirectory(sc, tmp)
        test_single_member_group(sc, tmp)
        test_groups_union(sc, tmp)
        test_groups_do_not_chain(sc, tmp)
        test_name_prefix_is_not_a_path_prefix(sc, tmp)
        test_parent_dir_does_not_match(sc, tmp)
        test_ro_and_rw_land_in_the_right_list(sc, tmp)
        test_both_access_levels_in_one_group_aborts(sc, tmp)
        test_expansion_of_home_and_vars(sc, tmp)
        test_symlinks_are_resolved_on_both_sides(sc, tmp)
        test_missing_dir_is_skipped_not_fatal(sc, tmp)
        test_empty_and_malformed_entries(sc, tmp)
        test_load_config_absent_and_malformed(sc)
        test_e2e_group_dirs_reach_safehouse(tmp, Path(profile_dir))
    print("OK")


if __name__ == "__main__":
    main()
