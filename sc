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


def discover_profiles() -> list[str]:
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(p.stem for p in PROFILES_DIR.glob("*.toml") if p.is_file())


def get_cached_token(profile: str) -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        stored = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", profile, "-w"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        return None
    ts_str, _, token = stored.partition(":")
    if not ts_str or not token:
        return None
    try:
        ts = int(ts_str)
    except ValueError:
        return None
    age = int(time.time()) - ts
    if age >= TTL:
        subprocess.run(
            ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", profile],
            capture_output=True, check=False,
        )
        return None
    remaining = TTL - age
    err(f"GitHub token: cached ({remaining // 3600}h{(remaining % 3600) // 60}m remaining)")
    return token


def cache_token(profile: str, token: str) -> None:
    if TTL == 0:
        return
    if sys.platform != "darwin":
        err("GitHub token: caching not supported on this platform")
        return
    payload = f"{int(time.time())}:{token}"
    rc = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", profile, "-w", payload],
        capture_output=True,
    ).returncode
    err("GitHub token: fetched and cached" if rc == 0 else "GitHub token: caching failed (will retry next run)")


def load_profile(name: str) -> dict:
    path = PROFILES_DIR / f"{name}.toml"
    if not path.is_file():
        fail(f"Error: Profile file not found: {path}")
    with path.open("rb") as f:
        return tomllib.load(f)


def profile_dirs(profile: dict) -> tuple[list[str], list[str]]:
    """Return (ro, rw) lists of existing absolute paths from profile [dirs] section."""
    def resolve(paths: list[str]) -> list[str]:
        out: list[str] = []
        for p in paths:
            expanded = Path(os.path.expandvars(p)).expanduser()
            if not expanded.exists():
                err(f"Profile dir missing, skipping: {p}")
                continue
            out.append(str(expanded))
        return out

    dirs = profile.get("dirs") or {}
    return resolve(dirs.get("ro") or []), resolve(dirs.get("rw") or [])


def profile_env_pass(profile: dict) -> list[str]:
    """Return env var names to forward into the sandbox from [env].pass."""
    env = profile.get("env") or {}
    return [str(v) for v in (env.get("pass") or [])]


def fetch_token_from_1password(profile_name: str, profile: dict) -> str:
    gh = profile.get("github") or {}
    op_ref = gh.get("token", "")
    op_account = gh.get("op_account", "")
    if not op_ref:
        fail(f"Error: [github].token not set in profile '{profile_name}'")
    cmd = ["op", "read", op_ref]
    if op_account:
        cmd += ["--account", op_account]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        err(result.stderr.strip() or "op read failed")
        fail(f"Error: Failed to retrieve GitHub token from 1Password (profile: {profile_name})")
    return result.stdout.strip()


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


# safehouse allows network-bind only for `(local ip)` plus a handful of named
# unix sockets (Chrome/Codex/VS Code/agent-browser singletons). Tools that talk
# to their own helper processes over a unix socket in the per-user temp dir are
# therefore killed at bind() with "bind: operation not permitted" — writing the
# socket file is allowed, listening on it is not.
#
# Each entry is a Seatbelt regex for the socket file's *basename* under
# /var/folders/<x>/<y>/T/ (any depth). Add new cases as data here; do NOT widen
# this to the whole temp dir: safehouse deliberately denies outbound to
# vscode-git-*.sock / vscode-ipc-*.sock in that same dir (a host VS Code binds
# those, outside the sandbox's trust boundary), and an appended blanket allow
# would override those denies — Seatbelt takes the last match.
TEMP_UNIX_SOCKET_BASENAMES = [
    # hashicorp/go-plugin's provider handshake: it listens on
    # $TMPDIR/plugin<random-digits> on every non-Windows platform. Used by
    # terraform (`plan`/`apply`/`test` all launch providers), terragrunt,
    # packer and vault. Without this, terraform dies with
    # "plugin init error: listen unix …/T/plugin123: bind: operation not permitted".
    r"plugin[0-9]+",
]


def temp_unix_socket_profile_text(
    basenames: list[str] | None = None,
) -> str:
    """Seatbelt fragment allowing bind/listen/connect on the temp-dir unix
    sockets listed in TEMP_UNIX_SOCKET_BASENAMES."""
    names = "|".join(basenames if basenames is not None else TEMP_UNIX_SOCKET_BASENAMES)
    # Match the per-user darwin temp dir by shape, not by literal path, so the
    # fragment is machine-independent (mirrors safehouse's own /var/folders rules).
    pattern = r"^(/private)?/var/folders/[^/]+/[^/]+/T/(.*/)?(" + names + r")$"
    return f'''\
;; sc: allow helper-process unix sockets inside the per-user temp dir
;; (see TEMP_UNIX_SOCKET_BASENAMES in sc). safehouse's network-bind is
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
  -P, --no-profile     Do not use any profile (no token, no profile dirs).
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
  -t, --temp           Create a fresh temp dir under $TMPDIR/sc, cd into it,
                       mount it read/write, and launch claude there. No repo
                       is auto-shared (the temp dir isn't a git repo). For
                       claude launches, $TMPDIR/sc must have been trusted once
                       (run claude there and accept the folder-trust prompt);
                       otherwise sc stops with setup instructions.
  -r, --repo-root      cd into the git repo root before launching, instead of
                       the current dir. The repo root is auto-shared rw either
                       way; this only changes claude's starting directory.
  -dr  PATH            safehouse --add-dirs-ro=PATH (read-only). Repeatable.
  -dw  PATH            safehouse --add-dirs=PATH    (read/write). Repeatable.
  -H, --history        fzf-pick a previous launch (dir + args) and re-run it
                       in that directory. History is recorded automatically
                       on every launch to ~/.config/sc/history.jsonl, which
                       syncs across machines via the dotfiles symlink.
  --warm-token         Resolve profile, fetch GitHub token (1Password or
                       cache), store in Keychain, then exit. Does not launch
                       claude. Errors if no profile is selected.
  -h, --help           Show this help and exit.
  --                   End wrapper options; remaining args go to claude
                       (e.g. `sc -- -c` to resume, `sc -- --help` for
                       claude's own help). Unknown args before `--` are
                       rejected.

With no -p/-P, the persisted profile (if any) is used.

Environment:
  GITHUB_TOKEN_CACHE_TTL    Keychain cache TTL in seconds (default 36000).
                            Set to 0 to disable caching.
  PROFILE_DIR               Override config dir (default ~/.config/sc).
  SAFE_CLAUDE_PROFILES_DIR  Override profiles dir (default $PROFILE_DIR/profiles).
  SAFEHOUSE_BIN             Override safehouse binary (default `safehouse` on PATH).

Profile file (~/.config/sc/profiles/<name>.toml):
  [github] token, op_account   1Password ref for the GitHub token.
  [dirs] ro, rw                 Lists of dirs to mount. Both ~ and $VARS are
                                expanded, e.g. rw = ["$OBSIDIAN_NOTES_DIR"].
  [env] pass                    Env var names to forward into the sandbox via
                                safehouse --env-pass, e.g. ["OBSIDIAN_NOTES_DIR"].
"""


def parse_args(argv: list[str]) -> tuple[bool, str, bool, bool, bool, bool, bool, bool, bool, bool, bool, bool, list[str], list[str], list[str]]:
    """Return (select_profile, profile_name, no_profile, aws, keychain, yes, warm_token, history, temp, codex, repo_root, shell, ro_dirs, rw_dirs, passthrough)."""
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
        elif a == "--":
            passthrough.extend(argv[i + 1:])
            break
        else:
            fail(f"Error: unknown argument: {a}\nPass arguments to claude after `--` (e.g. `sc -- {a}`).")
    return select_profile, profile_name, no_profile, aws, keychain, yes, warm_token, history, temp, codex, repo_root, shell, ro_dirs, rw_dirs, passthrough


def resolve_profile(select_profile: bool, profile_name: str, no_profile: bool) -> str | None:
    if no_profile:
        return None
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
            return profile_name
        profiles = discover_profiles()
        if not profiles:
            fail(f"No profiles found ({PROFILES_DIR}/*.toml)")
        choice = fzf_pick(["none", *profiles], "Select profile: ")
        if not choice:
            fail("No profile selected.")
        PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_FILE.write_text(choice + "\n")
        return choice
    if PROFILE_FILE.is_file():
        return PROFILE_FILE.read_text().strip() or None
    return None


def main() -> None:
    select_profile, profile_name, no_profile, aws, keychain, yes, warm_token, history, temp, codex, repo_root_flag, shell, ro_dirs, rw_dirs, passthrough = parse_args(sys.argv[1:])

    agent_bin, agent_cfg, yolo_flag = agent_spec(codex)
    if shell:
        agent_bin = os.environ.get("SHELL") or "zsh"

    invocation_cwd = os.getcwd()

    if history:
        run_history_picker()  # chdir + exec, never returns

    profile_id = resolve_profile(select_profile, profile_name, no_profile)

    if warm_token:
        if not profile_id or profile_id == "none":
            fail("Error: --warm-token requires a profile (use -p to select one)")
        err(f"Profile: {profile_id}")
        profile = load_profile(profile_id)
        if not (profile.get("github") or {}).get("token"):
            fail(f"Error: profile '{profile_id}' has no [github].token to warm")
        token = get_cached_token(profile_id)
        if token is None:
            token = fetch_token_from_1password(profile_id, profile)
            cache_token(profile_id, token)
        err(f"GitHub token: ...{token[-5:]}")
        return

    safehouse_bin = shutil.which(SAFEHOUSE) or (SAFEHOUSE if Path(SAFEHOUSE).is_file() else None)
    if not safehouse_bin:
        fail(f"safe-claude: safehouse binary not found (tried '{SAFEHOUSE}')")
    require_safehouse_version(safehouse_bin)

    if yes and not shell:
        passthrough.insert(0, yolo_flag)

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
        err(f"Profile: {profile_id}")
        profile = load_profile(profile_id)
    else:
        err("Profile: (none)")

    profile_ro, profile_rw = profile_dirs(profile)

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
    ro_dirs = profile_ro + default_ro_dirs + ro_dirs

    default_rw_dirs = [
        agent_cfg,
        str(Path("~/.terraform.versions").expanduser()),
        # Terraform CLI config dir (credentials, plugin-cache/, etc.). Mounted
        # rw so terraform/terragrunt can populate the plugin cache from inside
        # the sandbox.
        str(Path("~/.terraform.d").expanduser()),
    ]
    # sc's own config (profiles, history, persisted profile). PROFILE_DIR is
    # often a symlink, so mount the resolved real target — mounting the link
    # path alone leaves the contents inaccessible inside the sandbox.
    try:
        if PROFILE_DIR.exists():
            default_rw_dirs.append(str(PROFILE_DIR.resolve()))
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
    rw_dirs = profile_rw + default_rw_dirs + rw_dirs

    if temp_dir:
        rw_dirs.append(temp_dir)

    if aws:
        rw_dirs.append(str(Path("~/.aws").expanduser()))

    safehouse_args: list[str] = [f"--enable={f}" for f in SAFEHOUSE_FEATURES]
    unix_socket_profile = write_temp_unix_socket_profile()
    if unix_socket_profile:
        safehouse_args.append(f"--append-profile={unix_socket_profile}")
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
        err(f"AWS: sharing ~/.aws (rw), AWS_PROFILE={os.environ.get('AWS_PROFILE', '(unset)')}")

    for var in profile_env_pass(profile):
        safehouse_args.append(f"--env-pass={var}")
        err(f"Env: passing {var}={os.environ.get(var, '(unset)')} through")

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
