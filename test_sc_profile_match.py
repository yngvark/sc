#!/usr/bin/env -S uv --quiet run --python >=3.11 --script
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for [match] dirs in ~/.config/sc/profiles/<name>.toml.

A profile decides which GitHub token the sandbox gets, so picking the wrong one
is not a cosmetic slip: it hands the agent credentials for another account (or
none). The selection rules are therefore pinned here — deepest match wins, a
match beats the persisted profile but loses to -p/-P, and an ambiguous match
aborts rather than guessing.

Run directly: ./test_sc_profile_match.py
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
        out.append(os.path.realpath(root / n))
    return out


def write_profile(sc, name: str, body: str) -> None:
    sc.PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    (sc.PROFILES_DIR / f"{name}.toml").write_text(body)


def clear_profiles(sc) -> None:
    if sc.PROFILES_DIR.is_dir():
        for p in sc.PROFILES_DIR.glob("*.toml"):
            p.unlink()
    sc.PROFILE_FILE.unlink(missing_ok=True)


def match_dirs(*paths: str) -> str:
    return "[match]\ndirs = [" + ", ".join(f'"{p}"' for p in paths) + "]\n"


def test_matches_the_dir_and_below(sc, tmp: Path) -> None:
    """A [match] dir covers itself and every subdirectory — a repo is worked in
    from its subdirs at least as often as from its root."""
    clear_profiles(sc)
    work, = mkdirs(tmp, "work")
    deep, = mkdirs(tmp, "work/a/b")
    write_profile(sc, "work", match_dirs(work))
    assert sc.match_profile_for(work) == ("work", work)
    assert sc.match_profile_for(deep) == ("work", work)


def test_no_match_outside(sc, tmp: Path) -> None:
    clear_profiles(sc)
    work, other = mkdirs(tmp, "work", "other")
    write_profile(sc, "work", match_dirs(work))
    assert sc.match_profile_for(other) is None
    # a name prefix is not a path prefix
    other2, = mkdirs(tmp, "work-other")
    assert sc.match_profile_for(other2) is None
    # matching goes downwards only: the parent of a [match] dir gets nothing
    assert sc.match_profile_for(str(tmp)) is None


def test_several_dirs_per_profile(sc, tmp: Path) -> None:
    clear_profiles(sc)
    a, b = mkdirs(tmp, "one", "two")
    write_profile(sc, "work", match_dirs(a, b))
    assert sc.match_profile_for(a) == ("work", a)
    assert sc.match_profile_for(b) == ("work", b)


def test_deepest_match_wins(sc, tmp: Path) -> None:
    """A broad profile can be narrowed by a specific one without either knowing
    about the other."""
    clear_profiles(sc)
    src, = mkdirs(tmp, "src")
    inner, = mkdirs(tmp, "src/work")
    deep, = mkdirs(tmp, "src/work/repo/sub")
    write_profile(sc, "broad", match_dirs(src))
    write_profile(sc, "narrow", match_dirs(inner))
    assert sc.match_profile_for(src) == ("broad", src)
    assert sc.match_profile_for(inner) == ("narrow", inner)
    assert sc.match_profile_for(deep) == ("narrow", inner)


def test_same_profile_twice_is_not_ambiguous(sc, tmp: Path) -> None:
    """Overlapping dirs inside ONE profile pick the same token, so there is
    nothing to resolve."""
    clear_profiles(sc)
    src, = mkdirs(tmp, "src")
    inner, = mkdirs(tmp, "src/work")
    write_profile(sc, "work", match_dirs(src, inner))
    assert sc.match_profile_for(inner) == ("work", inner)


def test_ambiguous_match_aborts(sc, tmp: Path) -> None:
    """Two profiles claiming the same dir have no safe resolution — the choice
    decides which GitHub token the sandbox gets."""
    clear_profiles(sc)
    shared, = mkdirs(tmp, "shared")
    write_profile(sc, "a", match_dirs(shared))
    write_profile(sc, "b", match_dirs(shared))
    try:
        sc.match_profile_for(shared)
    except SystemExit as e:
        assert e.code == 1, e.code
    else:
        raise AssertionError("an ambiguous [match] did not abort")


def test_expansion_and_symlinks(sc, tmp: Path) -> None:
    """~ and $VARS expand and both sides resolve, like config.toml groups."""
    clear_profiles(sc)
    real, = mkdirs(tmp, "real")
    os.environ["SC_TEST_MATCH_DIR"] = real
    write_profile(sc, "work", match_dirs("$SC_TEST_MATCH_DIR"))
    assert sc.match_profile_for(real) == ("work", real)
    link = tmp / "link"
    if not link.exists():
        link.symlink_to(real)
    # a symlinked cwd matches its realpath [match] dir...
    assert sc.match_profile_for(str(link)) == ("work", real)
    # ...and a symlinked [match] dir matches the real cwd
    write_profile(sc, "work", match_dirs(str(link)))
    assert sc.match_profile_for(real) == ("work", real)


def test_missing_dir_and_no_match_section(sc, tmp: Path) -> None:
    """A stale [match] dir just never matches — no warning spam, no abort."""
    clear_profiles(sc)
    work, = mkdirs(tmp, "work")
    write_profile(sc, "gone", match_dirs(str(tmp / "does-not-exist")))
    write_profile(sc, "plain", '[github]\ntoken = "op://x/y/z"\n')
    assert sc.match_profile_for(work) is None


def test_unreadable_profile_is_skipped(sc, tmp: Path) -> None:
    """One malformed profile file must not block a launch that would have
    matched another profile."""
    clear_profiles(sc)
    work, = mkdirs(tmp, "work")
    write_profile(sc, "broken", '[match]\ndirs = ["unclosed\n')
    write_profile(sc, "work", match_dirs(work))
    assert sc.match_profile_for(work) == ("work", work)


def test_precedence(sc, tmp: Path) -> None:
    """-P and -p beat the match; the match beats the persisted profile."""
    clear_profiles(sc)
    work, elsewhere = mkdirs(tmp, "work", "elsewhere")
    write_profile(sc, "work", match_dirs(work))
    write_profile(sc, "other", '[github]\ntoken = "op://x/y/z"\n')
    sc.PROFILE_FILE.write_text("other\n")

    name, origin = sc.resolve_profile(False, "", False, work)
    assert name == "work", (name, origin)
    assert "matched" in origin, origin
    # the persisted choice still applies where nothing matches
    assert sc.resolve_profile(False, "", False, elsewhere) == ("other", "persisted")
    # -P wins over a match
    assert sc.resolve_profile(False, "", True, work)[0] is None
    # -p <name> wins over a match, and persists
    assert sc.resolve_profile(True, "other", False, work) == ("other", "-p")
    assert sc.PROFILE_FILE.read_text().strip() == "other"
    # no match, nothing persisted
    sc.PROFILE_FILE.unlink()
    assert sc.resolve_profile(False, "", False, elsewhere) == (None, "")


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


def test_e2e_matched_profile_dirs_reach_safehouse(tmp: Path, profile_dir: Path) -> None:
    """The real sc, launched with no flags in a matched dir, mounts that
    profile's [dirs] — i.e. the match reached the whole launch, not just the
    resolver."""
    work, extra = mkdirs(tmp, "e2e/work", "e2e/extra")
    profiles = profile_dir / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "e2e.toml").write_text(
        match_dirs(work) + f'\n[dirs]\nrw = ["{extra}"]\n'
    )
    (profile_dir / "active-profile").write_text("none\n")  # match must beat this

    argv_file = tmp / "argv"
    sc_tmp = tmp / "tmp"
    sc_tmp.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "SAFEHOUSE_BIN": _stub_safehouse(tmp, argv_file),
        "PROFILE_DIR": str(profile_dir),
        "TMPDIR": str(sc_tmp),
    }

    def launch(cwd: str, *args: str) -> tuple[list[str], str]:
        argv_file.unlink(missing_ok=True)
        r = subprocess.run(
            [str(SC), *args, "--", "--version"],
            capture_output=True, text=True, env=env, cwd=cwd, timeout=120,
        )
        assert r.returncode == 99, f"sc did not reach exec: {r.returncode}\n{r.stderr}"
        return argv_file.read_text().splitlines(), r.stderr

    argv, stderr = launch(work)
    assert "Profile: e2e" in stderr, stderr
    assert any(a.startswith("--add-dirs=") and extra in a for a in argv), argv

    # from a subdirectory of the matched dir
    sub, = mkdirs(tmp, "e2e/work/deep")
    argv, stderr = launch(sub)
    assert "Profile: e2e" in stderr, stderr

    # -P overrides the match for one launch
    argv, stderr = launch(work, "--no-profile")
    assert "Profile: (none)" in stderr, stderr
    assert not any(extra in a for a in argv), argv

    # outside the matched dirs the persisted "none" applies again
    other, = mkdirs(tmp, "e2e/other")
    argv, stderr = launch(other)
    assert "Profile: (none)" in stderr, stderr


def main() -> None:
    with tempfile.TemporaryDirectory() as profile_dir, tempfile.TemporaryDirectory() as work:
        sc = load_sc(profile_dir)
        tmp = Path(work)
        test_matches_the_dir_and_below(sc, tmp)
        test_no_match_outside(sc, tmp)
        test_several_dirs_per_profile(sc, tmp)
        test_deepest_match_wins(sc, tmp)
        test_same_profile_twice_is_not_ambiguous(sc, tmp)
        test_ambiguous_match_aborts(sc, tmp)
        test_expansion_and_symlinks(sc, tmp)
        test_missing_dir_and_no_match_section(sc, tmp)
        test_unreadable_profile_is_skipped(sc, tmp)
        test_precedence(sc, tmp)
        clear_profiles(sc)
        test_e2e_matched_profile_dirs_reach_safehouse(tmp, Path(profile_dir))
    print("OK")


if __name__ == "__main__":
    main()
