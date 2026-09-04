#!/usr/bin/env -S uv --quiet run --python >=3.11 --script
# /// script
# requires-python = ">=3.11"
# ///
"""
safe-claude.py

Wrapper around `safehouse` that handles GitHub token (1Password-backed,
Keychain-cached), AWS sharing, and resume/continue, then execs safehouse to
launch claude inside its sandbox profile.

Token resolution runs OUTSIDE the sandbox (where `op` and `security` work).
The resolved token is exported as GITHUB_TOKEN and forwarded via safehouse's
--env-pass=GITHUB_TOKEN, so `gh` inside the sandbox picks it up.

Run `safe-claude.py --help` for usage.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

PROFILE_DIR = Path(os.environ.get("PROFILE_DIR", str(Path.home() / ".config/sc")))
PROFILES_DIR = Path(os.environ.get("SAFE_CLAUDE_PROFILES_DIR", str(PROFILE_DIR / "profiles")))
PROFILE_FILE = PROFILE_DIR / "active-profile"
HISTORY_FILE = PROFILE_DIR / "history.jsonl"
CONFIG_FILE = PROFILE_DIR / "config.toml"
HISTORY_MAX = 100
SAFEHOUSE = os.environ.get("SAFEHOUSE_BIN", "safehouse")

# safehouse policy features sc always enables. Each entry is a `--enable=`
# flag; add a case here rather than branching at launch time.
SAFEHOUSE_FEATURES = [
    "ssh",                  # git commit signing via ssh-agent
    "playwright-chrome",    # playwright-cli / browser-driven verification
    "docker",               # docker CLI + daemon socket
]

# sc relies on safehouse's ssh integration allowing the launchd-managed
# SSH_AUTH_SOCK socket under /var/run — macOS Tahoe 26.4 moved it there from
# /private/tmp. Without that rule the sandbox cannot reach ssh-agent, so git
# commit signing silently degrades to an interactive passphrase prompt that
# nothing can answer, and the error names a passphrase rather than the sandbox.
# safehouse 0.11.1 covers it (eugene1g/agent-safehouse#138), so that is the
# floor: sc carried its own --append-profile workaround until then.
MIN_SAFEHOUSE_VERSION = (0, 11, 1)

# Shared with the retired claude-docker on purpose so the warm cache
# survives the migration.
KEYCHAIN_SERVICE = "claude-docker-github-token"
TTL = int(os.environ.get("GITHUB_TOKEN_CACHE_TTL", "36000"))   # 10h


def err(msg: str) -> None:
    print(msg, file=sys.stderr)


def fail(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    err(msg)
    sys.exit(code)


def parse_version(text: str) -> tuple[int, ...] | None:
    """The first dotted-numeric run in TEXT, e.g. `safehouse --version`'s
    "Agent Safehouse 0.11.1" -> (0, 11, 1). None if there is none. A
    pre-release suffix ("0.12.0-rc1") is dropped, which is accurate enough for
    a >= floor check."""
    m = re.search(r"\d+(?:\.\d+)*", text)
    return tuple(int(p) for p in m.group(0).split(".")) if m else None


def safehouse_version_too_old(version_output: str) -> tuple[int, ...] | None:
    """The parsed version if it is below MIN_SAFEHOUSE_VERSION, else None.
    Output with no version in it (an unexpected build) counts as new enough:
    fail open, since blocking every launch on a failed guess is worse than the
    signing breakage this guards against."""
    version = parse_version(version_output)
    if version is None or version >= MIN_SAFEHOUSE_VERSION:
        return None
    return version


def require_safehouse_version(safehouse_bin: str) -> None:
    """Abort if SAFEHOUSE_BIN is older than MIN_SAFEHOUSE_VERSION. A safehouse
    that cannot be run or does not report a version is let through."""
    try:
        proc = subprocess.run(
            [safehouse_bin, "--version"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return
    old = safehouse_version_too_old(proc.stdout + proc.stderr)
    if old is None:
        return
    fail(
        f"safe-claude: safehouse {'.'.join(map(str, old))} is too old — "
        f"sc needs {'.'.join(map(str, MIN_SAFEHOUSE_VERSION))} or newer.\n"
        "  Older versions block the ssh-agent socket on macOS Tahoe 26.4+, so git\n"
        "  commit signing inside the sandbox fails with an unanswerable passphrase\n"
        "  prompt. Upgrade with `brew upgrade agent-safehouse` or `safehouse update`."
    )


# `aws configure export-credentials` resolves credentials exactly the way the
# SDKs do — env vars, config file, SSO and assume-role caches, credential_process
# — and exits non-zero when there are none or the cached SSO token has expired.
# Unlike `sts get-caller-identity` it needs no network round-trip, so it is cheap
# enough to run on every launch. Its *stdout* contains the secret access key, so
# only stderr is ever surfaced.
AWS_LOGIN_CHECK_ARGS = ["configure", "export-credentials"]
AWS_LOGIN_CHECK_TIMEOUT = 20


def aws_error_text(stderr: str) -> str:
    """The aws CLI's complaint, trimmed to one line for reporting. Takes the
    last non-empty stderr line (the CLI prints a leading blank one) and drops
    its `aws: [ERROR]:` prefix."""
    lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    if not lines:
        return "aws reported no usable credentials (and printed no error)"
    return re.sub(r"^aws:\s*\[ERROR\]:\s*", "", lines[-1])


def aws_login_error(profile: str) -> str | None:
    """None if PROFILE's credentials resolve, else the aws CLI's error text.
    An `aws` that is missing or cannot be run counts as fine: blocking the
    launch on a check that could not run is worse than the confusing in-sandbox
    failure it guards against (same fail-open stance as the safehouse gate)."""
    aws_bin = shutil.which("aws")
    if aws_bin is None:
        err("AWS: `aws` not found on PATH — skipping the credentials check")
        return None
    try:
        proc = subprocess.run(
            [aws_bin, *AWS_LOGIN_CHECK_ARGS, "--profile", profile],
            capture_output=True, text=True, timeout=AWS_LOGIN_CHECK_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        err("AWS: credentials check could not run — skipping it")
        return None
    return None if proc.returncode == 0 else aws_error_text(proc.stderr)


def check_aws_ready() -> str:
    """The AWS_PROFILE to forward, "" if there is none. Aborts when a profile is
    set but its credentials do not resolve. Runs on the host, before the sandbox
    exists, because that is where `aws`, ~/.aws and the SSO cache are reachable;
    inside the sandbox the same failure shows up much later as an opaque
    credentials error. An unset AWS_PROFILE is fine: -a exists to share the
    credentials dir, and the profile can be chosen or changed inside the
    session."""
    profile = os.environ.get("AWS_PROFILE", "").strip()
    if not profile:
        return ""
    error = aws_login_error(profile)
    if error is not None:
        fail(
            f"Error: AWS profile '{profile}' has no usable credentials.\n"
            f"  aws says: {error}\n"
            f"  Log in first, e.g. `aws sso login --profile {profile}`, then re-run."
        )
    return profile


def discover_profiles() -> list[str]:
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(p.stem for p in PROFILES_DIR.glob("*.toml") if p.is_file())


# Keychain service for secrets an env bundle pulls from 1Password (see
# ENV_BUNDLE_KEYS); the account is "<bundle>/<VAR>". Distinct from
# KEYCHAIN_SERVICE so a bundle can never read a profile's GitHub token.
ENV_SECRET_SERVICE = "sc-env-secret"


def cached_secret(service: str, account: str, label: str) -> str | None:
    """The Keychain-cached value under SERVICE/ACCOUNT if it is fresher than TTL,
    else None (an expired entry is deleted). LABEL prefixes the status line."""
    if sys.platform != "darwin":
        return None
    try:
        stored = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        return None
    ts_str, _, value = stored.partition(":")
    if not ts_str or not value:
        return None
    try:
        ts = int(ts_str)
    except ValueError:
        return None
    age = int(time.time()) - ts
    if age >= TTL:
        subprocess.run(
            ["security", "delete-generic-password", "-s", service, "-a", account],
            capture_output=True, check=False,
        )
        return None
    remaining = TTL - age
    err(f"{label}: cached ({remaining // 3600}h{(remaining % 3600) // 60}m remaining)")
    return value


def cache_secret(service: str, account: str, value: str, label: str) -> None:
    """Store VALUE under SERVICE/ACCOUNT in the Keychain, timestamped for TTL."""
    if TTL == 0:
        return
    if sys.platform != "darwin":
        err(f"{label}: caching not supported on this platform")
        return
    payload = f"{int(time.time())}:{value}"
    rc = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", payload],
        capture_output=True,
    ).returncode
    err(f"{label}: fetched and cached" if rc == 0 else f"{label}: caching failed (will retry next run)")


def get_cached_token(profile: str) -> str | None:
    return cached_secret(KEYCHAIN_SERVICE, profile, "GitHub token")


def cache_token(profile: str, token: str) -> None:
    cache_secret(KEYCHAIN_SERVICE, profile, token, "GitHub token")


def load_profile(name: str) -> dict:
    path = PROFILES_DIR / f"{name}.toml"
    if not path.is_file():
        fail(f"Error: Profile file not found: {path}")
    with path.open("rb") as f:
        return tomllib.load(f)


def resolve_dirs(paths: list, source: str) -> list[str]:
    """Existing real paths from PATHS, with $VARS and ~ expanded and symlinks
    resolved — mounting a link path alone leaves the contents inaccessible
    inside the sandbox (same reason PROFILE_DIR is mounted resolved). A path
    that does not exist is dropped with a warning naming SOURCE: safehouse
    rejects a missing --add-dirs entry, so one stale line in config would
    otherwise block every launch."""
    out: list[str] = []
    for p in paths:
        expanded = Path(os.path.expandvars(str(p))).expanduser()
        if not expanded.exists():
            err(f"{source} dir missing, skipping: {p}")
            continue
        out.append(os.path.realpath(expanded))
    return out


def profile_dirs(profile: dict) -> tuple[list[str], list[str]]:
    """Return (ro, rw) lists of existing absolute paths from profile [dirs] section."""
    dirs = profile.get("dirs") or {}
    return (
        resolve_dirs(dirs.get("ro") or [], "Profile"),
        resolve_dirs(dirs.get("rw") or [], "Profile"),
    )


def profile_named_dirs(profile: dict) -> list[str]:
    """The [dirs] entries of PROFILE as written, before resolution — the names
    the sandbox has to keep working (see symlink_read_literals)."""
    dirs = profile.get("dirs") or {}
    return [str(p) for p in ((dirs.get("ro") or []) + (dirs.get("rw") or []))]


def load_config() -> dict:
    """The parsed global config (CONFIG_FILE), {} when there is none. A malformed
    file aborts rather than launching without its grants: a silently narrower
    sandbox surfaces much later as an unrelated `Operation not permitted`."""
    if not CONFIG_FILE.is_file():
        return {}
    try:
        with CONFIG_FILE.open("rb") as f:
            return tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as e:
        fail(f"Error: could not read {CONFIG_FILE}: {e}")


def dir_covers(member: str, path: str) -> bool:
    """True if PATH is MEMBER or lives under it. Both sides are expanded and
    realpath'd first: safehouse resolves --add-dirs to realpaths and Seatbelt
    matches the resolved path of the file being opened, so comparing link paths
    would silently miss (same lesson as PROFILE_DIR.resolve() below)."""
    m = os.path.realpath(Path(os.path.expandvars(member)).expanduser())
    p = os.path.realpath(path)
    return p == m or p.startswith(m.rstrip(os.sep) + os.sep)


def config_dirs_for(config: dict, cwd: str) -> tuple[list[str], list[str]]:
    """The (ro, rw) dirs the global config grants for a launch in CWD.

    [group.<name>] holds one ro or rw list: a set of dirs that belong together.
    Launching in any member — or anywhere below it — mounts every member of that
    group, so the relation is symmetric and needs stating once. A group has a
    single access level; ro and rw in one group is an error. Every activated
    group contributes: grants union, they do not override. No transitive
    closure — two groups sharing a member do not chain, so what a launch gets is
    readable straight off the file."""
    ro: list[str] = []
    rw: list[str] = []
    for name, access, members in activated_groups(config, cwd):
        paths = resolve_dirs(members, f"Config [group.{name}]")
        if not paths:
            continue
        err(f"Dirs: {name} -> {', '.join(compress_home(p) for p in paths)} ({access})")
        (ro if access == "ro" else rw).extend(paths)
    return ro, rw


def activated_groups(config: dict, cwd: str) -> list[tuple[str, str, list[str]]]:
    """The (name, access, members-as-written) of every [group] a launch in CWD
    activates. Split out of config_dirs_for() so the members are also available
    unresolved — symlink_read_literals() needs the names, not the realpaths."""
    out: list[tuple[str, str, list[str]]] = []
    for name, spec in sorted((config.get("group") or {}).items()):
        if not isinstance(spec, dict):
            continue
        members_ro = spec.get("ro") or []
        members_rw = spec.get("rw") or []
        if members_ro and members_rw:
            fail(f"Error: [group.{name}] in {CONFIG_FILE} has both ro and rw; "
                 "a group is one set of dirs at one access level — split it in two")
        access = "ro" if members_ro else "rw"
        members = [str(m) for m in (members_ro or members_rw)]
        if not any(dir_covers(m, cwd) for m in members):
            continue
        out.append((name, access, members))
    return out


def profile_match_dirs(profile: dict) -> list[str]:
    """The [match].dirs entries of PROFILE — the directories it applies in."""
    match = profile.get("match") or {}
    return [str(d) for d in (match.get("dirs") or [])]


def read_profile_file(name: str) -> dict | None:
    """The parsed profile NAME, or None if it cannot be read. Used when scanning
    every profile for a [match] — one unreadable file must not stop a launch that
    has nothing to do with it, so it warns instead of aborting (unlike
    load_profile(), which is the profile actually being used)."""
    path = PROFILES_DIR / f"{name}.toml"
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as e:
        err(f"Profile: skipping unreadable {path}: {e}")
        return None


def match_profile_for(cwd: str) -> tuple[str, str] | None:
    """The (name, matched dir) of the profile whose [match].dirs covers CWD, or
    None. A dir covers itself and everything below it, so a repo's subdirectories
    match too.

    The deepest match wins: every member covering CWD is an ancestor of it, so
    they all sit on one chain and "longest path" is exactly "most specific" —
    `[match]` in a broad profile (~/src) can be narrowed by a specific one
    (~/src/work) without either knowing about the other. Two *different* profiles
    matching at the same depth is a config mistake with no safe resolution — the
    profile picks the GitHub token — so it aborts and names them."""
    matches: list[tuple[int, str, str]] = []
    for name in discover_profiles():
        profile = read_profile_file(name)
        if profile is None:
            continue
        for member in profile_match_dirs(profile):
            if not dir_covers(member, cwd):
                continue
            resolved = os.path.realpath(Path(os.path.expandvars(member)).expanduser())
            matches.append((len(resolved.rstrip(os.sep)), name, resolved))
    if not matches:
        return None
    deepest = max(depth for depth, _, _ in matches)
    finalists = sorted({(name, member) for depth, name, member in matches if depth == deepest})
    if len({name for name, _ in finalists}) > 1:
        fail(
            "Error: several profiles claim this directory at the same depth: "
            + ", ".join(f"{name} ([match] {compress_home(member)})" for name, member in finalists)
            + f"\n  {compress_home(os.path.realpath(cwd))} would get an arbitrary GitHub token.\n"
            "  Make one [match] dir more specific, or pass -p <name> / -P for this launch."
        )
    return finalists[0]


def profile_env_pass(profile: dict) -> list[str]:
    """Return env var names to forward into the sandbox from [env].pass."""
    env = profile.get("env") or {}
    return [str(v) for v in (env.get("pass") or [])]


def op_read(op_ref: str, op_account: str, what: str) -> str:
    """The secret at OP_REF via `op read`, in OP_ACCOUNT when given. Aborts naming
    WHAT (e.g. "GitHub token (profile: jobb)") when op fails or returns nothing."""
    cmd = ["op", "read", op_ref]
    if op_account:
        cmd += ["--account", op_account]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        err(result.stderr.strip() or "op read failed")
        fail(f"Error: Failed to retrieve {what} from 1Password")
    return result.stdout.strip()


def fetch_token_from_1password(profile_name: str, profile: dict) -> str:
    gh = profile.get("github") or {}
    op_ref = gh.get("token", "")
    if not op_ref:
        fail(f"Error: [github].token not set in profile '{profile_name}'")
    return op_read(op_ref, gh.get("op_account", ""), f"GitHub token (profile: {profile_name})")


# Env bundles: [env.<name>] tables in CONFIG_FILE, activated per launch with
# `-e <name>`. A bundle is a set of environment variables for a tool inside the
# sandbox, most often one that would otherwise need the (denied) Keychain, so
# the credential is resolved on the host instead and handed over as a variable.
# Inside a bundle only these keys are read; anything else is a typo and aborts.
ENV_BUNDLE_KEYS = {
    "vars",        # plain values written in the file; ~ and $VARS expand on the host
    "op",          # 1Password references (op://...), resolved on the host and cached
    "pass",        # names of host variables forwarded unchanged
    "op_account",  # the 1Password account for the op references
}
# Variables sc itself sets from other flags; a bundle naming one would silently
# win or lose depending on ordering, so it is rejected instead.
RESERVED_ENV_VARS = {"GITHUB_TOKEN", "AWS_PROFILE"}


def env_bundle_names(config: dict) -> list[str]:
    return sorted(name for name, spec in (config.get("env") or {}).items() if isinstance(spec, dict))


def env_bundle(config: dict, name: str) -> dict:
    """The [env.NAME] table of CONFIG. Aborts listing the defined bundles when
    NAME is not one, and on a key that is not in ENV_BUNDLE_KEYS."""
    bundles = config.get("env") or {}
    spec = bundles.get(name)
    if not isinstance(spec, dict):
        names = env_bundle_names(config)
        listed = ", ".join(names) if names else "(none)"
        fail(f"Error: no [env.{name}] in {CONFIG_FILE}. Defined bundles: {listed}")
    unknown = sorted(set(spec) - ENV_BUNDLE_KEYS)
    if unknown:
        fail(f"Error: [env.{name}] in {CONFIG_FILE} has unknown key(s) {', '.join(unknown)}; "
             f"allowed: {', '.join(sorted(ENV_BUNDLE_KEYS))}")
    return spec


def _string_table(bundle: dict, name: str, key: str) -> dict[str, str]:
    table = bundle.get(key) or {}
    if not isinstance(table, dict) or not all(isinstance(v, str) for v in table.values()):
        fail(f"Error: [env.{name}.{key}] in {CONFIG_FILE} must map variable names to strings")
    return {str(k): v for k, v in table.items()}


def bundle_env_vars(bundle: dict, name: str) -> dict[str, str]:
    """[env.NAME.vars] with ~ and $VARS expanded against the host environment."""
    return {
        k: os.path.expanduser(os.path.expandvars(v))
        for k, v in _string_table(bundle, name, "vars").items()
    }


def bundle_env_op(bundle: dict, name: str) -> dict[str, str]:
    """[env.NAME.op]: variable name -> op:// reference."""
    refs = _string_table(bundle, name, "op")
    bad = sorted(k for k, v in refs.items() if not v.startswith("op://"))
    if bad:
        fail(f"Error: [env.{name}.op] in {CONFIG_FILE}: {', '.join(bad)} must be op:// references")
    return refs


def bundle_env_pass(bundle: dict, name: str) -> list[str]:
    names = bundle.get("pass") or []
    if not isinstance(names, list) or not all(isinstance(v, str) for v in names):
        fail(f"Error: [env.{name}].pass in {CONFIG_FILE} must be a list of variable names")
    return list(names)


def bundle_op_account(bundle: dict) -> str:
    return str(bundle.get("op_account") or "")


def check_bundle_var_names(bundle_vars: dict[str, list[str]]) -> None:
    """Abort if a variable is named twice across BUNDLE_VARS (bundle name ->
    every variable it sets, from all three tables) or collides with one sc sets
    itself. Two definitions of one variable have no right answer."""
    owners: dict[str, list[str]] = {}
    for bundle, names in bundle_vars.items():
        for var in names:
            owners.setdefault(var, []).append(bundle)
    for var, bundles in sorted(owners.items()):
        if var in RESERVED_ENV_VARS:
            fail(f"Error: [env.{bundles[0]}] sets {var}, which sc manages itself (see -a and profiles)")
        if len(bundles) > 1:
            where = ", ".join(f"[env.{b}]" for b in bundles)
            fail(f"Error: {var} is defined more than once: {where}")


def resolve_bundle_secrets(name: str, bundle: dict) -> dict[str, str]:
    """The [env.NAME.op] variables with their values, from the Keychain cache
    when fresh, else from 1Password (and cached). Never logs a value."""
    account = bundle_op_account(bundle)
    out: dict[str, str] = {}
    for var, ref in bundle_env_op(bundle, name).items():
        label = f"Env: {var}"
        value = cached_secret(ENV_SECRET_SERVICE, f"{name}/{var}", label)
        if value is None:
            value = op_read(ref, account, f"{var} ([env.{name}])")
            cache_secret(ENV_SECRET_SERVICE, f"{name}/{var}", value, label)
        out[var] = value
    return out


def fzf_pick(options: list[str], prompt: str) -> str:
    proc = subprocess.run(
        ["fzf", f"--prompt={prompt}"],
        input="\n".join(options),
        text=True, capture_output=True,
    )
    return proc.stdout.strip()


def compress_home(path: str) -> str:
    """Replace a leading $HOME with ~ for portability across machines."""
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def expand_home(path: str) -> str:
    return str(Path(path).expanduser())


def permission_flags(yes: bool, mode: str, yolo_flag: str) -> list[str]:
    """Flags to prepend to the agent's argv for -y / -m. Not used with --shell."""
    if yes:
        return [yolo_flag]
    if mode:
        return ["--permission-mode", mode]
    return []


def agent_spec(codex: bool) -> tuple[str, str, str]:
    """Return (binary, config_dir, yolo_flag) for the selected agent."""
    if codex:
        return (
            "codex",
            str(Path("~/.codex").expanduser()),
            "--dangerously-bypass-approvals-and-sandbox",
        )
    return (
        "claude",
        str(Path("~/.claude").expanduser()),
        "--dangerously-skip-permissions",
    )


# safehouse's claude/codex agent profiles auto-inject its keychain integration
# (55-integrations-optional/keychain.sb) because agents may store their own
# OAuth creds in the login keychain — but that opens the WHOLE login keychain,
# notably gh's keyring OAuth token, which defeats the restricted GITHUB_TOKEN
# the profile injects. These denies are appended after the generated policy so
# they win over the integration's allows (Seatbelt: last match wins). trustd
# stays allowed — it does TLS trust evaluation, not credential storage.
# Claude Code falls back to ~/.claude/.credentials.json. Escape hatch: sc --keychain.
def keychain_deny_profile_text() -> str:
    home = str(Path.home())
    return f'''\
;; sc: deny macOS Keychain access (see sc --help, --keychain to re-allow).
(deny mach-lookup
    (global-name "com.apple.SecurityServer")
    (global-name "com.apple.securityd.xpc")
    (global-name "com.apple.secd")
    (global-name "com.apple.security.agent")
    (global-name "com.apple.security.authhost"))
(deny file-read* file-write*
    (subpath "{home}/Library/Keychains"))
'''


def write_keychain_deny_profile() -> str | None:
    """Write the keychain-deny fragment for safehouse --append-profile and
    return its path; None off-darwin. Overwritten on every launch."""
    if sys.platform != "darwin":
        return None
    path = Path(tempfile.gettempdir()) / "sc-deny-keychain.sb"
    path.write_text(keychain_deny_profile_text())
    return str(path)


# safehouse resolves every --add-dirs path to its realpath and emits its
# "ancestor directory literals" for that *resolved* chain only. Seatbelt checks
# the path as the process wrote it, so a grant on ~/Tresorit/.../sc leaves
# ~/.config/sc — a dotfiles symlink pointing at it — denied: the kernel cannot
# read the link it has to follow. Nothing inside the sandbox can widen the
# policy afterwards, so the bridge has to be laid at launch.
#
# The fragment below mirrors safehouse's own trick: `file-read*` on a `literal`
# grants readdir on that one directory entry, not recursive access under it.
# Read only, and never on the resolved target (safehouse already grants that at
# whatever access level was asked for) — writing *through* the link lands on the
# target, which carries its own grant.
def symlinked_names(named_paths: list[str]) -> list[str]:
    """The entries of NAMED_PATHS that reach their target through a symlink —
    expanded, but deliberately not resolved. A path that already is its own
    realpath needs no bridge: safehouse's own grant covers it."""
    out: list[str] = []
    for raw in named_paths:
        try:
            written = os.path.abspath(Path(os.path.expandvars(str(raw))).expanduser())
            if not os.path.exists(written) or os.path.realpath(written) == written:
                continue
        except OSError:
            continue
        if written not in out:
            out.append(written)
    return sorted(out)


def symlink_read_literals(named_paths: list[str]) -> list[str]:
    """Path prefixes to allow `file-read*` on so NAMED_PATHS keep working under
    the names they were written with: every symlinked name plus each of its
    ancestors, because the kernel walks the whole chain and the symlink may sit
    anywhere along it."""
    out: list[str] = []
    for name in symlinked_names(named_paths):
        p = Path(name)
        for prefix in [p, *p.parents]:
            if str(prefix) not in out:
                out.append(str(prefix))
    return sorted(out)


def symlink_paths_profile_text(literals: list[str]) -> str:
    rules = "\n".join(f'    (literal "{p}")' for p in literals)
    return f''';; sc: keep symlinked grants reachable by the path they were written with.
;; safehouse grants the realpath; Seatbelt matches the path as written, so the
;; link and its ancestors need readdir/readlink. Read-only, one dir entry each.
(allow file-read*
{rules})
'''


def write_symlink_paths_profile(named_paths: list[str]) -> str | None:
    """Write the symlink-bridge fragment for safehouse --append-profile and
    return its path; None off-darwin or when no granted dir is symlinked."""
    if sys.platform != "darwin":
        return None
    literals = symlink_read_literals(named_paths)
    if not literals:
        return None
    path = Path(tempfile.gettempdir()) / "sc-symlink-paths.sb"
    path.write_text(symlink_paths_profile_text(literals))
    return str(path)


# safehouse allows network-bind only for `(local ip)` plus a handful of named
# unix sockets (Chrome/Codex/VS Code/agent-browser singletons). Tools that talk
# to their own helper processes over a unix socket in the per-user temp dir are
# therefore killed at bind() with "bind: operation not permitted" — writing the
# socket file is allowed, listening on it is not.
#
# Each entry is a Seatbelt regex for the socket file's name — a bare basename,
# or a `subdir/name` tail when the tool nests its sockets — matched at any depth
# under /var/folders/<x>/<y>/T/. Add new cases as data here; do NOT widen this
# to the whole temp dir: safehouse deliberately denies outbound to
# vscode-git-*.sock / vscode-ipc-*.sock in that same dir (a host VS Code binds
# those, outside the sandbox's trust boundary), and an appended blanket allow
# would override those denies — Seatbelt takes the last match.
TEMP_UNIX_SOCKET_NAMES = [
    # hashicorp/go-plugin's provider handshake: it listens on
    # $TMPDIR/plugin<random-digits> on every non-Windows platform. Used by
    # terraform (`plan`/`apply`/`test` all launch providers), terragrunt,
    # packer and vault. Without this, terraform dies with
    # "plugin init error: listen unix …/T/plugin123: bind: operation not permitted".
    r"plugin[0-9]+",
    # @playwright/cli's daemon: the client spawns a browser daemon and reaches
    # it over $TMPDIR/playwright-cli/<workspace-hash>/<session>.sock, plus a
    # devtools singleton at $TMPDIR/playwright-cli/devtools.sock. The session
    # name comes from `--session`, so the leaf stays a wildcard. Without this,
    # `playwright-cli open` dies with "listen EPERM … /default.sock".
    r"playwright-cli/([^/]+/)?[^/]+\.sock",
]


def temp_unix_socket_profile_text(
    names_list: list[str] | None = None,
) -> str:
    """Seatbelt fragment allowing bind/listen/connect on the temp-dir unix
    sockets listed in TEMP_UNIX_SOCKET_NAMES."""
    names = "|".join(names_list if names_list is not None else TEMP_UNIX_SOCKET_NAMES)
    # Match the per-user darwin temp dir by shape, not by literal path, so the
    # fragment is machine-independent (mirrors safehouse's own /var/folders rules).
    pattern = r"^(/private)?/var/folders/[^/]+/[^/]+/T/(.*/)?(" + names + r")$"
    return f'''\
;; sc: allow helper-process unix sockets inside the per-user temp dir
;; (see TEMP_UNIX_SOCKET_NAMES in sc). safehouse's network-bind is
;; ip-only, which breaks tools that IPC over a socket in $TMPDIR.
(allow network-bind network-inbound
    (local unix-socket (path-regex #"{pattern}")))
(allow network-outbound
    (remote unix-socket (path-regex #"{pattern}")))
'''


def write_temp_unix_socket_profile() -> str | None:
    """Write the temp-dir unix-socket fragment for safehouse --append-profile
    and return its path; None off-darwin. Overwritten on every launch."""
    if sys.platform != "darwin":
        return None
    path = Path(tempfile.gettempdir()) / "sc-temp-unix-sockets.sb"
    path.write_text(temp_unix_socket_profile_text())
    return str(path)


CLAUDE_STATE_FILE = Path.home() / ".claude.json"
TEMP_PARENT = Path(tempfile.gettempdir()) / "sc"


def is_claude_trusted(path: str, state_file: Path = CLAUDE_STATE_FILE) -> bool:
    """True if PATH is covered by a folder-trust record in ~/.claude.json.
    Claude Code accepts trust from the dir itself or ANY ancestor, keyed by
    realpath (verified against the trust gate in claude 2.1.220), so walk up.
    Missing/corrupt state file counts as untrusted (fail closed)."""
    try:
        data = json.loads(state_file.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return False
    p = os.path.realpath(path)
    while True:
        if isinstance(projects.get(p), dict) and projects[p].get("hasTrustDialogAccepted") is True:
            return True
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent


def require_trusted_temp_parent() -> None:
    """Stop with setup instructions unless Claude Code trusts TEMP_PARENT.
    sc never writes to ~/.claude.json; the user grants trust once per machine
    by accepting claude's folder-trust prompt inside TEMP_PARENT."""
    if is_claude_trusted(str(TEMP_PARENT)):
        return
    TEMP_PARENT.mkdir(parents=True, exist_ok=True)  # so the cd below works
    fail(
        f"Error: Claude Code has not trusted the sc temp parent dir: {TEMP_PARENT}\n"
        "Without it, every `sc -t` launch shows the folder-trust prompt.\n"
        "One-time setup (per machine):\n"
        f"    cd {TEMP_PARENT} && claude\n"
        "Accept the folder-trust prompt, exit claude, then re-run sc -t."
    )


def make_temp_dir() -> str:
    """Create a fresh temp dir under TEMP_PARENT and return its path."""
    TEMP_PARENT.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(dir=TEMP_PARENT)


def load_history() -> list[dict]:
    if not HISTORY_FILE.is_file():
        return []
    entries: list[dict] = []
    for line in HISTORY_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("dir"), str) and isinstance(obj.get("args"), list):
            entries.append({"dir": obj["dir"], "args": [str(a) for a in obj["args"]]})
    return entries


def record_launch(raw_args: list[str], cwd: str | None = None) -> None:
    entry = {"dir": compress_home(cwd or os.getcwd()), "args": list(raw_args)}
    entries = [e for e in load_history() if not (e["dir"] == entry["dir"] and e["args"] == entry["args"])]
    entries.insert(0, entry)
    del entries[HISTORY_MAX:]
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text("".join(json.dumps(e) + "\n" for e in entries))


def history_display(entry: dict) -> str:
    return f"{entry['dir']}\t{shlex.join(entry['args']) or '(no flags)'}"


def run_history_picker() -> "NoReturn":  # type: ignore[name-defined]
    entries = load_history()
    if not entries:
        fail("No sc history yet.")
    by_line: dict[str, dict] = {}
    lines: list[str] = []
    for e in entries:
        line = history_display(e)
        if line not in by_line:        # dedup already guarantees this, but be safe
            by_line[line] = e
            lines.append(line)
    choice = fzf_pick(lines, "Resume sc: ")
    if not choice:
        fail("Nothing selected.")
    entry = by_line.get(choice)
    if entry is None:
        fail("Selection did not match a history entry.")
    target = expand_home(entry["dir"])
    if not Path(target).is_dir():
        fail(f"Error: directory no longer exists: {target}")
    os.chdir(target)
    sc_exe = shutil.which("sc") or str(Path(__file__).resolve())
    os.execv(sc_exe, [sc_exe, *entry["args"]])


USAGE = """\
Usage: safe-claude.py [options] [-- <claude args>]

Run claude inside the safehouse sandbox profile.

Options:
  -p, --profile [name]
                       A profile bundles a GitHub token (from 1Password) and
                       a list of extra directories to auto-mount. Defined
                       per file at ~/.config/sc/profiles/<name>.toml.
                       Without a name, fzf-pick. Choice is persisted.
                       Usually unnecessary: a profile with a [match] dirs list
                       covering the current dir is selected automatically.
  -P, --no-profile     Do not use any profile (no token, no profile dirs).
                       Also the way to override a [match] for one launch.
  --codex              Run codex instead of claude. Mounts ~/.codex (rw) instead
                       of ~/.claude, and maps -y to codex's
                       --dangerously-bypass-approvals-and-sandbox.
  -s, --shell          Launch your shell ($SHELL, default zsh) inside the
                       sandbox instead of the agent, for debugging mounts and
                       directories. All mounts/profile/dirs apply. Args after
                       `--` are passed to the shell (e.g. `sc -s -- -c 'ls /'`).
  -a, --aws            Mount ~/.aws read/write into the sandbox and pass
                       AWS_PROFILE through. Needed for SSO/role caches that
                       AWS CLI writes back to ~/.aws/{sso,cli}/cache.
                       If AWS_PROFILE is set, its credentials must resolve
                       (`aws configure export-credentials`) or sc refuses to
                       launch, so an expired SSO login is reported here instead
                       of surfacing inside the sandbox. An unset AWS_PROFILE is
                       fine — pick one inside the session.
  -k, --keychain       Allow macOS Keychain access inside the sandbox. By
                       default sc denies it (securityd IPC + keychain files),
                       so login-keychain secrets — e.g. gh's keyring OAuth
                       token — are unreachable and the profile's GITHUB_TOKEN
                       is the only GitHub credential. Use this flag if a tool
                       inside the sandbox genuinely needs the Keychain (e.g.
                       Claude Code login stored there instead of
                       ~/.claude/.credentials.json).
  -y                   Pass the agent's "skip all prompts" flag
                       (claude: --dangerously-skip-permissions; see --codex).
  -m, --mode [MODE]    Pass `--permission-mode MODE` to claude. With no value,
                       MODE defaults to `auto`, so `sc -m` == `sc -m auto`.
                       The value is not validated here — claude
                       rejects an unknown mode and lists the valid ones.
                       Overrides permissions.defaultMode in settings.json for
                       this launch. Mutually exclusive with -y, and claude-only
                       (codex has no equivalent).
  -t, --temp           Create a fresh temp dir under $TMPDIR/sc, cd into it,
                       mount it read/write, and launch claude there. No repo
                       is auto-shared (the temp dir isn't a git repo). For
                       claude launches, $TMPDIR/sc must have been trusted once
                       (run claude there and accept the folder-trust prompt);
                       otherwise sc stops with setup instructions.
  -r, --repo-root      cd into the git repo root before launching, instead of
                       the current dir. The repo root is auto-shared rw either
                       way; this only changes claude's starting directory.
  -e, --env NAME       Hand the sandbox the environment variables of the
                       [env.NAME] bundle in config.toml (below). Repeatable.
                       Plain values, 1Password secrets (resolved on the host
                       and Keychain-cached like the GitHub token) and forwarded
                       host variables. Independent of the profile, so
                       `sc -P -e datadog` works.
  -dr  PATH            safehouse --add-dirs-ro=PATH (read-only). Repeatable.
  -dw  PATH            safehouse --add-dirs=PATH    (read/write). Repeatable.
                       One-off grants. For dirs you want every time you work
                       somewhere, use [group] in config.toml below instead — the
                       sandbox cannot be widened once a session is running, so a
                       forgotten -dw costs a relaunch.
  -H, --history        fzf-pick a previous launch (dir + args) and re-run it
                       in that directory. History is recorded automatically
                       on every launch to ~/.config/sc/history.jsonl, which
                       syncs across machines via the dotfiles symlink.
  --warm-token         Fetch the profile's GitHub token and the 1Password
                       secrets of any -e bundles (or use the cache), store
                       them in Keychain, then exit. Does not launch claude.
                       Errors if there is nothing to warm.
  -h, --help           Show this help and exit.
  --                   End wrapper options; remaining args go to claude
                       (e.g. `sc -- -c` to resume, `sc -- --help` for
                       claude's own help). Unknown args before `--` are
                       rejected.

Profile selection, in order: -P, then -p, then the profile whose [match] dirs
cover the current directory, then the persisted choice from the last -p.

Environment:
  GITHUB_TOKEN_CACHE_TTL    Keychain cache TTL in seconds (default 36000).
                            Set to 0 to disable caching.
  PROFILE_DIR               Override config dir (default ~/.config/sc).
  SAFE_CLAUDE_PROFILES_DIR  Override profiles dir (default $PROFILE_DIR/profiles).
  SAFEHOUSE_BIN             Override safehouse binary (default `safehouse` on PATH).

Profile file (~/.config/sc/profiles/<name>.toml):
  [match] dirs                  Dirs this profile applies in, so no -p is
                                needed there. A dir covers everything below it;
                                the deepest match across all profiles wins.

    [match]
    dirs = ["~/src/work", "~/src/work-sandbox"]

  [github] token, op_account   1Password ref for the GitHub token.
  [dirs] ro, rw                 Lists of dirs to mount. Both ~ and $VARS are
                                expanded, e.g. rw = ["$OBSIDIAN_NOTES_DIR"].
  [env] pass                    Env var names to forward into the sandbox via
                                safehouse --env-pass, e.g. ["OBSIDIAN_NOTES_DIR"].

Config file (~/.config/sc/config.toml), applies to every launch, any profile:
  [group.<name>] ro or rw       A set of dirs that belong together. Launching
                                in any of them — or anywhere below one — mounts
                                all of them, at that one access level. Every
                                activated group contributes; grants union.
                                ~ and $VARS are expanded.

    [group.repos]
    ro = ["~/src"]                       # every repo, read-only, always
    [group.myproject]
    rw = ["~/src/myproject", "~/scratch"]  # in either one, get both

  [env.<name>]                  An env bundle, activated with `-e <name>`. Only
                                the bundle name is free; inside it sc reads:
    vars                          plain values (~ and $VARS expand on the host)
    op                            1Password op:// references, resolved on the
                                  host and Keychain-cached (GITHUB_TOKEN_CACHE_TTL)
    pass                          names of host variables forwarded unchanged
    op_account                    the 1Password account for the op references

    [env.datadog]
    op_account = "my-team.1password.eu"
    [env.datadog.vars]
    DD_SITE = "datadoghq.eu"
    DD_TOKEN_STORAGE = "file"            # pup: skip the (denied) Keychain
    PUP_CONFIG_DIR = "$TMPDIR/pup"       # pup: a config dir it can write
    [env.datadog.op]
    DD_API_KEY = "op://Vault/Datadog/api-key"
    DD_APP_KEY = "op://Vault/Datadog/app-key"
"""


def parse_args(argv: list[str]) -> tuple[bool, str, bool, bool, bool, bool, bool, bool, bool, bool, bool, bool, list[str], list[str], str, list[str], list[str]]:
    """Return (select_profile, profile_name, no_profile, aws, keychain, yes, warm_token, history, temp, codex, repo_root, shell, ro_dirs, rw_dirs, mode, env_bundles, passthrough).

    passthrough stays last so tests (and callers) can rely on [-1].
    """
    env_bundles: list[str] = []
    select_profile = False
    profile_name = ""
    no_profile = False
    aws = False
    keychain = False
    yes = False
    warm_token = False
    history = False
    temp = False
    codex = False
    repo_root = False
    shell = False
    ro_dirs: list[str] = []
    rw_dirs: list[str] = []
    passthrough: list[str] = []
    mode = ""

    def take_value(flag: str, idx: int) -> tuple[str, int]:
        if idx + 1 >= len(argv):
            fail(f"Error: {flag} requires a path argument")
        return argv[idx + 1], idx + 2

    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(USAGE, end="")
            sys.exit(0)
        elif a in ("-p", "--profile"):
            select_profile = True
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            if nxt and not nxt.startswith("-") and nxt != "--":
                profile_name = nxt
                i += 2
            else:
                i += 1
        elif a in ("-P", "--no-profile"):
            no_profile = True
            i += 1
        elif a in ("-a", "--aws"):
            aws = True
            i += 1
        elif a in ("-k", "--keychain"):
            keychain = True
            i += 1
        elif a == "-y":
            yes = True
            i += 1
        elif a in ("-m", "--mode"):
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            if nxt and not nxt.startswith("-") and nxt != "--":
                mode = nxt
                i += 2
            else:
                mode = "auto"
                i += 1
        elif a == "--warm-token":
            warm_token = True
            i += 1
        elif a in ("-H", "--history"):
            history = True
            i += 1
        elif a in ("-t", "--temp"):
            temp = True
            i += 1
        elif a in ("-r", "--repo-root"):
            repo_root = True
            i += 1
        elif a == "--codex":
            codex = True
            i += 1
        elif a in ("-s", "--shell"):
            shell = True
            i += 1
        elif a == "-dr":
            value, i = take_value(a, i)
            ro_dirs.append(value)
        elif a == "-dw":
            value, i = take_value(a, i)
            rw_dirs.append(value)
        elif a in ("-e", "--env"):
            value, i = take_value(a, i)
            if value not in env_bundles:
                env_bundles.append(value)
        elif a == "--":
            passthrough.extend(argv[i + 1:])
            break
        else:
            fail(f"Error: unknown argument: {a}\nPass arguments to claude after `--` (e.g. `sc -- {a}`).")

    if mode and yes:
        fail("Error: -m/--mode and -y are mutually exclusive (-y already forces a bypass mode).")
    if mode and codex:
        fail("Error: -m/--mode is claude-only; codex has no --permission-mode equivalent.")

    return select_profile, profile_name, no_profile, aws, keychain, yes, warm_token, history, temp, codex, repo_root, shell, ro_dirs, rw_dirs, mode, env_bundles, passthrough


def resolve_profile(select_profile: bool, profile_name: str, no_profile: bool, cwd: str) -> tuple[str | None, str]:
    """The (profile name, where it came from) for this launch, name None for no
    profile. Order: -P, then -p, then a [match] on CWD, then the persisted choice.

    A directory match beats the persisted profile on purpose: `active-profile`
    holds whatever the last -p picked, possibly in an unrelated project, and
    letting that shadow the match would leave the flag exactly as mandatory as
    before. -p still wins (and persists), -P still opts out."""
    if no_profile:
        return None, "--no-profile"
    if select_profile:
        if profile_name:
            if not (PROFILES_DIR / f"{profile_name}.toml").is_file():
                err(f"Error: profile not found: {profile_name}")
                err("Available profiles:")
                for p in discover_profiles():
                    err(f"  {p}")
                sys.exit(1)
            PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
            PROFILE_FILE.write_text(profile_name + "\n")
            return profile_name, "-p"
        profiles = discover_profiles()
        if not profiles:
            fail(f"No profiles found ({PROFILES_DIR}/*.toml)")
        choice = fzf_pick(["none", *profiles], "Select profile: ")
        if not choice:
            fail("No profile selected.")
        PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_FILE.write_text(choice + "\n")
        return choice, "picked"
    matched = match_profile_for(cwd)
    if matched is not None:
        name, member = matched
        return name, f"matched {compress_home(member)}"
    if PROFILE_FILE.is_file():
        return (PROFILE_FILE.read_text().strip() or None), "persisted"
    return None, ""


def main() -> None:
    select_profile, profile_name, no_profile, aws, keychain, yes, warm_token, history, temp, codex, repo_root_flag, shell, ro_dirs, rw_dirs, mode, env_bundle_names_wanted, passthrough = parse_args(sys.argv[1:])

    agent_bin, agent_cfg, yolo_flag = agent_spec(codex)
    if shell:
        agent_bin = os.environ.get("SHELL") or "zsh"

    invocation_cwd = os.getcwd()

    if history:
        run_history_picker()  # chdir + exec, never returns

    # Matched against the invocation cwd, like the config dir groups: which
    # project you are in is where you ran sc, not where -r/-t moved it.
    profile_id, profile_origin = resolve_profile(select_profile, profile_name, no_profile, invocation_cwd)
    origin_note = f" ({profile_origin})" if profile_origin else ""

    # Bundles are validated up front so a typo in -e or in config.toml stops the
    # launch before any 1Password prompt.
    bundles = {name: env_bundle(load_config(), name) for name in env_bundle_names_wanted}
    check_bundle_var_names({
        name: list(bundle_env_vars(b, name)) + list(bundle_env_op(b, name)) + bundle_env_pass(b, name)
        for name, b in bundles.items()
    })

    if warm_token:
        has_profile_token = bool(profile_id and profile_id != "none"
                                 and (load_profile(profile_id).get("github") or {}).get("token"))
        if not has_profile_token and not any(bundle_env_op(b, n) for n, b in bundles.items()):
            fail("Error: --warm-token found nothing to warm: no profile with a [github].token "
                 "(use -p) and no -e bundle with [env.<name>.op] entries")
        if has_profile_token:
            err(f"Profile: {profile_id}{origin_note}")
            token = get_cached_token(profile_id)
            if token is None:
                token = fetch_token_from_1password(profile_id, load_profile(profile_id))
                cache_token(profile_id, token)
            err(f"GitHub token: ...{token[-5:]}")
        for name, b in bundles.items():
            for var, value in resolve_bundle_secrets(name, b).items():
                err(f"Env: {var} ...{value[-4:]} ([env.{name}])")
        return

    safehouse_bin = shutil.which(SAFEHOUSE) or (SAFEHOUSE if Path(SAFEHOUSE).is_file() else None)
    if not safehouse_bin:
        fail(f"safe-claude: safehouse binary not found (tried '{SAFEHOUSE}')")
    require_safehouse_version(safehouse_bin)

    aws_profile = check_aws_ready() if aws else ""

    if not shell:
        passthrough[:0] = permission_flags(yes, mode, yolo_flag)

    # Create the temp dir and cd into it before repo detection below, so the
    # git rev-parse runs in the (repo-less) temp dir and shares nothing extra.
    temp_dir = ""
    if temp:
        if not codex and not shell:
            require_trusted_temp_parent()
        temp_dir = make_temp_dir()
        os.chdir(temp_dir)
        err(f"Temp dir: {temp_dir} (rw, cwd)")

    err(f"Agent: {agent_bin}")

    profile: dict = {}
    if profile_id and profile_id != "none":
        err(f"Profile: {profile_id}{origin_note}")
        profile = load_profile(profile_id)
    else:
        err(f"Profile: (none){origin_note}")

    profile_ro, profile_rw = profile_dirs(profile)
    # Dir groups from the global config, matched against where sc was invoked
    # (before -r/-t moved us): "I am in one of these, give me all of them".
    config = load_config()
    config_ro, config_rw = config_dirs_for(config, invocation_cwd)

    # Every dir as it was *written* — profile [dirs], activated groups, -dr/-dw.
    # The grants above are realpaths; these names are what the sandbox also has
    # to accept, and a symlink among them needs a bridge (see below).
    named_dirs = profile_named_dirs(profile) + list(ro_dirs) + list(rw_dirs)
    for _, _, members in activated_groups(config, invocation_cwd):
        named_dirs += members

    default_ro_dirs = [
        str(Path("~/.gitconfig").expanduser()),
        str(Path("~/.ssh").expanduser()),
        str(Path("~/.local/bin").expanduser()),
        str(Path("~/.local/share/uv/tools").expanduser()),
    ]
    # ~/.codex is mounted rw when --codex is used (agent_cfg); otherwise share
    # it read-only so claude can read codex config/auth without writing to it.
    if not codex:
        default_ro_dirs.append(str(Path("~/.codex").expanduser()))
    ro_dirs = profile_ro + config_ro + default_ro_dirs + ro_dirs

    default_rw_dirs = [
        agent_cfg,
        str(Path("~/.terraform.versions").expanduser()),
        # Terraform CLI config dir (credentials, plugin-cache/, etc.). Mounted
        # rw so terraform/terragrunt can populate the plugin cache from inside
        # the sandbox.
        str(Path("~/.terraform.d").expanduser()),
    ]
    # sc's own config (profiles, history, persisted profile). PROFILE_DIR is
    # often a symlink (a dotfiles link into a synced folder), so mount the
    # resolved real target — mounting the link path alone leaves the contents
    # inaccessible inside the sandbox. The link path itself is bridged below, so
    # ~/.config/sc keeps working as a name too.
    try:
        if PROFILE_DIR.exists():
            default_rw_dirs.append(str(PROFILE_DIR.resolve()))
            named_dirs.append(str(PROFILE_DIR))
    except OSError:
        pass
    repo_root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    ).stdout.strip()
    if repo_root:
        default_rw_dirs.append(repo_root)
        err(f"Repo: sharing {repo_root} (rw)")
        if repo_root_flag:
            os.chdir(repo_root)
            err(f"Repo: cd into {repo_root}")
    elif repo_root_flag:
        fail("Error: --repo-root used outside a git repo")
    rw_dirs = profile_rw + config_rw + default_rw_dirs + rw_dirs

    if temp_dir:
        rw_dirs.append(temp_dir)

    if aws:
        rw_dirs.append(str(Path("~/.aws").expanduser()))

    named_dirs += default_ro_dirs + default_rw_dirs

    safehouse_args: list[str] = [f"--enable={f}" for f in SAFEHOUSE_FEATURES]
    unix_socket_profile = write_temp_unix_socket_profile()
    if unix_socket_profile:
        safehouse_args.append(f"--append-profile={unix_socket_profile}")
    # Before the keychain deny below, so that deny stays the last match on the
    # paths it covers — an appended allow would otherwise override it.
    symlink_profile = write_symlink_paths_profile(named_dirs)
    if symlink_profile:
        safehouse_args.append(f"--append-profile={symlink_profile}")
        names = ", ".join(compress_home(p) for p in symlinked_names(named_dirs))
        err(f"Dirs: symlinked paths kept usable by name: {names}")
    if keychain:
        err("Keychain: allowed (--keychain)")
    else:
        keychain_profile = write_keychain_deny_profile()
        if keychain_profile:
            safehouse_args.append(f"--append-profile={keychain_profile}")
            err("Keychain: denied (re-allow with --keychain)")
    if ro_dirs:
        safehouse_args.append(f"--add-dirs-ro={':'.join(ro_dirs)}")
    if rw_dirs:
        safehouse_args.append(f"--add-dirs={':'.join(rw_dirs)}")

    if aws:
        safehouse_args.append("--env-pass=AWS_PROFILE")
        if aws_profile:
            err(f"AWS: sharing ~/.aws (rw), AWS_PROFILE={aws_profile} (credentials OK)")
        else:
            err("AWS: sharing ~/.aws (rw), AWS_PROFILE unset (set one in the session)")

    for var in profile_env_pass(profile):
        safehouse_args.append(f"--env-pass={var}")
        err(f"Env: passing {var}={os.environ.get(var, '(unset)')} through")

    # -e bundles: every variable is exported into sc's own environment and
    # forwarded by name, the same route GITHUB_TOKEN takes below. Secret values
    # are never printed, only their last characters.
    for name, b in bundles.items():
        plain = bundle_env_vars(b, name)
        for var, value in plain.items():
            os.environ[var] = value
            safehouse_args.append(f"--env-pass={var}")
        if plain:
            err(f"Env: {name} -> " + ", ".join(f"{k}={v}" for k, v in plain.items()))
        for var in bundle_env_pass(b, name):
            safehouse_args.append(f"--env-pass={var}")
            err(f"Env: {name} -> passing {var}={os.environ.get(var, '(unset)')} through")
        for var, value in resolve_bundle_secrets(name, b).items():
            os.environ[var] = value
            safehouse_args.append(f"--env-pass={var}")
            err(f"Env: {name} -> {var} ...{value[-4:]} (1Password)")

    if profile and (profile.get("github") or {}).get("token"):
        token = get_cached_token(profile_id)
        if token is None:
            token = fetch_token_from_1password(profile_id, profile)
            cache_token(profile_id, token)
        os.environ["GITHUB_TOKEN"] = token
        safehouse_args.append("--env-pass=GITHUB_TOKEN")
        err(f"GitHub token: ...{token[-5:]}")

    record_launch(sys.argv[1:], cwd=invocation_cwd)

    cmd = [safehouse_bin, *safehouse_args, "--", agent_bin, *passthrough]
    err("Launching: " + " ".join([SAFEHOUSE, *safehouse_args, "--", agent_bin, *passthrough]))
    os.execvp(safehouse_bin, cmd)


if __name__ == "__main__":
    main()
