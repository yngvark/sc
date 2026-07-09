# SSH agent socket allow for macOS 26 (Tahoe)

## Symptom

Inside the `sc` sandbox, `git commit` failed for anyone with commit signing
enabled (`commit.gpgsign=true`, `gpg.format=ssh`, an SSH signing key):

```
error: Enter passphrase for ".../id_ed25519_github_signing_key":
       incorrect passphrase supplied to decrypt private key?
fatal: failed to write commit object
```

It is not a wrong passphrase. `ssh-add -l` inside the sandbox returned
`Error connecting to agent: Operation not permitted`.

## Cause

Git normally signs by talking to the running **ssh-agent** (via `SSH_AUTH_SOCK`),
which holds the already-decrypted key — no prompt. When the sandbox blocks the
agent socket, git falls back to reading the passphrase-protected key file
directly and prompts for the passphrase, which cannot be answered
non-interactively, so signing fails.

`sc` already passes `--enable=ssh` to safehouse, whose `ssh.sb` integration
allows the launchd-managed agent socket — but only at the historical locations:

```
/tmp/com.apple.launchd.*/Listeners
/private/tmp/com.apple.launchd.*/Listeners
~/.ssh/agent/*                       (Tahoe-style, but not the one SSH_AUTH_SOCK uses)
```

macOS 26 (Tahoe) relocated the launchd socket that `SSH_AUTH_SOCK` points at to
`/var/run/com.apple.launchd.*/Listeners` (i.e. `/private/var/run/...`). That
path is not in safehouse's allow-list, so the sandbox denies the connect. This
is why commits "used to work" — a macOS upgrade moved the socket.

## Fix

The root cause is an upstream gap in safehouse's `ssh.sb`; the proper fix is to
add the `/private/var/run/com.apple.launchd.*/Listeners` class there
(https://github.com/eugene1g/agent-safehouse). Until that ships, `sc` closes
the gap itself: `write_ssh_agent_profile()` detects when `SSH_AUTH_SOCK`
resolves under `/private/var/run/` and appends a one-rule sandbox profile via
safehouse `--append-profile` that re-allows the socket class (file access +
`network-outbound` unix-socket connect), mirroring safehouse's own `/tmp` rule.

The fragment is written to a stable temp file (`$TMPDIR/sc-ssh-agent.sb`,
overwritten each launch) and only added on macOS when the `/var/run` socket is
actually in use, so older machines and non-macOS hosts are unaffected. Remove
the workaround once safehouse covers `/var/run` upstream.

## Verification

- `test_sc_ssh_agent.py` covers the decision (`/var/run` → allow, `/tmp` → skip,
  empty → skip), the fragment's rule content, and the stable-file write.
- End-to-end (requires a fresh `sc` launch, since nested `sandbox-exec` is
  denied inside an existing sandbox): inside `sc`, `ssh-add -l` lists the signing
  key and `git commit` signs without a passphrase prompt.
