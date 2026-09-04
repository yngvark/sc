#!/usr/bin/env -S uv --quiet run --python >=3.11 --script
# /// script
# requires-python = ">=3.11"
# ///
"""Prototype of the directory picker behind a bare `sc -dw`.

Prints the chosen directories, one per line, and nothing else. Nothing is
mounted and nothing is launched — this exists so the interaction can be tried
before it goes into `sc`.

    ./prototypes/dir-picker.py                 # browse mode, starting here
    ./prototypes/dir-picker.py ~/src           # browse mode, starting there
    ./prototypes/dir-picker.py --mode=live     # type-a-path mode

Two interaction models are implemented so they can be compared:

browse  Python holds the current directory and each level is a fresh fzf run.
        Enter only moves: into a subdirectory, up on ../, over to ~ or /.
        ctrl-s takes the row under the cursor (or every Tab-marked row) and
        finishes; ctrl-a takes them and keeps you browsing, so directories in
        unrelated trees can go in one selection. ./ is the row that means the
        directory you are standing in.

live    One fzf whose list follows the path you type. Typing `~/src/mo` lists
        `~/src` and filters it; typing `/opt/` walks off anywhere. Tab marks,
        Enter finishes.
"""

import os
import shlex
import subprocess
import sys
from pathlib import Path

FZF_BIN = os.environ.get("SC_FZF_BIN", "fzf")

SELF = "./"
PARENT = "../"
JUMPS = ["~", "/"]

BROWSE_HEADER = (
    "enter: open   ctrl-s: take it, done   ctrl-a: take it, keep browsing"
    "\ntab: mark several   ./ is the directory you are in"
)
LIVE_HEADER = "type a path to go anywhere   tab: mark   enter: pick"


def die(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def compress_home(path: str) -> str:
    """Replace a leading $HOME with ~, the way sc displays paths."""
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def expand_home(path: str) -> str:
    return str(Path(os.path.expandvars(path)).expanduser())


def subdirs(current: str) -> list[str]:
    """Directory names one level under `current`, sorted, dotdirs last.

    Symlinks to directories count. An unreadable directory yields nothing
    rather than raising, so browsing into /private or a permission-denied tree
    just shows an empty level.
    """
    try:
        entries = list(os.scandir(current))
    except OSError:
        return []
    names = []
    for e in entries:
        try:
            if e.is_dir():
                names.append(e.name)
        except OSError:
            continue
    return sorted(names, key=lambda n: (n.startswith("."), n.lower()))


def browse_entries(current: str) -> list[str]:
    """One level's fzf menu: this dir, the parent, the subdirs, then the jumps."""
    rows = [SELF]
    if current != "/":
        rows.append(PARENT)
    rows += [n + "/" for n in subdirs(current)]
    rows += [j for j in JUMPS if os.path.realpath(expand_home(j)) != current]
    return rows


def list_for_query(query: str, home_base: str) -> list[str]:
    """Candidates for live mode: the directories one level under the query.

    The query is a path being typed, so its last segment is a partial name
    rather than a directory: `~/src/mo` lists `~/src`, and fzf then filters
    that listing down with `mo`. A query ending in `/` lists that directory
    itself. A query with no `/` at all is not a path yet, so the listing stays
    at `home_base` — the directory the picker started in.
    """
    query = query.strip()
    if "/" not in query:
        base = home_base
    else:
        expanded = expand_home(query)
        base = expanded if query.endswith("/") else os.path.dirname(expanded) or "/"
    if not os.path.isdir(base):
        return []
    prefix = compress_home(base).rstrip("/")
    return [f"{prefix}/{n}" for n in subdirs(base)]


def run_fzf(rows: list[str], prompt: str, header: str, expect: list[str]) -> tuple[str, list[str]]:
    """Returns (key pressed, selected lines). Key is "" for plain Enter."""
    args = [
        FZF_BIN,
        "--multi",
        "--height=60%",
        "--reverse",
        "--info=inline",
        f"--prompt={prompt}",
        f"--header={header}",
    ]
    if expect:
        args.append("--expect=" + ",".join(expect))
    proc = subprocess.run(args, input="\n".join(rows), text=True, capture_output=True)
    if proc.returncode not in (0, 1):        # 130 = Esc / ctrl-c
        return "abort", []
    out = proc.stdout.split("\n")
    if not expect:
        return "", [line for line in out if line]
    key = out[0] if out else ""
    return key, [line for line in out[1:] if line]


def run_fzf_live(start: str) -> list[str]:
    """One fzf whose candidate list reloads from whatever path is typed."""
    # Runs on every keystroke, so it calls the interpreter directly rather than
    # re-entering through the uv shebang.
    lister = (f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}"
              f" --list {shlex.quote(start)} {{q}}")
    args = [
        FZF_BIN,
        "--multi",
        "--height=60%",
        "--reverse",
        "--info=inline",
        "--prompt=dw> ",
        f"--header={LIVE_HEADER}",
        f"--bind=change:reload({lister})",
    ]
    initial = list_for_query("", start)
    proc = subprocess.run(args, input="\n".join(initial), text=True, capture_output=True)
    if proc.returncode not in (0, 1):
        return []
    return [os.path.realpath(expand_home(l)) for l in proc.stdout.split("\n") if l]


def browse(start: str) -> list[str]:
    """The file-manager loop. Returns the accumulated realpaths."""
    current = os.path.realpath(start)
    picked: list[str] = []

    def add(rows: list[str]) -> None:
        for row in rows:
            target = resolve_row(current, row)
            if target not in picked:
                picked.append(target)

    while True:
        count = f" ({len(picked)} selected)" if picked else ""
        prompt = f"{compress_home(current)}{count}> "
        key, rows = run_fzf(
            browse_entries(current), prompt, BROWSE_HEADER, ["ctrl-a", "ctrl-s"],
        )
        if key == "abort":
            return []
        # ../ is a way to move, never something to mount, so it is dropped from
        # anything that selects. Mounting the parent means going up and taking
        # ./ there.
        if key == "ctrl-a":
            add([r for r in rows if r != PARENT])
            continue
        if key == "ctrl-s":
            add([r for r in rows if r != PARENT])
            return picked
        if rows:                      # plain enter: open the row under the cursor
            current = resolve_row(current, rows[0])


def resolve_row(current: str, row: str) -> str:
    if row == SELF:
        return current
    if row == PARENT:
        return os.path.dirname(current) or "/"
    if row in JUMPS:
        return os.path.realpath(expand_home(row))
    return os.path.realpath(os.path.join(current, row.rstrip("/")))


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--list":                 # fzf reload hook, live mode
        base = argv[1] if len(argv) > 1 else os.getcwd()
        for row in list_for_query(argv[2] if len(argv) > 2 else "", base):
            print(row)
        return 0

    mode = "browse"
    start = os.getcwd()
    for a in argv:
        if a.startswith("--mode="):
            mode = a.split("=", 1)[1]
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        elif a.startswith("-"):
            die(f"unknown argument: {a}")
        else:
            start = expand_home(a)

    if mode not in ("browse", "live"):
        die(f"unknown mode: {mode} (browse, live)")
    if not os.path.isdir(start):
        die(f"not a directory: {start}")
    if not sys.stdin.isatty() and "SC_FZF_BIN" not in os.environ:
        die("this is an interactive picker; run it from a terminal")

    picked = browse(start) if mode == "browse" else run_fzf_live(start)
    if not picked:
        print("nothing selected", file=sys.stderr)
        return 1
    for p in picked:
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
