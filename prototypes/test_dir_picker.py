#!/usr/bin/env -S uv --quiet run --python >=3.11 --script
# /// script
# requires-python = ">=3.11"
# ///
"""Non-interactive checks for the dir-picker prototype.

The interaction itself is judged by hand; these cover the listing logic and the
browse loop's key handling, driven by a scripted fzf stub on SC_FZF_BIN.

    ./prototypes/test_dir_picker.py
"""

import os
import subprocess
import sys
import tempfile
from importlib.util import module_from_spec, spec_from_loader
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
PICKER = HERE / "dir-picker.py"

# Each line is one fzf run: "<key>\t<selected>\t<selected>...".
STUB = """\
#!/usr/bin/env python3
import os, sys
script = os.environ["FZF_SCRIPT"]
step = int(os.environ.get("FZF_STEP_FILE") and open(os.environ["FZF_STEP_FILE"]).read() or 0)
sys.stdin.read()
lines = [l for l in open(script).read().split("\\n") if l]
key, *picked = lines[step].split("\\t")
open(os.environ["FZF_STEP_FILE"], "w").write(str(step + 1))
if "--expect" in " ".join(sys.argv):
    print(key)
for p in picked:
    print(p)
"""


def load_picker():
    loader = SourceFileLoader("dir_picker", str(PICKER))
    spec = spec_from_loader(loader.name, loader)
    mod = module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_subdirs_sorts_dotdirs_last(p, tmp):
    for name in ("zebra", ".hidden", "Alpha", "beta"):
        os.mkdir(os.path.join(tmp, name))
    open(os.path.join(tmp, "a-file"), "w").close()
    assert p.subdirs(tmp) == ["Alpha", "beta", "zebra", ".hidden"], p.subdirs(tmp)


def test_subdirs_of_unreadable_dir_is_empty(p, tmp):
    locked = os.path.join(tmp, "locked")
    os.mkdir(locked, mode=0o000)
    try:
        assert p.subdirs(locked) == []
    finally:
        os.chmod(locked, 0o755)


def test_browse_entries_has_self_parent_subdirs_and_jumps(p, tmp):
    os.mkdir(os.path.join(tmp, "kid"))
    rows = p.browse_entries(tmp)
    assert rows[:2] == ["./", "../"], rows
    assert "kid/" in rows, rows
    assert rows[-2:] == ["~", "/"], rows


def test_browse_entries_at_root_has_no_parent(p, tmp):
    assert "../" not in p.browse_entries("/")


def test_list_for_query_lists_the_partial_segments_parent(p, tmp):
    os.makedirs(os.path.join(tmp, "src", "monorepo"))
    os.makedirs(os.path.join(tmp, "src", "other"))
    rows = p.list_for_query(f"{tmp}/src/mono", tmp)
    assert rows == [f"{tmp}/src/monorepo", f"{tmp}/src/other"], rows


def test_list_for_query_trailing_slash_lists_that_dir(p, tmp):
    os.makedirs(os.path.join(tmp, "src", "monorepo"))
    assert p.list_for_query(f"{tmp}/src/", tmp) == [f"{tmp}/src/monorepo"]


def test_list_for_query_without_a_slash_stays_at_the_start_dir(p, tmp):
    """Typing a bare name filters where you are; it must not jump to /."""
    os.makedirs(os.path.join(tmp, "monorepo"))
    assert p.list_for_query("mono", tmp) == [f"{tmp}/monorepo"]
    assert p.list_for_query("", tmp) == [f"{tmp}/monorepo"]


def test_list_for_query_tolerates_nonsense(p, tmp):
    assert p.list_for_query(f"{tmp}/nope/deeper", tmp) == []
    assert p.list_for_query("x", f"{tmp}/gone") == []


def test_list_for_query_compresses_home(p, tmp):
    rows = p.list_for_query(os.path.expanduser("~/"), tmp)
    assert rows and all(r.startswith("~/") for r in rows), rows[:3]


def run_picker(tmp, steps, args):
    """Run the real prototype with a scripted fzf. Returns its stdout lines."""
    script = os.path.join(tmp, "fzf-script")
    Path(script).write_text("\n".join(steps) + "\n")
    stub = os.path.join(tmp, "fzf-stub")
    Path(stub).write_text(STUB)
    os.chmod(stub, 0o755)
    step_file = os.path.join(tmp, "fzf-step")
    Path(step_file).write_text("0")
    env = {**os.environ, "SC_FZF_BIN": stub, "FZF_SCRIPT": script,
           "FZF_STEP_FILE": step_file}
    proc = subprocess.run([sys.executable, str(PICKER), *args],
                          capture_output=True, text=True, env=env)
    return [l for l in proc.stdout.split("\n") if l], proc


def test_browse_descends_then_picks(tmp):
    root = os.path.join(tmp, "root")
    os.makedirs(os.path.join(root, "src", "monorepo"))
    out, proc = run_picker(tmp, ["ctrl-o\tsrc/", "\tmonorepo/"], [root])
    assert proc.returncode == 0, proc.stderr
    assert out == [os.path.realpath(os.path.join(root, "src", "monorepo"))], out


def test_browse_banks_marks_across_levels(tmp):
    root = os.path.join(tmp, "root")
    os.makedirs(os.path.join(root, "a", "deep"))
    os.makedirs(os.path.join(root, "b"))
    out, proc = run_picker(
        tmp,
        ["ctrl-a\ta/\tb/",     # mark two here, keep browsing
         "ctrl-o\ta/",         # descend into a
         "\tdeep/"],           # pick deep, finish
        [root],
    )
    assert proc.returncode == 0, proc.stderr
    assert out == [os.path.realpath(os.path.join(root, p))
                   for p in ("a", "b", "a/deep")], out


def test_browse_up_then_picks_the_dir_it_landed_in(tmp):
    root = os.path.join(tmp, "root")
    os.makedirs(os.path.join(root, "a", "deep"))
    out, proc = run_picker(tmp, ["ctrl-u\t", "\t./"], [os.path.join(root, "a", "deep")])
    assert proc.returncode == 0, proc.stderr
    assert out == [os.path.realpath(os.path.join(root, "a"))], out


def test_picking_nothing_exits_nonzero(tmp):
    root = os.path.join(tmp, "root")
    os.makedirs(root)
    out, proc = run_picker(tmp, ["\t"], [root])
    assert proc.returncode == 1, proc.stdout
    assert out == [], out
    assert "nothing selected" in proc.stderr


def main() -> int:
    p = load_picker()
    unit = [test_subdirs_sorts_dotdirs_last, test_subdirs_of_unreadable_dir_is_empty,
            test_browse_entries_has_self_parent_subdirs_and_jumps,
            test_browse_entries_at_root_has_no_parent,
            test_list_for_query_lists_the_partial_segments_parent,
            test_list_for_query_trailing_slash_lists_that_dir,
            test_list_for_query_without_a_slash_stays_at_the_start_dir,
            test_list_for_query_tolerates_nonsense,
            test_list_for_query_compresses_home]
    e2e = [test_browse_descends_then_picks, test_browse_banks_marks_across_levels,
           test_browse_up_then_picks_the_dir_it_landed_in,
           test_picking_nothing_exits_nonzero]
    for t in unit:
        with tempfile.TemporaryDirectory() as tmp:
            t(p, os.path.realpath(tmp))
    for t in e2e:
        with tempfile.TemporaryDirectory() as tmp:
            t(os.path.realpath(tmp))
    print(f"OK ({len(unit) + len(e2e)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
