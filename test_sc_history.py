#!/usr/bin/env -S uv --quiet run --python >=3.11 --script
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for the sc command-history / resume-picker feature.

Loads the `sc` script as a module with PROFILE_DIR pointed at a temp dir, so
the pure history functions can be exercised without launching anything.
Run directly: ./test_sc_history.py
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


def test_roundtrip(sc, tmp):
    d = tmp / "proj"
    d.mkdir()
    os.chdir(d)
    sc.record_launch(["-dr", "/tmp/bar", "--", "-c"])
    hist = sc.load_history()
    assert len(hist) == 1, hist
    assert hist[0]["args"] == ["-dr", "/tmp/bar", "--", "-c"], hist
    # cwd under $HOME would be compressed; temp dir is not, so stored verbatim
    assert hist[0]["dir"] == os.getcwd(), hist


def test_dedup_moves_to_front(sc, tmp):
    sc.HISTORY_FILE.unlink(missing_ok=True)
    a, b = tmp / "a", tmp / "b"
    a.mkdir(); b.mkdir()
    os.chdir(a); cwd_a = os.getcwd(); sc.record_launch(["-dr", "x"])
    os.chdir(b); cwd_b = os.getcwd(); sc.record_launch(["-dr", "y"])
    os.chdir(a); sc.record_launch(["-dr", "x"])  # re-run first combo
    hist = sc.load_history()
    assert len(hist) == 2, hist                       # deduped, not 3
    assert hist[0]["dir"] == cwd_a and hist[0]["args"] == ["-dr", "x"], hist
    assert hist[1]["dir"] == cwd_b, hist


def test_distinct_newest_first(sc, tmp):
    sc.HISTORY_FILE.unlink(missing_ok=True)
    os.chdir(tmp)
    sc.record_launch(["-a"])
    sc.record_launch(["-y"])
    hist = sc.load_history()
    assert [e["args"] for e in hist] == [["-y"], ["-a"]], hist


def test_truncation(sc, tmp):
    sc.HISTORY_FILE.unlink(missing_ok=True)
    os.chdir(tmp)
    for n in range(sc.HISTORY_MAX + 25):
        sc.record_launch(["-dr", f"d{n}"])
    hist = sc.load_history()
    assert len(hist) == sc.HISTORY_MAX, len(hist)
    assert hist[0]["args"] == ["-dr", f"d{sc.HISTORY_MAX + 24}"], hist[0]


def test_home_compression_and_display(sc):
    home = os.path.expanduser("~")
    compressed = sc.compress_home(os.path.join(home, "src", "proj"))
    assert compressed == "~/src/proj", compressed
    assert sc.expand_home(compressed) == os.path.join(home, "src", "proj")
    assert sc.compress_home("/tmp/outside") == "/tmp/outside"
    assert sc.history_display({"dir": "~/x", "args": []}).endswith("(no flags)")
    assert "-dr /tmp/bar" in sc.history_display({"dir": "~/x", "args": ["-dr", "/tmp/bar"]})


def main() -> None:
    with tempfile.TemporaryDirectory() as profile_dir, tempfile.TemporaryDirectory() as work:
        sc = load_sc(profile_dir)
        tmp = Path(work)
        test_roundtrip(sc, tmp)
        test_dedup_moves_to_front(sc, tmp)
        test_distinct_newest_first(sc, tmp)
        test_truncation(sc, tmp)
        test_home_compression_and_display(sc)
    print("OK")


if __name__ == "__main__":
    main()
