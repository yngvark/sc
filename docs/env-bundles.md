# Env bundles (`sc -e <name>`)

An env bundle is a named set of environment variables that sc hands to the
sandbox when launched with `-e <name>`. It exists for tools inside the sandbox
that need a credential but cannot reach the macOS Keychain, which sc denies
(`keychain-deny.md`). sc resolves the credential on the host instead and passes
it in as a plain variable, the same route `GITHUB_TOKEN` takes. Bundles are
opt-in per launch, like `-a` for AWS, and independent of the profile, so
`sc -P -e datadog` works.

## Config

Bundles live in `~/.config/sc/config.toml` as `[env.<name>]` tables, next to the
`[group.<name>]` directory groups. Only the bundle name is free. Inside a bundle
sc reads exactly these keys, and any other key aborts the launch as a typo:

| Key          | Meaning                                                                                  |
| ------------ | ---------------------------------------------------------------------------------------- |
| `vars`       | Plain values written in the file. `~` and `$VARS` expand against the host environment.    |
| `op`         | 1Password `op://` references. sc runs `op read` on the host and caches the value.          |
| `pass`       | Names of host variables forwarded unchanged (same meaning as a profile's `[env].pass`).    |
| `op_account` | The 1Password account for the `op` references. Optional.                                   |

Every variable from `vars`, `op` and `pass` is forwarded with
`safehouse --env-pass=<VAR>`. sc writes the values it resolved into its own
environment as the last step of a launch, after every `op read` and Keychain
lookup has finished, so a bundle is free to set `HOME`, `PATH` or `OP_ACCOUNT`
for the sandbox without moving where sc itself looks for credentials.

Each variable gets exactly one owner. Alongside the bundles, the owners are
`--aws` (`AWS_PROFILE`), a profile's `[env].pass`, and a profile's
`[github].token` (`GITHUB_TOKEN`). Two owners for one name abort the launch:
one value would reach the sandbox while the status line announced the other.
A name used twice across a bundle's tables or across two bundles, and a name sc
manages from another flag, abort before any 1Password prompt. So does a key that
is not a legal environment variable name, meaning letters, digits and `_`, not
starting with a digit.

`op` values are cached in the Keychain under service `sc-env-secret`, account
`<bundle>/<VAR>`, with the same `GITHUB_TOKEN_CACHE_TTL` as the GitHub token. The
service differs from the GitHub token's on purpose, so a bundle can never read a
profile's token. `sc -e <name> --warm-token` fills the cache without launching.
sc prints only the last four characters of a secret.

## Example: Datadog through pup

```toml
[env.datadog]
op_account = "my-team.1password.eu"

[env.datadog.vars]
DD_SITE = "datadoghq.eu"
DD_TOKEN_STORAGE = "file"
PUP_CONFIG_DIR = "$TMPDIR/pup"

[env.datadog.op]
DD_API_KEY = "op://Vault/Datadog/api-key"
DD_APP_KEY = "op://Vault/Datadog/app-key"
```

Each variable is there for a reason:

- `DD_API_KEY`, `DD_APP_KEY`: pup's API-key authentication. pup's default is an
  OAuth login whose tokens live in the macOS Keychain, unreachable in the sandbox.
- `DD_SITE`: pup defaults to `datadoghq.com`; without this an EU org gets 403s.
- `DD_TOKEN_STORAGE=file`: pup probes the Keychain on every command, even with
  API keys in env, and fails with "A default keychain could not be found" inside
  the sandbox. This makes it use file storage instead. No token file is ever
  written, because API keys are never stored.
- `PUP_CONFIG_DIR`: pup's default config dir on macOS is
  `~/Library/Application Support/pup`, which the sandbox cannot reach, and pup
  panics when it cannot create the dir. `$TMPDIR` is the same per-user path
  inside and outside the sandbox and is writable there.

TLS is unaffected by the Keychain deny: pup verifies certificates through
`trustd`, which stays allowed.

### pup on the host

On the host, pup's Homebrew binary is only ad-hoc signed, so every upgrade is a
new app identity and macOS prompts for the Keychain password again on the next
`pup auth login`. Two ways around it: skip the OAuth login on the host entirely,
since the sandbox uses API keys, or put `token_storage: file` in
`~/Library/Application Support/pup/config.yaml` so pup keeps its tokens in a
0600 file instead of the Keychain.

## Verification

Unit tests: `./test_sc_env_bundles.py` for the config tables, and
`./test_sc_env_forwarding.py` for the forwarding table (one owner per variable,
and `op`/`security` running with the host environment). End-to-end, from outside
a sandbox:

```
sc -P -e datadog -s -- -c 'pup --no-agent auth status; pup --no-agent monitors list --limit 1'
```

Expected: `auth_method: api_keys`, the configured site, one monitor, and no
Keychain prompt. A second launch reports both keys as cached. Without `-e`,
`sc -P -s -- -c 'env | grep -c ^DD_'` prints 0.
