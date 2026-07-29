# Trust check for `-t` temp dirs

## Symptom

Every `sc -t` launch showed Claude Code's folder-trust prompt ("Is this a
project you created or one you trust?") before anything could happen. `-y`
(`--dangerously-skip-permissions`) does not suppress it — the trust dialog is
separate from permission modes.

## Cause

Claude Code persists trust per directory in `~/.claude.json` under
`projects.<realpath>.hasTrustDialogAccepted`. `-t` used to create a fresh
`mktemp -d` dir in a random location on every launch, so no trust record ever
covered it and the prompt always reappeared.

## Design

Two facts, verified against the trust gate in the claude binary (2.1.220):

- **Trust inherits downward.** When deciding whether to prompt, claude walks
  up from cwd and accepts if the dir *or any ancestor* has
  `hasTrustDialogAccepted: true`.
- **Records are keyed by normalized realpath**, which matches Python's
  `os.path.realpath` (relevant on macOS where `$TMPDIR` is a symlinked
  `/var/folders/...` path resolving under `/private`).

So `sc -t` now creates temp dirs under one predefined parent, `$TMPDIR/sc`
(`TEMP_PARENT` in `sc`), and before a claude launch checks — read-only — that
this parent is trusted (`is_claude_trusted()`, which mirrors claude's
ancestor walk). If it is not, sc stops and prints the one-time setup:

    cd $TMPDIR/sc && claude

Accepting the folder-trust prompt there records the parent in
`~/.claude.json`, after which every fresh temp dir inside it is trusted.

Decisions:

- **Check, don't write.** An earlier iteration pre-seeded the trust record
  into `~/.claude.json` before launch (see git history). Reverted: sc should
  not mutate claude's state file. The trust decision stays with claude's own
  dialog; sc only reads the result.
- **Fail closed.** A missing/unreadable/corrupt `~/.claude.json` counts as
  untrusted and stops the launch with instructions, rather than launching
  into the prompt.
- **`$TMPDIR/sc` as the parent**: per-user private darwin temp, cleaned by
  the OS after a few days of non-use, and a stable path per machine so the
  one-time trust sticks. (Trust is per machine — `~/.claude.json` is not
  synced.)
- Only claude launches are checked (not `--codex`, not `-s` shell mode —
  neither has the dialog), but all `-t` temp dirs live under the same parent.
- `CLAUDE_CODE_SANDBOXED=1` was considered and rejected: the claude binary
  treats it as "already trusted" (it is set inside claude's own managed
  sandboxes), but it is undocumented and its meaning could change.

## Verification

`test_sc_temp.py` covers: temp dirs created under `TEMP_PARENT` (parent
auto-created), exact-dir trust, ancestor trust (the case the design depends
on), untrusted and `accepted: false` records, missing/corrupt state file
fail-closed, and symlink→realpath resolution.
