# Auto-trust of `-t` temp dirs

## Symptom

Every `sc -t` launch showed Claude Code's folder-trust prompt ("Is this a
project you created or one you trust?") before anything could happen. `-y`
(`--dangerously-skip-permissions`) does not suppress it — the trust dialog is
separate from permission modes.

## Cause

Claude Code persists trust per directory in `~/.claude.json` under
`projects.<realpath>.hasTrustDialogAccepted`. `-t` creates a fresh
`mktemp -d` dir on every launch, so no trust record ever exists and the
prompt always reappears. There is no supported way to skip the dialog — no
CLI flag, env var, or settings.json option covers it (checked against the
CLI reference, permission-modes, and headless docs as of 2026-07); seeding
the state file is the only mechanism.

## Fix

`seed_claude_trust()` in `sc` pre-writes the trust record before exec'ing
safehouse: it loads `~/.claude.json`, sets
`projects[realpath(temp_dir)].hasTrustDialogAccepted = true`, and writes the
file back (2-space JSON, matching Claude Code's own formatting). Keying by
`os.path.realpath` matters: `mktemp -d` returns `/var/folders/...` but Claude
Code records the resolved `/private/var/folders/...` path.

Scope is deliberately narrow:

- Only for `-t` temp dirs — they are empty and created by `sc` itself, so
  trusting them is always safe. Ordinary launches in real directories still
  get the prompt; auto-trusting arbitrary dirs would defeat its purpose.
- Only for claude (not `--codex`, not `-s` shell mode).
- Fail-open: an unreadable/corrupt `~/.claude.json` is left untouched and
  the launch proceeds (the user just sees the prompt as before).

The write happens outside the sandbox before `execvp`, so no sandbox policy
change is needed; Claude Code reads the already-updated file at startup.

## Verification

`test_sc_temp.py` covers: new state file creation, preservation of existing
keys/projects, realpath resolution via a symlink, and the corrupt-file
fail-open path.
