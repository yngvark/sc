# Per-directory mount grants

`~/.config/sc/config.toml` declares which extra directories a launch gets, keyed
on the directory `sc` was launched from:

```toml
[when."~/src"]
ro = ["~/src"]                       # every repo, read-only, always

[when."~/src/myproject"]
rw = ["~/scratch", "~/notes"]        # only when working in myproject
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

## Why global rather than per-profile

Profile `[dirs]` already exists, but a profile is about *identity* — which GitHub
token, which env vars — while these grants are about *location*. The same
directory pairing (this repo needs that scratch dir) holds whichever profile is
active, and `-P` must not silently drop it. `config.toml` therefore applies to
every launch, and its grants are merged with the profile's rather than replacing
them. It lives in `PROFILE_DIR`, which is already mounted rw and syncs across
machines via the dotfiles symlink, so the mapping follows the machine set.

## Matching rules

**A key covers itself and everything below it.** Launching in
`~/src/myproject/docs` matches a key of `~/src/myproject`. Without this,
every launch from a subdirectory would silently get nothing, and there would be
no way to write one rule covering all repos.

**Every matching key contributes.** A broad key and a narrow key both apply and
their grants union. Nothing overrides anything, so adding a narrow entry can
never quietly remove a grant a broader entry was providing.

**Comparison is on realpaths, after `~`/`$VAR` expansion**, on both sides — a
symlinked working directory matches a real-path key and vice versa. This mirrors
what safehouse does to the paths themselves; comparing link paths would produce
grants that match the config but not the sandbox.

**No recursion.** If a granted directory has its own `[when]` entry, that entry
is not pulled in. Only the launch directory is matched. Recursion would make the
effective grant set of a launch hard to predict from reading the file, and the
whole point is that it is obvious.

**A missing directory is skipped with a warning, not fatal.** safehouse rejects a
non-existent `--add-dirs` path, so one stale line would otherwise block every
launch from that directory.

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
  against the *invocation* cwd, before `-r`/`-t` change directory — "when I am
  working here" means where `sc` was run.
- `test_sc_config_dirs.py`: the matching rules above, each pinned by a test,
  plus an end-to-end launch against a stub safehouse asserting the mapped path
  reaches `--add-dirs=`.

`config.toml` is personal, machine-synced config. Agents must print a proposed
file rather than edit it (see `CLAUDE.md`).
