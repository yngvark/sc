# Sandbox features enabled on every launch

## What it is

safehouse's policy is deny-by-default. Optional capabilities are opted into
per launch with `--enable=<feature>`, and `sc` always passes a fixed set,
listed in one place as `SAFEHOUSE_FEATURES` in `sc`:

| Feature             | Why sc enables it                                   |
| ------------------- | --------------------------------------------------- |
| `ssh`               | git commit signing through the ssh-agent socket      |
| `playwright-chrome` | browser-driven verification of frontend changes      |
| `docker`            | docker/podman CLI and daemon socket                  |

Adding a capability means adding a row to that list, not a branch at launch
time. `SAFEHOUSE_FEATURES` order is irrelevant; safehouse composes the
fragments and each one is self-contained.

## Why these are on by default rather than per-flag

A denied capability does not surface as "the sandbox blocked this". It
surfaces as a plain `Operation not permitted` from an unrelated tool, or worse
as a wrong-looking success: without `ssh`, git commit signing degrades to an
interactive passphrase prompt nothing can answer
(see `safehouse-version-gate.md`); without `docker`, `which docker` reports
*not found* even though Docker Desktop is installed, because the sandbox
denies `stat` on `~/.docker/bin` and `/Applications/Docker.app`. An agent
inside the sandbox reads that as "docker isn't installed on this machine" and
works around a problem that doesn't exist.

Both of those are capabilities an agent needs for ordinary verification work —
running a test container, checking a change in a browser, signing a commit.
Gating them behind a flag means the failure is discovered mid-session, by
which point the diagnosis has already gone wrong. Enabling them up front costs
sandbox scope; leaving them off costs correctness of the agent's mental model.

## What `--enable=docker` opens

safehouse labels the docker fragment *high-risk*, and it is broader than a
single socket. It re-allows, over the base policy's explicit denies:

- Daemon sockets: `/var/run/docker.sock` and its `/private` backing path,
  plus the user-scoped sockets for Docker Desktop (`~/.docker/run`), OrbStack,
  Colima, and Rancher Desktop — as both file access and `network-outbound`
  (a `connect()` on a unix socket is classified as network, not file, by
  Seatbelt, so both rule types are needed).
- Runtime state and config: `~/.docker`, `~/.colima`.
- Podman equivalents, since Podman speaks the Docker API and the base policy
  denies it in the same block.
- Reads of `/Applications/Docker.app` (and the `/System/Volumes/Data` alias),
  because the CLI binary lives inside the app bundle and the `docker` on PATH
  is a symlink into it.

Reaching the daemon means the sandboxed agent can start containers, and a
container is not itself sandboxed by Seatbelt — it can mount host paths the
policy would otherwise deny. This is the deliberate trade-off above, not an
oversight. `sc -k`-style opt-out does not exist for features; to run without
docker, call `safehouse` directly.

## Verification

`./test_sc_safehouse_features.py`:

- the expected features are listed, and the `--enable=` flags are built from
  the list rather than inline literals;
- the installed safehouse accepts every listed feature — it exits 1 on an
  unknown `--enable` value, so a renamed or misspelled feature would
  otherwise abort every launch;
- the base policy's last rule for `/var/run/docker.sock` is a `deny` and
  `--enable=docker` flips it to `allow` (Seatbelt is last-match-wins), so the
  feature cannot survive in name only;
- an end-to-end launch against a stub `$SAFEHOUSE_BIN` records the real argv
  and every feature appears in it.
