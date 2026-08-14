# Directory groups

`~/.config/sc/config.toml` declares sets of directories that belong together.
Launching `sc` in any member of a group — or anywhere below one — mounts every
member of that group, at the group's one access level:

```toml
[group.repos]
ro = ["~/src"]                                     # every repo, read-only, always

[group.myproject]
rw = ["~/src/myproject", "~/scratch", "~/notes"]   # in any of these, get all of them
```

`ro` becomes `safehouse --add-dirs-ro`, `rw` becomes `--add-dirs` — the same
grants `-dr`/`-dw` produce, just declared instead of typed.

## Why declarative grants rather than a better flag

**The sandbox cannot be widened once a session is running.** `sc` execs
`safehouse`, which renders a deny-by-default Seatbelt policy and applies it with
`sandbox-exec -f`. That policy is frozen for the whole process tree and can only
ever be narrowed. Three plausible escapes were checked and none work:

- **A symlink into an already-granted dir.** safehouse resolves every
  `--add-dirs` path to its realpath, and the kernel matches the *resolved* path
  of the file being opened. `sc` already relies on this in the other direction —
  it mounts `PROFILE_DIR.resolve()` because granting the link path leaves the
  contents unreachable.
- **Re-entering a wider sandbox from inside.** Nested `sandbox_apply` is denied
  (`sandbox-exec: sandbox_apply: Operation not permitted`), as
  `temp-dir-unix-sockets.md` records.
- **Editing the appended `.sb` fragments mid-session.** safehouse emits a
  `deny file-write*` for every `--append-profile` path, and the policy was read
  at launch anyway.

So the cost of a forgotten `-dw` is a relaunch, which also throws away the
agent's session. A shortcut for that relaunch was considered and rejected: it
still restarts, and it only helps *after* you have already discovered the gap.
Declaring the dirs removes the discovery step — by the time you need the
directory, it is already granted.

## Why a group rather than a launch-dir → grants mapping

The relation between a project and its scratch dir, its notes, or a sibling repo
is **mutual**: whichever of them you are working in, you want the others. A
mapping keyed on the launch directory is one-directional — the key gets nothing
from its values, and launching inside a value grants nothing back. Expressing a
mutual pairing means writing it once per direction, and every added directory
multiplies the entries.

A group states the relation once and reads the way it is meant: *these
directories belong together.* It also removes the degenerate self-reference —
"all my repos, readable, whenever I work in any of them" is a one-member group
(`ro = ["~/src"]`), not a key naming `~/src` whose value repeats `~/src`.

The cost is that a group cannot express an asymmetry (this dir gets that one, but
not the reverse). Nothing wanted that, and the reverse grant is harmless: every
member is a directory you already work in.

## Why global rather than per-profile

Profile `[dirs]` already exists, but a profile is about *identity* — which GitHub
token, which env vars — while these grants are about *location*. The same
directory pairing (this repo needs that scratch dir) holds whichever profile is
active, and `-P` must not silently drop it. `config.toml` therefore applies to
every launch, and its grants are merged with the profile's rather than replacing
them. It lives in `PROFILE_DIR`, which is already mounted rw and syncs across
machines via the dotfiles symlink, so the mapping follows the machine set.

## Rules

**A member covers itself and everything below it.** Launching in
`~/src/myproject/docs` activates a group with `~/src/myproject` in it. Without
this, every launch from a subdirectory would silently get nothing, and one group
could not cover all repos.

**Activation grants the whole group**, each member at its own path — not at the
launch directory.

**One access level per group.** `ro` and `rw` in the same group aborts the
launch. "Enter from any member, get every member" has to mean one thing; a group
that mixed the two would grant different access depending on which side you came
from, which is exactly the directional behaviour groups exist to avoid. Two
access levels means two groups, which may overlap freely.

**Every activated group contributes.** A broad group and a narrow one both
apply and their grants union. Nothing overrides anything, so adding a narrow
group can never quietly remove a grant a broader one was providing.

**No chaining.** Groups `[A, B]` and `[B, C]` do not merge: launching in `A`
grants `A` and `B` only. Transitive closure would make the effective grant set of
a launch hard to predict from reading the file, and the whole point is that it is
obvious. (Launching in `B` activates both groups, so it does get all three.)

**Comparison and grants are on realpaths, after `~`/`$VAR` expansion.** A
symlinked launch directory matches a real-path member and vice versa, and the
path handed to safehouse is the resolved one. Seatbelt matches the resolved path
of the file being opened, so a link path would produce a grant that matches the
config but not the sandbox — the same lesson as `PROFILE_DIR.resolve()`.

**A missing directory is skipped with a warning, not fatal.** safehouse rejects a
non-existent `--add-dirs` path, so one stale line would otherwise block every
launch from anywhere in that group.

**A malformed `config.toml` aborts the launch.** The alternative — launching with
the grants silently absent — surfaces much later as an unrelated
`Operation not permitted` from whatever the agent was doing, which is the failure
mode this file exists to avoid.

## Rejected alternatives

**`safehouse --enable=wide-read`** grants read-only visibility across `/`, which
would end restarts for read access. It does nothing for write access, which is
the case that actually forces relaunches, and it exposes every credential file on
disk to a sandbox that has network access.

**safehouse's own `<workdir>/.safehouse`** file holds `add-dirs`/`add-dirs-ro`
and is the closest upstream equivalent. It is per-repo rather than global (so
grants cannot be expressed once for all repos), it is read only when the workdir
has been trusted, and a checked-in `.safehouse` in a cloned repo becomes a policy
input — a trust surface `config.toml` does not have, since it lives outside every
repo.

**`/add-dir` in Claude Code** does not enter into it. Verified: reading and
writing absolute paths outside claude's workspace roots works without it. It adds
a workspace root, which affects permission-prompt scoping (moot under `-y`), that
directory's `CLAUDE.md`, and default search scope — not file access. The Seatbelt
grant is the only gate.

## Where this lives

- `sc`: `CONFIG_FILE`, `load_config()`, `dir_covers()`, `config_dirs_for()`, and
  the `resolve_dirs()` helper shared with `profile_dirs()`. Wired into `main()`
  against the *invocation* cwd, before `-r`/`-t` change directory — "I am working
  here" means where `sc` was run.
- `test_sc_config_dirs.py`: the rules above, each pinned by a test, plus an
  end-to-end launch against a stub safehouse asserting that entering from either
  member reaches `--add-dirs=`.

`config.toml` is personal, machine-synced config. Agents must print a proposed
file rather than edit it (see `CLAUDE.md`).
