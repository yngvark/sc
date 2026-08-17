# sc — safe-claude

Thin wrapper around [agent-safehouse](https://github.com/eugene1g/agent-safehouse)
that adds 1Password-backed GitHub token injection and runs `claude` (or, with
`--codex`, `codex`) inside the sandbox.

## Why this exists

The sandboxed agent should act on GitHub with a deliberately restricted
credential. This wrapper resolves a GitHub PAT from 1Password outside the
sandbox, caches it in macOS Keychain with a TTL, then passes only
`GITHUB_TOKEN` through to the sandboxed process via
`safehouse --env-pass=GITHUB_TOKEN`.

For that restriction to mean anything, the sandbox must not offer a stronger
credential as fallback: safehouse's claude agent profile auto-allows macOS
Keychain IPC (Claude Code may store its own login there), which would let
`gh` fall back to the keyring OAuth token when `GITHUB_TOKEN` is unset. sc
therefore appends a policy fragment denying Keychain access, making the
profile's PAT the only GitHub credential inside the sandbox. `--keychain`
restores access if a tool genuinely needs it. See
`docs/keychain-deny.md`.

If you don't need a GitHub token, call `safehouse claude` directly. The
wrapper is only useful for the `-p` (profile) flag.

## Requirements

safehouse 0.11.1 or newer. sc checks this on every launch and refuses to
start on older versions, because they block the ssh-agent socket on recent
macOS and the resulting failure looks like a git passphrase problem rather
than a sandbox one. See `docs/safehouse-version-gate.md`.

## Enabled sandbox features

Every launch passes a fixed set of `safehouse --enable=` features
(`SAFEHOUSE_FEATURES` in `sc`): `ssh` for git commit signing, and
`playwright-chrome` and `docker` because verifying a change in the sandbox
otherwise fails as an unexplained "operation not permitted". See
`docs/safehouse-features.md`.

## AWS readiness gate

When `AWS_PROFILE` is set, `sc -a` refuses to launch unless its credentials
actually resolve, checked on the host with `aws configure export-credentials`.
An expired SSO login otherwise surfaces much later as an opaque credentials
error from whatever the agent was doing inside the sandbox. An unset
`AWS_PROFILE` only gets a note. See `docs/aws-readiness-gate.md`.

## Layout

```
~/.config/sc/
├── safe-claude.py       → dotfiles symlink (uv-run Python wrapper)
├── config.toml          → directory groups to mount ([group] sections)
├── profiles/            → *.toml profiles (GitHub token + extra mount dirs)
├── active-profile       (state) persisted profile choice from fzf
├── history.jsonl        (state) recorded launches for `sc -H`
└── old/                 archived hand-rolled agent.sb + run-sandboxed.sh
```

`~/.config/sc` is a dotfiles symlink into Tresorit, so `config.toml`, profiles,
the persisted profile choice, and history sync across machines.

## Shell entry points

Defined in `~/.zshrc`:

| Alias / function | Calls                                       |
| ---------------- | ------------------------------------------- |
| `sc`             | `safe-claude.py "$@"`                       |
| `safe-claude`    | `safe-claude.py "$@"`                       |
| `safe`           | `safehouse "$@"` (generic launcher)         |

## Usage

```
sc                            # claude in sandbox, use persisted profile (if any)
sc -p                         # fzf-pick a profile, persist
sc -p <name>                  # use the named profile, persist
sc -P                         # explicitly use no profile (no token, no dirs)
sc -a                         # mount ~/.aws (rw) and pass AWS_PROFILE (checks login first)
sc -k                         # allow macOS Keychain access (denied by default)
sc -y                         # pass the agent's "skip all prompts" flag
sc -m auto                    # claude --permission-mode auto (also: plan, acceptEdits, ...)
sc --codex                    # run codex instead of claude (mounts ~/.codex rw)
sc --codex -y                 # codex with --dangerously-bypass-approvals-and-sandbox
sc -t                         # fresh mktemp -d, cd into it, mount it rw, run there
sc -dr  /repos/refs           # safehouse --add-dirs-ro=/repos/refs
sc -dw  /tmp/scratch          # safehouse --add-dirs=/tmp/scratch
sc -dr A -dr B                # repeatable; safehouse joins with ':'
                              # (recurring dirs belong in config.toml [group], below)
sc -H                         # fzf-pick a previous launch, re-run it there
sc --history                  # same as -H
sc --warm-token               # resolve/cache the profile token, then exit
sc -- -p "explain this code"  # everything after -- goes to the agent
sc -- --help                  # the agent's own help
sc -h                         # wrapper help
```

## Command history / resume

Every launch records its working directory and full argument list to
`~/.config/sc/history.jsonl` (deduplicated, most-recent first, capped at
100 entries). Because `~/.config/sc` is a dotfiles symlink into Tresorit,
the history syncs across machines.

`sc -H` (or `--history`) opens an fzf picker over that history, showing
`<dir>` + the `sc` args used. Selecting an entry `cd`s into that directory
and re-runs the exact `sc` command (including anything after `--`). Useful
for resuming a long `-dr ...` invocation later, on this or another machine,
without remembering the flags or which directory it ran in.

For sandbox scope not covered by the wrapper, use `safe` (or `safehouse`)
directly:

```
safe --enable=kubectl kubectl get pods
safe --add-dirs=~/src/monorepo -- claude --dangerously-skip-permissions
safe --workdir=/tmp/scratch -- bash
```

See `safehouse --help` for the full set of flags
(`--enable=...`, `--workdir`, `--trust-workdir-config`, `--append-profile`, …).

## Directory groups (`config.toml`)

The sandbox policy is fixed when a session starts and can never be widened, so a
directory you forgot to pass with `-dw` costs a full relaunch. `config.toml`
removes the flag from the loop: declare once which directories belong together,
and working in any of them reaches all of them.

`~/.config/sc/config.toml`:

```toml
[group.repos]
ro = ["~/src"]                                     # every repo, read-only, always

[group.myproject]
rw = ["~/src/myproject", "~/scratch", "~/notes"]   # in any of these, get all of them
```

```
$ cd ~/src/myproject/docs && sc
Dirs: myproject -> ~/src/myproject, ~/scratch, ~/notes (rw)
Dirs: repos -> ~/src (ro)
```

A group activates when `sc` is launched in any member or anywhere below one, and
then mounts every member — the relation is symmetric, so it is stated once rather
than once per direction. A group has a single access level (`ro` *or* `rw`,
meaning the same as `-dr`/`-dw`); every activated group contributes and grants
union. Groups sharing a member do not chain. `~` and `$VARS` expand, symlinks
resolve, and missing dirs are skipped with a warning. Unlike profile `[dirs]`,
this file applies to every launch regardless of profile, including `-P`. See
`docs/dir-groups.md`.

## Profile files

Each file under `profiles/` defines one profile. A profile bundles a GitHub
token (from 1Password) and a list of extra directories to auto-mount.

`profiles/<name>.toml`:

```toml
[github]
token = "op://Vault/Item/field"            # 1Password secret reference
op_account = "my-team.1password.eu"           # optional, default account if omitted

[dirs]
ro = ["~/some/read-only/path"]              # mounted via --add-dirs-ro
rw = ["~/some/read-write/path", "$OBSIDIAN_NOTES_DIR"]  # via --add-dirs; ~ and $VARS expand

[env]
pass = ["OBSIDIAN_NOTES_DIR"]               # forwarded via --env-pass into the sandbox
```

All sections are optional. In `[dirs]`, both `~` and `$VARS` are expanded, so
env-var-defined paths (e.g. `$OBSIDIAN_NOTES_DIR`) can be mounted; missing dirs
are skipped with a warning. `[env].pass` lists env var names to forward into the
sandbox via `safehouse --env-pass` — use it alongside a `[dirs]` entry when a
tool inside the sandbox needs both the directory and the var pointing at it.

Resolution order on `-p <name>` / persisted profile:

1. Look up `claude-docker-github-token` / account `<profile>` in macOS
   Keychain. If present and fresher than `GITHUB_TOKEN_CACHE_TTL`, use it.
2. Otherwise run `op read <ref> [--account <op_account>]`, cache result.

`-P` skips everything. No `-p` and no persisted profile = no token.

Cache key (`claude-docker-github-token`) is shared with the retired
`claude-docker` tool on purpose — warm cache survived the migration.

## Environment

| Variable                   | Default                            | Meaning                                          |
| -------------------------- | ---------------------------------- | ------------------------------------------------ |
| `GITHUB_TOKEN_CACHE_TTL`   | `36000` (10 h)                     | Keychain cache TTL in seconds. `0` disables.     |
| `PROFILE_DIR`              | `~/.config/sc`                     | Config dir.                                      |
| `SAFE_CLAUDE_PROFILES_DIR` | `$PROFILE_DIR/profiles`            | Override profiles dir.                           |
| `SAFEHOUSE_BIN`            | `safehouse` on `PATH`              | Override safehouse binary.                       |

## History

This started as a hand-rolled `sandbox-exec` profile (`agent.sb` +
`run-sandboxed.sh`) modeled on safehouse's policy output. Once it was
clear safehouse covers everything the custom profile did, the
hand-rolled bits were archived under `old/` and the wrapper switched to
calling `safehouse` directly. The only remaining wrapper logic is the
1Password/Keychain dance that safehouse intentionally doesn't do.
