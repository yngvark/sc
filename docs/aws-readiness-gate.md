# AWS readiness gate (`sc -a`)

## What it does

`sc -a` mounts `~/.aws` read/write and forwards `AWS_PROFILE` into the sandbox.
If `AWS_PROFILE` is set, sc first verifies on the host that its credentials
resolve, and refuses to launch when they do not:

```
Error: AWS profile 'my-sso' has no usable credentials.
  aws says: The SSO session associated with this profile has expired or is otherwise invalid
  Log in first, e.g. `aws sso login --profile my-sso`, then re-run.
```

On success the startup line confirms what was checked:

```
AWS: sharing ~/.aws (rw), AWS_PROFILE=my-sso (credentials OK)
```

With no `AWS_PROFILE` there is nothing to check and nothing to block on — sc
says so and launches:

```
AWS: sharing ~/.aws (rw), AWS_PROFILE unset (set one in the session)
```

The check runs right after the safehouse version gate — before the temp dir,
the 1Password token fetch and the policy fragments — so a blocked launch has no
side effects, not even a history entry.

## Why the check exists

`-a` is the flag you pass when the agent's job involves AWS, and an expired SSO
login is silent at launch time: the mounted `~/.aws/sso/cache` looks fine, the
token in it does not. SSO tokens expire on a working-day timescale, so this is
the common case, not an edge case.

The symptom arrives minutes later, mid-task, as an AWS API error attributed to
whatever the agent happened to be running — a terraform plan, a CLI call in a
script — rather than to the launch. The fix (`aws sso login`, which opens a
browser) belongs on the host, so the whole detour is avoidable by checking one
command before the sandbox exists.

## Design decisions

**An unset `AWS_PROFILE` is not an error.** The purpose of `-a` is to share the
credentials directory; forwarding `AWS_PROFILE` is a convenience on top. The
profile is a per-command choice (`aws --profile`, `AWS_PROFILE=… terraform …`)
that can be set or changed inside the session, so demanding one on the host
would refuse launches that go on to work fine. A profile that *is* set is
different: every aws command in the session inherits it, so a broken one is
worth blocking on.

**`aws configure export-credentials`, not `sts get-caller-identity`.**
`export-credentials` resolves credentials the way the SDKs do — env vars, config
file, SSO and assume-role caches, `credential_process` — and exits non-zero when
there are none or the cached SSO token has expired. It needs no network
round-trip, so it is cheap enough to run on every launch, and its error text is
already user-facing. `get-caller-identity` would additionally prove the
credentials work server-side, at the cost of latency and a network dependency on
every `sc -a`; the failure it would catch and `export-credentials` would not
(locally-valid credentials the server rejects) is rare enough not to pay for.

**stdout is never printed.** A successful `export-credentials` prints the secret
access key and session token as JSON. `aws_login_error` discards stdout entirely
and reports only the last stderr line, with the `aws: [ERROR]:` prefix stripped.

**The check names the profile explicitly.** `--profile <name>` is passed rather
than relying on the inherited `AWS_PROFILE`, so what gets verified is provably
what gets forwarded.

**Fail open when the check itself cannot run.** No `aws` on `PATH`, or a probe
that errors or times out, lets the launch proceed with a note on stderr. This
matches the safehouse version gate: blocking a launch because a guard could not
run is worse than the confusion it prevents.

**Hard fail, no escape hatch.** A set-but-unusable profile aborts rather than
warns — sc's startup already prints several lines, and a warning about a failure
that arrives minutes later would scroll past unread. There is no skip flag
either: unsetting `AWS_PROFILE` (or omitting `-a`) is the escape hatch.

**Only under `-a`.** A launch without the flag never touches AWS, so it must
not pay the check or inherit its failure modes.

## Verification

`test_sc_aws_check.py` covers:

- error-text trimming: the real `aws` output shape (leading blank line,
  `aws: [ERROR]:` prefix), unprefixed output, and empty output
- `aws_login_error` following the stub `aws` exit code, and failing open when
  no `aws` is on `PATH`
- end-to-end against stub `aws` and `safehouse` binaries: an expired login
  aborts with the profile name, what `aws` said, and the `aws sso login` hint,
  without reaching `Launching:`
- end-to-end pass case: exec is reached (the stub's exit 99), the confirmation
  line is printed, the stub was called with exactly
  `configure export-credentials --profile my-sso`, and no credential material
  from its stdout appears in sc's output
- end-to-end that unset and whitespace-only `AWS_PROFILE` reach exec, print the
  unset note, and never invoke `aws` — even when the stub `aws` would fail
- that a launch without `-a` is neither gated nor mentions AWS, even with a
  broken login

The end-to-end cases redirect `PROFILE_DIR` and `TMPDIR` and prepend the stub
`aws` to `PATH`, so they need no AWS login, touch no synced config, and do not
collide with the `--append-profile` fragments a live `sc` session
write-protects in the real temp dir.
