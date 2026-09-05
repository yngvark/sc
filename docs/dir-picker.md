# Browsing for directories to mount

`sc -dw` and `sc -dr` without a path open an fzf directory browser. What you
take there becomes `--add-dirs=` / `--add-dirs-ro=` for the launch.

```
$ cd ~/src/myproject && sc -dw
rw ~/src> ▏
  enter: open   ctrl-s: take it, done   ctrl-a: take it, keep browsing
  tab: mark several   ../ goes up and lands on the directory you left
  ../
> myproject/
  otherproject/
  ~
  /
```

The browser opens on the parent of the directory sc was launched in, with the
cursor already on that directory, so `ctrl-s` alone mounts where you are. That
is both the likeliest thing to want and one Enter away from its siblings, which
are the next likeliest.

## Why a picker rather than a better path syntax

A safehouse session's Seatbelt policy is fixed at exec and can never be
widened, so a directory you failed to name costs a full relaunch — the same
pressure that motivates `[group]` in `config.toml` (`dir-groups.md`). Groups
answer the recurring case. The picker answers the other one: a directory you
need once, whose path you would have to recall and type correctly before the
sandbox starts.

Globs were the obvious alternative and are worse for that job. `-dw 'proj*'`
requires knowing the naming pattern in advance, quoting it so the shell does
not expand it first, and it silently grants whatever else happens to match.
Browsing shows you the directories before you commit to them.

## The keys

Enter only moves: into a subdirectory, up on `../`, over to `~` or `/`. It
never ends the picker, so walking around costs no thought about what a
keystroke might commit you to.

`ctrl-s` takes the row under the cursor — or every Tab-marked row — and starts
the launch. `ctrl-a` takes the same rows but leaves the picker open, which is
how directories in unrelated trees end up in one selection. Esc cancels and
stops the launch, discarding anything already taken.

Nothing in the list stands for the directory you are standing in. Instead,
`../` moves up and leaves the cursor on the name you just left, so taking it is
Enter then `ctrl-s`. A `./` row would otherwise repeat on every level for a
case that this handles in one extra keystroke — and opening on the parent means
the common case needs no keystroke at all.

`../` is a way to move and is dropped from anything that selects, including a
Tab mark. Mounting it by accident would grant the whole tree above the
directory you were aiming at.

The cursor move binds to fzf's `load` event rather than `start`. At `start`
fzf has not read stdin yet, so there is no list to position within and `pos()`
is silently ignored.

## Where the picked paths end up

The browser returns realpaths, which is what safehouse grants — a symlink
mounted under its link name leaves the contents unreachable inside the sandbox
(`symlinked-dirs.md`).

The picker runs after every cheap check has passed (unknown flags, safehouse
version, AWS readiness, `-e` bundle names) and before anything with a side
effect, so a mistake elsewhere in the command line stops the launch without
making you browse first.

The launch recorded in `history.jsonl` names the directories you picked rather
than the bare `-dw`, so `sc -H` reproduces the session instead of reopening the
picker. Paths are recorded home-relative, since history syncs to machines where
`$HOME` differs.
