# Unix sockets in the per-user temp dir

## What this enables

Tools that split themselves into a parent and a helper process, talking over a
unix-domain socket in `$TMPDIR`, work inside `sc`. The case that motivated it is
Terraform: `terraform plan`, `apply` and `test` all launch provider plugins, and
`hashicorp/go-plugin` — the handshake library every provider uses — listens on
`$TMPDIR/plugin<random-digits>` on all non-Windows platforms. Without an allow,
every provider launch dies at startup:

```
plugin init error: error="listen unix /var/folders/.../T/plugin501607228: bind: operation not permitted"
```

The same library backs terragrunt, packer and vault plugins, so they are covered
too.

## Why the sandbox blocks it

safehouse's `20-network.sb` grants `network-bind` for IP sockets only:

```
(allow network-bind    (local ip))
(allow network-inbound (local ip))
```

Unix sockets are allowed one path at a time, for the specific cases safehouse
knows about — Chrome/Chromium/Codex `SingletonSocket`, VS Code's `vscode-ipc`,
`agent-browser`'s daemon. Everything else is denied by default.

The denial is narrow and easy to misread: `/var/folders` is mounted read-write,
so *creating* the socket file succeeds. Only `bind()` fails, with `EPERM`. Note
also that `dangerouslyDisableSandbox` (Claude Code's own inner sandbox) has no
effect here — the `sc` Seatbelt policy applies to every descendant process and
cannot be lifted from inside.

## Design

`temp_unix_socket_profile_text()` renders a Seatbelt fragment appended via
`safehouse --append-profile`, allowing bind/listen *and* connect for the socket
names listed in `TEMP_UNIX_SOCKET_BASENAMES`. Both directions are needed: the
provider listens, and Terraform core connects. Applied on every macOS launch —
it grants nothing until a process actually binds a matching name.

Two constraints shape it:

- **Scoped to socket basenames, not to the temp dir as a whole.** safehouse
  deliberately *denies* outbound connections to `vscode-git-*.sock` and
  `vscode-ipc-*.sock` in that same directory, because a VS Code running outside
  the sandbox binds them — connecting would hand git credentials and
  open-arbitrary-URL capability across a trust boundary. Appended fragments are
  emitted after the generated rules and Seatbelt takes the last match, so a
  blanket temp-dir allow would silently override those denies. New cases are
  added as regex entries in the list.
- **The temp dir is matched by shape** (`/var/folders/<x>/<y>/T/`), the way
  safehouse's own rules do it, so the fragment carries no machine-specific path.

Unrelated but worth knowing when a socket still fails: macOS caps
`sockaddr_un.sun_path` at 104 bytes. Pointing `TMPDIR` at a deeply nested
directory produces `bind: invalid argument` regardless of policy.

## Verification

`test_sc_unix_sockets.py`:

- fragment content — both directions present, name-anchored, no literal
  local paths;
- the basename list drives the regex (nothing hardcoded in the renderer);
- stable fragment path across launches;
- **compile check** — generates the full safehouse policy with the fragment and
  runs `sandbox-exec -f`. `sandbox-exec` compiles before applying, so a Seatbelt
  syntax error is caught even from inside a sandbox, where the apply step itself
  is denied. Mutating the fragment to invalid syntax fails this test.
- **bind check** — under the real policy, `plugin<pid>` binds and connects,
  while an unlisted name in the same directory stays denied. Requires a
  non-nested shell (nested `sandbox_apply` is refused), so it self-skips inside
  `sc` and prints why.
