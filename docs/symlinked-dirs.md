# Symlinked directories keep working by name

In short: `~/.config/sc` is a symlink. safehouse allowed only its target, so the
kernel could not read the link, and any path through `~/.config/sc` was denied —
while the same file under the Tresorit path worked. sc now adds a read-only
Seatbelt rule (the `sandbox-exec` policy language, appended to safehouse's
generated policy via `--append-profile`) for the link path and its parents. Both
names work; access at the target is unchanged.

The rest of this file is why, and what the rule may and may not do.

A granted directory is reachable inside the sandbox both by its real path and by
the symlink you wrote it as. Dotfiles setups depend on this: `~/.config/sc` is a
stow link into a synced folder, and it is the name every tool and every agent
actually uses.

```
$ cd ~/src/myproject && sc
Dirs: sc -> ~/Tresorit/.../sc/.config/sc, ~/src/sc (rw)
Dirs: symlinked paths kept usable by name: ~/.config/sc
```

## The problem

safehouse resolves every `--add-dirs` path to its realpath and emits its
"ancestor directory literals" for that *resolved* chain only. Seatbelt, though,
checks the path as the process wrote it. Verified against safehouse 0.11.1:

```
$ safehouse --add-dirs=/tmp/x/link --stdout
;; Generated ancestor directory literals for extra read/write path: /tmp/x/real
(allow file-read* (literal "/") (literal "/tmp") (literal "/tmp/x") (literal "/tmp/x/real"))
(allow file-read* file-write* (subpath "/tmp/x/real"))
```

Nothing covers `/tmp/x/link`. The kernel has to read that link to resolve
anything through it, so `ls /tmp/x/link` fails with `Operation not permitted`
while `ls /tmp/x/real` works. Passing the link path instead does not help —
safehouse collapses it to the same realpath.

The failure is badly misleading in practice. `config.toml` says the directory is
granted, the launch banner confirms it, and the sandbox still refuses the path
you use — which reads as "dir groups are broken" rather than "that name is a
symlink". And because the policy is frozen once the session starts, nothing
inside can widen it: the bridge has to be laid at launch or not at all.

## The bridge

For every directory sc is asked to grant, it compares the written path with its
realpath. When they differ, the written path and each of its ancestors go into an
appended `--append-profile` fragment:

```
;; sc: keep symlinked grants reachable by the path they were written with.
(allow file-read*
    (literal "/")
    (literal "/Users/me")
    (literal "/Users/me/.config")
    (literal "/Users/me/.config/sc"))
```

This mirrors safehouse's own trick for resolved ancestors, and safehouse
documents why it is `file-read*` rather than `file-read-metadata`: agents call
`readdir()` on ancestors of the working directory during startup, and a
metadata-only grant makes Claude Code blank `PATH`.

## Rules

**Read-only, always.** Writing *through* a link lands on the target, which
already carries whatever access level was asked for. A write allow here would
grant more than the `[group]` or `[dirs]` entry asked for.

**`literal`, never `subpath`.** A literal grants that one directory entry —
listing its immediate children — not recursive access to the tree under it. So
bridging `~/.config/sc` exposes the *names* in `~/.config`, and nothing else new.

**The whole written chain, not just the leaf.** The symlink can sit anywhere
along the path, so every prefix is listed. Prefixes that safehouse already
granted are harmless duplicates.

**Only paths that need it.** A directory that is its own realpath contributes
nothing, so the fragment stays empty on machines with no symlinked grants and no
`--append-profile` is passed at all.

**Appended before the keychain deny.** Seatbelt takes the last match, so an
appended allow overrides an earlier deny. The bridge is read-only and touches
only directory entries, but ordering it first keeps `keychain-deny.md`'s denies
final regardless. safehouse's own terminal denies are emitted after every
appended fragment, so they win too.

**Every source of grants is covered**: profile `[dirs]`, activated
`config.toml` `[group]` members, `-dr`/`-dw`, and sc's own defaults — including
`PROFILE_DIR` itself, which is the case that started this.

## Where this lives

- `sc`: `symlinked_names()`, `symlink_read_literals()`,
  `symlink_paths_profile_text()`, `write_symlink_paths_profile()`.
  `activated_groups()` was split out of `config_dirs_for()` so the group members
  are available as written, not only as realpaths, and `profile_named_dirs()`
  does the same for profile `[dirs]`.
- `test_sc_symlink_paths.py`: the rules above, plus an end-to-end launch against
  a stub safehouse asserting the fragment is appended, is read-only, and comes
  before the keychain deny.

## Verification limits

A sandbox cannot be nested — `sandbox-exec: sandbox_apply: Operation not
permitted` — so a session running inside safehouse cannot test the finished
policy against the kernel. What is checked instead: the fragment compiles as
Seatbelt (`sandbox-exec -f` reaches `sandbox_apply` rather than a syntax error),
safehouse embeds it after its generated policy and before its own terminal
denies, and the literals are the expected ones. The kernel-level check is the
next launch: `sc` then `ls ~/.config/sc`.
