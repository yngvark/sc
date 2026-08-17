# Choosing claude's permission mode per launch (`-m`)

## What it does

`sc -m <mode>` forwards `--permission-mode <mode>` to claude, e.g.
`sc -m auto`, `sc -m plan`. It overrides `permissions.defaultMode` in
`~/.claude/settings.json` for that one launch, and nothing else about the
sandbox changes.

The value is optional: a bare `sc -m` means `sc -m auto`, since auto is the
mode wanted often enough that typing it every time is friction. `-m` followed
by another flag or `--` still means the bare form — the next token is only
taken as the mode when it does not start with `-`, the same rule `-p` uses.

## Why a mode flag and not a `--auto` flag

claude has several permission modes and the set grows over time. A dedicated
boolean per mode would mean a new `sc` flag every time claude adds one, so
`-m` takes the mode as data instead — the same reasoning that keeps extra
directories in `config.toml` rather than in branches in the script.

For the same reason `sc` does **not** validate the value. claude owns the
list of valid modes; a copy here would go stale and reject modes that work.
An unknown mode fails fast inside claude, which prints the valid choices.

## Relationship to `-y`

`-y` passes the agent's blanket bypass flag (`--dangerously-skip-permissions`
for claude). That is a permission mode too, so combining it with `-m` would
hand claude two conflicting instructions. `sc` rejects the combination rather
than silently picking one.

`-m` is claude-only: codex has no equivalent of `--permission-mode`, so
`sc --codex -m ...` is rejected instead of being dropped on the floor.

With `-s/--shell` no agent is launched, so neither `-y` nor `-m` contributes
anything — same as before.

## Where it lives in the code

`parse_args` collects the value; `permission_flags(yes, mode, yolo_flag)`
turns `-y`/`-m` into the argv prefix for the agent. Keeping that a pure
function is what makes the behaviour testable without launching a sandbox.
`passthrough` stays the last element of the `parse_args` tuple — tests index
it as `[-1]`.

## Note on the default

Without `-y` or `-m`, claude uses `permissions.defaultMode` from
`settings.json`. If the goal is "auto mode by default", that setting is the
place for it; `-m` is for deviating from it on a single launch.

## Verification

Unit tests: `./test_sc_permission_mode.py` (parsing, rejections, the flag
prefix). End-to-end, pointing `SAFEHOUSE_BIN` at a stub that echoes its
argv:

```
SAFEHOUSE_BIN=/path/to/stub sc -P -m auto -- -c
```

Expected: the launched command ends with `claude --permission-mode auto -c`.
