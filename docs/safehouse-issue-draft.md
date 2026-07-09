# Draft issue for eugene1g/agent-safehouse

Draft of the upstream issue to file against
https://github.com/eugene1g/agent-safehouse . Structure mirrors the template in
https://github.com/zensical/ui/issues/181 (AI Note → Description → Environment →
Reproduction → Analysis → Impact → Fix).

---

**Title:** `--enable=ssh` blocks the ssh-agent socket on macOS Tahoe — `SSH_AUTH_SOCK` now lives under `/var/run`

> [!NOTE]
> This issue was investigated and written by an AI agent (Claude Code), operated and reviewed by [@yngvark](https://github.com/yngvark). The symptom and root cause were reproduced by running the commands below, and the proposed fix was confirmed to compile into the generated policy. The macOS-version attribution is the one part I could not confirm from a primary source — see Analysis.

## Description

Under `--enable=ssh` on macOS Tahoe, a sandboxed process cannot reach the
ssh-agent. `ssh-add -l` fails with `Error connecting to agent: Operation not
permitted`, and `git commit` with SSH commit signing falls back to reading the
passphrase-protected key file, prompting for a passphrase that can't be answered
non-interactively:

```
error: Enter passphrase for ".../id_ed25519_signing_key": incorrect passphrase supplied to decrypt private key?
fatal: failed to write commit object
```

`SSH_AUTH_SOCK` is passed through into the sandbox correctly — the problem is
purely that the socket path it points at is not in the `ssh` integration's
allow-list.

## Environment

- macOS 26.5.1 (Tahoe), build 25F80, Apple Silicon
- Agent Safehouse 0.11.0
- `SSH_AUTH_SOCK=/var/run/com.apple.launchd.<id>/Listeners` (i.e. `/private/var/run/...`)

## Reproduction

On an affected machine (see Analysis for which macOS versions), with a running
ssh-agent that holds at least one key:

```console
$ echo $SSH_AUTH_SOCK
/var/run/com.apple.launchd.XXXXXXXX/Listeners      # note: /var/run, not /tmp

$ ssh-add -l                                       # outside the sandbox: works
256 SHA256:... you@host (ED25519)

$ safehouse --enable=ssh -- ssh-add -l             # inside the sandbox: fails
Error connecting to agent: Operation not permitted
```

Applying the fix below as an `--append-profile` fragment makes the in-sandbox
`ssh-add -l` succeed and lets `git commit` sign without a passphrase prompt.

## Analysis

The `ssh` integration (`55-integrations-optional/ssh.sb`) allows the
launchd-managed agent socket only at the historical `/tmp` and `/private/tmp`
locations (plus `~/.ssh/agent`). On this machine the socket that
`SSH_AUTH_SOCK` actually points at is under `/private/var/run/`, which no rule
covers, so the connect is denied by default.

The `~/.ssh/agent/s.*` socket that the integration *does* allow is a separate
socket; connecting to it hung in my testing rather than reaching the agent that
holds the key, so it is not a usable fallback.

On when the path moved: commit signing worked in the sandbox on this machine
until recently and broke without any config change on my end, so I suspect a
recent Tahoe point release moved the socket from `/private/tmp` to `/var/run`.
The strongest external evidence I found is an
[Apple Community thread](https://discussions.apple.com/thread/256252831) where a
user reports the path as `/private/tmp/...` on 26.3 and `/var/run/...` on 26.4,
which would put the move at **macOS Tahoe 26.4** — but that is single-source
forum evidence, not an authoritative Apple source. The safe takeaway regardless
of version: `SSH_AUTH_SOCK` should be honored at runtime and both socket
locations allowed.

## Impact

Any commit signing, `ssh`, `scp`, or agent-dependent workflow inside the sandbox
breaks on affected macOS versions. For SSH commit signing specifically it
degrades to an interactive passphrase prompt that cannot be satisfied
non-interactively, so `git commit` fails outright.

## Fix

Allow the `/var/run` launchd socket class alongside the existing `/tmp` ones —
both the file access and the `network-outbound` unix-socket connect:

```scheme
(allow file-read* file-write*
    (regex #"^/private/var/run/com\.apple\.launchd\.[^/]+/Listeners$"))
(allow network-outbound
    (remote unix-socket (path-regex #"^/private/var/run/com\.apple\.launchd\.[^/]+/Listeners$")))
```

Confirmed working as an `--append-profile` fragment on 26.5.1.
