# Minimum safehouse version gate

## What it does

`sc` refuses to launch when the resolved `safehouse` binary is older than
`MIN_SAFEHOUSE_VERSION` (currently `0.11.1`), printing the version it found,
the floor, and the upgrade command:

```
safe-claude: safehouse 0.11.0 is too old — sc needs 0.11.1 or newer.
  Older versions block the ssh-agent socket on macOS Tahoe 26.4+, so git
  commit signing inside the sandbox fails with an unanswerable passphrase
  prompt. Upgrade with `brew upgrade agent-safehouse` or `safehouse update`.
```

The check runs immediately after the binary is resolved, before any sandbox
profile is generated, so a blocked launch has no side effects.

## Why there is a floor at all

`sc` is a thin wrapper that delegates the entire sandbox policy to safehouse,
so it inherits safehouse's coverage gaps. One of those gaps is expensive
enough to warrant a hard floor.

macOS Tahoe 26.4 moved the launchd-managed socket that `SSH_AUTH_SOCK` points
at from `/private/tmp/com.apple.launchd.*/Listeners` to `/var/run/...`.
safehouse's `ssh` integration allowed only the old locations, so inside the
sandbox nothing could reach ssh-agent. Git then falls back from agent-based
signing to reading the passphrase-protected key file directly and prompts for
a passphrase that a non-interactive agent cannot answer:

```
error: Enter passphrase for ".../id_ed25519_github_signing_key":
       incorrect passphrase supplied to decrypt private key?
fatal: failed to write commit object
```

The error names a passphrase, not the sandbox, and appears only for people who
sign commits — so the cause is several steps removed from the symptom. That
distance is the argument for failing loudly at startup instead of letting the
launch proceed. safehouse 0.11.1 allows both socket locations
([issue #138](https://github.com/eugene1g/agent-safehouse/issues/138)), which
sets the floor.

`sc` previously closed the gap itself with an `--append-profile` fragment. The
gate replaces that workaround: rather than patch around old safehouse
versions, require one that already covers it.

## Design decisions

**Hard fail, not a warning.** `sc`'s startup already prints several lines
(agent, profile, repo, keychain), so a warning would scroll past unread — and
the failure it predicts is exactly the kind nobody connects back to a startup
message.

**Fail open on anything unparseable.** If `safehouse --version` cannot be run,
or its output contains no dotted-numeric version (a source or `--head` build
printing something unexpected), the launch proceeds. Blocking every launch on
a failed guess about the version string is worse than the signing breakage the
gate guards against.

**Version, not capability.** A more precise check would grep the generated
policy (`safehouse --enable=ssh --stdout`) for the `/var/run` socket rule,
which would be robust to forks and to upstream regressions. The version check
was chosen for cost and clarity: `--version` is a ~20 ms call against a
~1000-line policy generation, and "upgrade safehouse" is a more actionable
message than "your policy lacks a rule". Worth revisiting if a second
version-sensitive dependency appears.

**Pre-release suffixes are ignored.** `parse_version` takes the leading
dotted-numeric run, so `0.12.0-rc1` reads as `(0, 12, 0)`. That treats an rc
as equal to its release rather than earlier, which is accurate enough for a
floor comparison and avoids implementing pre-release ordering.

## Verification

`test_sc_safehouse_version.py` covers:

- version parsing, including suffixed and version-less output
- the accept/reject boundary around the floor, including the shorter `0.11`
  (which must be rejected — tuple comparison makes `(0, 11) < (0, 11, 1)`)
- fail-open on unrecognized output and on an unrunnable binary
- that the safehouse installed on this machine satisfies the gate
- end-to-end, driving the real `sc` against a stub `safehouse` via
  `$SAFEHOUSE_BIN`: an old version aborts with the message and never reaches
  `Launching:`, a current version reaches exec (the stub's exit 99)

The end-to-end cases redirect `PROFILE_DIR` and `TMPDIR`, so they neither
touch synced config nor collide with the `--append-profile` fragments a live
`sc` session write-protects in the real temp dir.
