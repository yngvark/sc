# Profile matching on the launch directory

A profile declares the directories it applies in, and `sc` selects it there
without `-p`:

```toml
# ~/.config/sc/profiles/work.toml
[match]
dirs = ["~/src/work", "~/src/work-sandbox"]

[github]
token = "op://Vault/Work GitHub PAT/credential"
```

```
$ cd ~/src/work/some-repo && sc
Profile: work (matched ~/src/work)
```

## Why the profile decides, not a config table

Which profile a directory needs is a property of the profile: it is the same
fact as "this is the token for these repos". Keeping the list in the profile file
means the token, the extra dirs, the env vars and the locations all live in one
file, added and removed together — a profile is deleted by deleting its file, and
nothing is left pointing at it.

The alternative was a `[profile.<name>]` table in `config.toml` next to the
[dir groups](dir-groups.md). It reads well in one place, but it splits one
profile across two files and can name a profile that no longer exists. The dir
groups are in `config.toml` for the opposite reason: they are about *location*
and must apply under every profile, including `-P`, so they cannot live in any
one profile file.

Reusing the dir groups themselves — a `profile = "work"` key on
`[group.work]` — was rejected too. A group is deliberately symmetric: a shared
`~/scratch` member would then also select that profile, and a group's whole point
is that entering from any member is equivalent. Profile selection is the opposite
shape: one directory, one identity, most specific wins.

## Rules

**A `[match]` dir covers itself and everything below it.** Repos are worked in
from their subdirectories at least as often as from their root. This is
`dir_covers()`, shared with the dir groups.

**The deepest match wins.** Every member that covers the launch directory is an
ancestor of it, so they all lie on one chain and the longest path is exactly the
most specific one. A broad profile (`~/src`) can therefore be narrowed by a
specific one (`~/src/work`) without either profile mentioning the other.

**Two different profiles matching at the same depth aborts.** The profile decides
which GitHub token the sandbox gets, so there is no harmless default to fall back
on — silently picking one hands the agent credentials for the wrong account. The
error names both profiles and the directory. `-p <name>` or `-P` gets past it for
the launch at hand.

**Selection order: `-P`, `-p`, `[match]`, persisted.** The match beats
`active-profile` on purpose. That file holds whatever the last `-p` picked,
possibly in an unrelated project; letting it shadow the match would leave `-p`
exactly as mandatory as before. A matched profile is *not* persisted — it is
derived from where you are, so there is nothing to remember. `-P` is the way to
opt out for one launch, and it also overrides a match.

**Matching is against the invocation directory**, before `-r`/`-t` change
directory — same as the dir groups. Which project you are in is where you ran
`sc`.

**Comparison is on realpaths, after `~`/`$VAR` expansion**, so a symlinked launch
directory matches a real-path `[match]` dir and vice versa.

**A `[match]` dir that does not exist simply never matches.** Unlike `[dirs]`,
nothing is handed to safehouse, so there is nothing for a stale line to break and
no warning is printed.

**An unreadable profile file is skipped with a warning.** Every profile is parsed
to find the match, so a malformed file would otherwise block launches from
directories that have nothing to do with it. The profile actually selected is
still parsed strictly (`load_profile()`), which aborts.

## Where this lives

- `sc`: `profile_match_dirs()`, `read_profile_file()`, `match_profile_for()`, and
  `resolve_profile()`, which returns the name together with where it came from so
  the launch banner can say `matched ~/src/work` or `persisted`.
- `test_sc_profile_match.py`: the rules above, plus an end-to-end launch against
  a stub safehouse asserting that a bare `sc` in a matched directory mounts that
  profile's `[dirs]`.

Profile files are personal, machine-synced config. Agents must print a proposed
file rather than edit it (see `CLAUDE.md`).
