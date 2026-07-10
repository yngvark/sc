# Denying macOS Keychain access inside the sandbox

## Problem

`sc` injects a deliberately restricted GitHub fine-grained PAT as
`GITHUB_TOKEN` so the sandboxed agent acts on GitHub with a capped blast
radius. In practice that cap didn't hold: an agent inside the sandbox ran
`env -u GITHUB_TOKEN gh ...` and `gh` silently fell back to the user's
keyring OAuth token (broad `repo` scope), which it could read from the login
keychain — from inside the sandbox.

## Why the keychain was reachable at all

safehouse's policy is deny-by-default, and its Keychain integration
(`55-integrations-optional/keychain.sb`) is *not* included by default. But
agent profiles can declare integrations as requirements, and the Claude Code
agent profile (`60-agents/claude.sb`) declares
`$$require=...keychain.sb...$$` — because Claude Code may store its own
OAuth credentials in the login keychain. Wrapping `claude` therefore
auto-injects allows for `com.apple.SecurityServer` and friends.

Keychain access is Mach IPC to `securityd`, not a file read, so safehouse's
directory scoping doesn't constrain it — and Seatbelt cannot filter keychain
access per item. Allowing Claude Code's credential item means allowing every
secret in the login keychain, including `gh`'s keyring token.

## Fix

`sc` writes a policy fragment (`sc-deny-keychain.sb`) and passes it via
`safehouse --append-profile`. Appended rules land after the generated policy,
and Seatbelt is last-match-wins (safehouse's own terminal denies rely on
this), so the fragment's denies override the integration's allows. It denies:

- `mach-lookup` to `com.apple.SecurityServer`, `com.apple.securityd.xpc`,
  `com.apple.secd`, `com.apple.security.agent`, `com.apple.security.authhost`
  — the services that decrypt or mediate keychain secrets.
- `file-read* file-write*` on `~/Library/Keychains` — the DB files.

`com.apple.trustd` stays allowed: it performs TLS trust evaluation, not
credential storage; denying it would break HTTPS.

safehouse additionally write-protects every `--append-profile` file inside
the sandbox, so the agent cannot edit the fragment.

## Consequences

- `gh` without `GITHUB_TOKEN` is anonymous instead of silently escalating to
  the keyring token. With a profile, the PAT is the only GitHub credential.
- `security find-generic-password ... -w` fails inside the sandbox.
- Claude Code must authenticate from `~/.claude/.credentials.json` (its file
  fallback) instead of the keychain. If login breaks after this change,
  launch with `sc --keychain` and consider re-logging-in so the file store
  is current.
- Escape hatch: `sc -k` / `sc --keychain` skips the fragment for tools that
  genuinely need the keychain.

## Alternatives considered

- **Shadow `GH_CONFIG_DIR`**: pointing `gh` at an empty config dir hides the
  keyring account (verified: `gh` only consults the keyring for hosts listed
  in `hosts.yml`). Rejected as primary fix — env vars are advice, not policy;
  the agent can unset them, which is exactly how the incident happened.
- **Blanket deny without opt-out**: rejected because safehouse requires the
  keychain for Claude Code for a reason; if a machine's Claude login lives
  only in the keychain, the deny would lock it out with no recourse.

## Verification

Unit tests: `./test_sc_keychain.py` (fragment content, wiring, flag).
End-to-end (must run from outside a sandbox):

```
sc -P -s -- -c 'security find-generic-password -s "gh:github.com" -w; env -u GITHUB_TOKEN gh auth status'
```

Expected: `security` fails with a keychain/IPC error; `gh` reports not
logged in. Then confirm `sc` still starts an authenticated claude session.
