#!/usr/bin/env -S uv --quiet run --python >=3.11 --script
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for the symlink bridge: keeping a granted dir reachable by the path it
was written with, not only by its realpath.

safehouse resolves every --add-dirs path and emits ancestor literals for the
*resolved* chain, while Seatbelt checks the path as the process wrote it. A
dotfiles symlink like ~/.config/sc -> ~/Tresorit/.../sc is therefore granted at
its target and denied at its own name, which reads as "my config.toml grant does
nothing" and cannot be fixed from inside a running session. These tests pin the
bridge: which literals it emits, that it stays read-only, and that it is appended
before the keychain deny so that deny still wins.

Run directly: ./test_sc_symlink_paths.py
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


def real(p) -> str:
    return os.path.realpath(p)


def test_a_real_dir_needs_no_bridge(sc, tmp: Path) -> None:
    """safehouse already grants the resolved chain, so a path that is its own
    realpath contributes nothing — the fragment must not grow for every dir."""
    (tmp / "plain").mkdir(exist_ok=True)
    plain = real(tmp / "plain")
    assert sc.symlinked_names([plain]) == []
    assert sc.symlink_read_literals([plain]) == []


def test_symlinked_dir_is_bridged_at_its_own_name(sc, tmp: Path) -> None:
    """The case from real use: ~/.config/sc -> a synced folder. The link path and
    every ancestor of it must be readable, or the kernel cannot follow it."""
    (tmp / "target").mkdir(exist_ok=True)
    (tmp / "cfg").mkdir(exist_ok=True)
    link = tmp / "cfg" / "sc"
    if not link.exists():
        link.symlink_to(tmp / "target")

    assert sc.symlinked_names([str(link)]) == [str(link)]
    literals = sc.symlink_read_literals([str(link)])
    assert str(link) in literals, literals
    assert str(link.parent) in literals, "the link's parent must be walkable"
    assert "/" in literals, literals
    # the target is safehouse's job — bridging it again would be a second grant
    assert real(tmp / "target") not in literals, literals


def test_a_symlinked_ancestor_is_bridged(sc, tmp: Path) -> None:
    """The symlink may sit above the granted dir, so the whole written chain is
    listed rather than just the leaf."""
    (tmp / "elsewhere/inner").mkdir(parents=True, exist_ok=True)
    link = tmp / "viaLink"
    if not link.exists():
        link.symlink_to(tmp / "elsewhere")
    written = str(link / "inner")
    assert sc.symlinked_names([written]) == [written]
    literals = sc.symlink_read_literals([written])
    assert written in literals and str(link) in literals, literals


def test_expansion_dedup_and_missing(sc, tmp: Path) -> None:
    (tmp / "target").mkdir(exist_ok=True)
    link = tmp / "expandme"
    if not link.exists():
        link.symlink_to(tmp / "target")
    os.environ["SC_TEST_LINK"] = str(link)
    # $VARS and ~ expand, like every other dir sc handles
    assert sc.symlinked_names(["$SC_TEST_LINK"]) == [str(link)]
    # the same link named twice is bridged once
    assert sc.symlinked_names([str(link), "$SC_TEST_LINK"]) == [str(link)]
    # a name that does not exist has nothing to bridge and must not abort
    assert sc.symlinked_names([str(tmp / "nope")]) == []
    # a dangling symlink likewise
    dangling = tmp / "dangling"
    if not dangling.exists(follow_symlinks=False):
        dangling.symlink_to(tmp / "nope")
    assert sc.symlinked_names([str(dangling)]) == []


def test_fragment_is_read_only_and_quotes_paths(sc) -> None:
    """A write allow here would widen the sandbox beyond the grant that was
    asked for: writing through the link lands on the target, which carries its
    own access level."""
    text = sc.symlink_paths_profile_text(["/Users/x/.config/sc", "/Users/x/.config"])
    assert "file-write" not in text, text
    assert text.count("(allow file-read*") == 1, text
    assert '(literal "/Users/x/.config/sc")' in text, text
    assert '(literal "/Users/x/.config")' in text, text
    # no subpath: a literal grants that one directory entry, not the tree
    assert "subpath" not in text, text


def test_no_fragment_when_nothing_is_symlinked(sc, tmp: Path) -> None:
    (tmp / "plain2").mkdir(exist_ok=True)
    assert sc.write_symlink_paths_profile([real(tmp / "plain2")]) is None


def test_fragment_written_when_something_is(sc, tmp: Path) -> None:
    (tmp / "t2").mkdir(exist_ok=True)
    link = tmp / "l2"
    if not link.exists():
        link.symlink_to(tmp / "t2")
    path = sc.write_symlink_paths_profile([str(link)])
    assert path is not None
    assert str(link) in Path(path).read_text()


def test_activated_groups_reports_members_as_written(sc, tmp: Path) -> None:
    """main() needs the unresolved members to build the bridge, so activation is
    exposed separately from the resolved grants."""
    (tmp / "g1").mkdir(exist_ok=True)
    (tmp / "g2").mkdir(exist_ok=True)
    config = {"group": {"g": {"rw": [str(tmp / "g1"), str(tmp / "g2")]}}}
    got = sc.activated_groups(config, real(tmp / "g1"))
    assert got == [("g", "rw", [str(tmp / "g1"), str(tmp / "g2")])], got
    assert sc.activated_groups(config, real(tmp)) == []


def _stub_safehouse(tmp_dir: Path, argv_file: Path) -> str:
    path = tmp_dir / "safehouse"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "Agent Safehouse 0.11.1"; exit 0; fi\n'
        f'for a in "$@"; do echo "$a" >> "{argv_file}"; done\n'
        "exit 99\n"
    )
    path.chmod(0o755)
    return str(path)


def test_e2e_symlinked_group_dir_is_bridged(tmp: Path, profile_dir: Path) -> None:
    """A real sc launch with a symlinked [group] member: the resolved target is
    granted rw AND the link path shows up in an appended read-only fragment,
    ordered before the keychain deny."""
    (tmp / "e2e/target").mkdir(parents=True, exist_ok=True)
    (tmp / "e2e/here").mkdir(parents=True, exist_ok=True)
    link = tmp / "e2e/link"
    if not link.exists():
        link.symlink_to(tmp / "e2e/target")

    (profile_dir / "config.toml").write_text(
        f'[group.e2e]\nrw = ["{tmp / "e2e/here"}", "{link}"]\n'
    )
    argv_file = tmp / "argv"
    argv_file.unlink(missing_ok=True)
    sc_tmp = tmp / "tmp"
    sc_tmp.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "SAFEHOUSE_BIN": _stub_safehouse(tmp, argv_file),
        "PROFILE_DIR": str(profile_dir),
        "TMPDIR": str(sc_tmp),
    }
    r = subprocess.run(
        [str(SC), "--no-profile", "--", "--version"],
        capture_output=True, text=True, env=env, cwd=str(tmp / "e2e/here"), timeout=120,
    )
    assert r.returncode == 99, f"sc did not reach exec: {r.returncode}\n{r.stderr}"
    argv = argv_file.read_text().splitlines()

    granted = next((a for a in argv if a.startswith("--add-dirs=")), "")
    assert real(tmp / "e2e/target") in granted, f"target not granted rw: {argv}"

    fragments = [a.removeprefix("--append-profile=") for a in argv if a.startswith("--append-profile=")]
    bridge = [f for f in fragments if "symlink" in f]
    assert bridge, f"no symlink fragment appended: {argv}"
    text = Path(bridge[0]).read_text()
    assert f'(literal "{link}")' in text, text
    assert "file-write" not in text, text

    # Seatbelt takes the last match, so the keychain deny must come after.
    keychain = [i for i, f in enumerate(fragments) if "keychain" in f]
    assert keychain and keychain[0] > fragments.index(bridge[0]), fragments


def main() -> None:
    with tempfile.TemporaryDirectory() as profile_dir, tempfile.TemporaryDirectory() as work:
        sc = load_sc(profile_dir)
        tmp = Path(work)
        test_a_real_dir_needs_no_bridge(sc, tmp)
        test_symlinked_dir_is_bridged_at_its_own_name(sc, tmp)
        test_a_symlinked_ancestor_is_bridged(sc, tmp)
        test_expansion_dedup_and_missing(sc, tmp)
        test_fragment_is_read_only_and_quotes_paths(sc)
        test_no_fragment_when_nothing_is_symlinked(sc, tmp)
        test_fragment_written_when_something_is(sc, tmp)
        test_activated_groups_reports_members_as_written(sc, tmp)
        test_e2e_symlinked_group_dir_is_bridged(tmp, Path(profile_dir))
    print("OK")


if __name__ == "__main__":
    main()
