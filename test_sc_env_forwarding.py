#!/usr/bin/env -S uv --quiet run --python >=3.11 --script
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for env forwarding: the single table (EnvForwarding) every variable sc
hands the sandbox goes through, whatever asked for it.

Two properties are pinned here. A variable claimed by two sources aborts the
launch, because otherwise the sandbox gets one value while sc's log announces
the other. And the values sc resolved reach its own environment only after the
last `op` and `security` call, because a bundle setting HOME, PATH or
OP_ACCOUNT would otherwise decide where those two look.

Run directly: ./test_sc_env_forwarding.py
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

SC = Path(__file__).resolve().parent / "sc"
SECRET = "s3cr3t-value"


def load_sc(profile_dir: str):
    os.environ["PROFILE_DIR"] = profile_dir
    loader = SourceFileLoader("sc_under_test", str(SC))
    spec = importlib.util.spec_from_loader("sc_under_test", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)  # does not run main() (guarded by __main__)
    return mod


def exits(fn, *args, **kw) -> str:
    """The message fail() printed, asserting FN(*ARGS) exited non-zero."""
    buf = io.StringIO()
    old = sys.stderr
    sys.stderr = buf
    try:
        fn(*args, **kw)
    except SystemExit as e:
        assert e.code not in (0, None)
        return buf.getvalue()
    finally:
        sys.stderr = old
    raise AssertionError("expected fail()")


def captured(fn, *args, **kw) -> str:
    """What FN(*ARGS) printed to stderr."""
    buf = io.StringIO()
    old = sys.stderr
    sys.stderr = buf
    try:
        fn(*args, **kw)
    finally:
        sys.stderr = old
    return buf.getvalue()


def test_claim_records_flag_source_and_value(sc) -> None:
    f = sc.EnvForwarding()
    f.claim("AWS_PROFILE", "--aws")
    f.claim("DD_SITE", "[env.datadog.vars]", "datadoghq.eu")
    f.claim("EDITOR", "[env.datadog].pass")
    assert f.flags == ["--env-pass=AWS_PROFILE", "--env-pass=DD_SITE", "--env-pass=EDITOR"]
    assert f.sources == {
        "AWS_PROFILE": "--aws",
        "DD_SITE": "[env.datadog.vars]",
        "EDITOR": "[env.datadog].pass",
    }
    # Only a variable sc resolved itself carries a value; the other two are
    # forwarded with whatever the host holds.
    assert f.values == {"DD_SITE": "datadoghq.eu"}


def test_claim_prints_the_status_line(sc) -> None:
    f = sc.EnvForwarding()
    assert captured(f.claim, "DD_SITE", "[env.d.vars]", "x", log="Env: d -> DD_SITE=x") == \
        "Env: d -> DD_SITE=x\n"
    assert captured(f.claim, "AWS_PROFILE", "--aws") == ""


def test_two_sources_for_one_variable_abort(sc) -> None:
    f = sc.EnvForwarding()
    f.claim("OBSIDIAN_NOTES_DIR", "profile [env].pass")
    msg = exits(f.claim, "OBSIDIAN_NOTES_DIR", "[env.notes.vars]", "/elsewhere")
    assert "OBSIDIAN_NOTES_DIR" in msg
    assert "profile [env].pass" in msg and "[env.notes.vars]" in msg
    # The loser leaves nothing behind.
    assert f.values == {} and f.flags == ["--env-pass=OBSIDIAN_NOTES_DIR"]

    f = sc.EnvForwarding()
    f.claim("AWS_PROFILE", "--aws")
    assert "--aws" in exits(f.claim, "AWS_PROFILE", "[env.aws.vars]", "other")

    f = sc.EnvForwarding()
    f.claim("GITHUB_TOKEN", "profile [github].token", "ghp_x")
    assert "profile [github].token" in exits(f.claim, "GITHUB_TOKEN", "[env.gh.op]", "ghp_y")


def test_apply_writes_the_environment_once(sc) -> None:
    f = sc.EnvForwarding()
    f.claim("SC_APPLY_UNDER_TEST", "[env.t.vars]", "written")
    f.claim("SC_APPLY_PASSTHROUGH", "[env.t].pass")
    try:
        assert "SC_APPLY_UNDER_TEST" not in os.environ, "claim must not touch os.environ"
        f.apply()
        assert os.environ["SC_APPLY_UNDER_TEST"] == "written"
        # A forwarded host variable is not invented out of thin air.
        assert "SC_APPLY_PASSTHROUGH" not in os.environ
    finally:
        os.environ.pop("SC_APPLY_UNDER_TEST", None)


def _stub_bin(dir_path: Path, op_home: Path, security_home: Path) -> None:
    """An `op` and a `security` that record the HOME they ran with. Both are
    what a bundle variable could redirect, so the recorded value is the test's
    evidence that nothing had been exported yet."""
    dir_path.mkdir(parents=True, exist_ok=True)
    op = dir_path / "op"
    op.write_text(
        "#!/bin/sh\n"
        f'echo "$HOME" >> "{op_home}"\n'
        f"echo {SECRET}\n"
    )
    op.chmod(0o755)
    security = dir_path / "security"
    security.write_text(
        "#!/bin/sh\n"
        f'echo "$HOME" >> "{security_home}"\n'
        'if [ "$1" = "find-generic-password" ]; then exit 44; fi\n'  # always a cache miss
        "exit 0\n"
    )
    security.chmod(0o755)


def _stub_safehouse(path: Path, argv_file: Path, env_file: Path) -> None:
    """A safehouse that answers --version, records the argv and the forwarded
    variables of a real launch, and exits 99 so a launch is distinguishable
    from a gate abort."""
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "Agent Safehouse 0.11.1"; exit 0; fi\n'
        f'for a in "$@"; do echo "$a" >> "{argv_file}"; done\n'
        f'{{ echo "HOME=$HOME"; echo "SC_FWD_PLAIN=$SC_FWD_PLAIN"; '
        f'echo "SC_FWD_SECRET=$SC_FWD_SECRET"; }} >> "{env_file}"\n'
        "exit 99\n"
    )
    path.chmod(0o755)


CONFIG = """
[env.b.vars]
HOME = "{fake_home}"
SC_FWD_PLAIN = "plain"

[env.b.op]
SC_FWD_SECRET = "op://Vault/Item/field"

[env.b]
pass = ["SC_FWD_HOST"]
"""

COLLIDING_CONFIG = """
[env.b.vars]
SC_FWD_PROFILE = "from-the-bundle"
"""

PROFILE = """
[env]
pass = ["SC_FWD_PROFILE"]
"""


class Launcher:
    """Runs the real sc against stub safehouse, op and security binaries."""

    def __init__(self, tmp: Path, profile_dir: Path) -> None:
        self.tmp = tmp
        self.profile_dir = profile_dir
        self.argv_file = tmp / "argv"
        self.env_file = tmp / "child-env"
        self.op_home = tmp / "op-home"
        self.security_home = tmp / "security-home"
        self.fake_home = tmp / "fake-home"
        self.work = tmp / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        safehouse = tmp / "safehouse"
        _stub_safehouse(safehouse, self.argv_file, self.env_file)
        _stub_bin(tmp / "bin", self.op_home, self.security_home)
        (profile_dir / "profiles").mkdir(parents=True, exist_ok=True)
        (profile_dir / "profiles" / "fwd.toml").write_text(PROFILE)
        sc_tmp = tmp / "tmp"
        sc_tmp.mkdir(exist_ok=True)
        self.env = {
            **os.environ,
            "SAFEHOUSE_BIN": str(safehouse),
            "PROFILE_DIR": str(profile_dir),
            "TMPDIR": str(sc_tmp),
            "PATH": f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "SC_FWD_HOST": "from-the-host",
            "SC_FWD_PROFILE": "from-the-profile",
        }

    def launch(self, config_text: str, *args: str) -> subprocess.CompletedProcess:
        for f in (self.argv_file, self.env_file, self.op_home, self.security_home):
            f.unlink(missing_ok=True)
        (self.profile_dir / "config.toml").write_text(
            config_text.format(fake_home=self.fake_home)
        )
        return subprocess.run(
            [str(SC), *args, "--", "--version"],
            capture_output=True, text=True, env=self.env, cwd=str(self.work), timeout=120,
        )

    def argv(self) -> list[str]:
        return self.argv_file.read_text().splitlines()

    def child_env(self) -> dict[str, str]:
        return dict(
            line.split("=", 1) for line in self.env_file.read_text().splitlines() if line
        )


def test_e2e_forwards_every_source(launcher: Launcher) -> None:
    r = launcher.launch(CONFIG, "-p", "fwd", "-e", "b")
    assert r.returncode == 99, f"sc did not reach exec: {r.returncode}\n{r.stderr}"
    argv = launcher.argv()
    for var in ("HOME", "SC_FWD_PLAIN", "SC_FWD_SECRET", "SC_FWD_HOST", "SC_FWD_PROFILE"):
        assert f"--env-pass={var}" in argv, f"{var} not forwarded: {argv}"
    # No variable is forwarded twice, and the values sc resolved did reach the
    # process it execs.
    passes = [a for a in argv if a.startswith("--env-pass=")]
    assert len(passes) == len(set(passes)), passes
    child = launcher.child_env()
    assert child["SC_FWD_PLAIN"] == "plain"
    assert child["SC_FWD_SECRET"] == SECRET
    assert child["HOME"] == str(launcher.fake_home)
    # A secret is logged by its last characters only.
    assert SECRET not in r.stderr
    assert f"...{SECRET[-4:]}" in r.stderr


def test_e2e_bundle_home_does_not_reach_op_or_security(launcher: Launcher) -> None:
    r = launcher.launch(CONFIG, "-p", "fwd", "-e", "b")
    assert r.returncode == 99, f"sc did not reach exec: {r.returncode}\n{r.stderr}"
    host_home = os.environ["HOME"]
    seen = launcher.op_home.read_text().split()
    assert seen and set(seen) == {host_home}, \
        f"op ran with a rewritten HOME: {seen} (host {host_home})"
    if sys.platform == "darwin":
        seen = launcher.security_home.read_text().split()
        assert seen and set(seen) == {host_home}, \
            f"security ran with a rewritten HOME: {seen} (host {host_home})"


def test_e2e_bundle_colliding_with_profile_pass_aborts(launcher: Launcher) -> None:
    r = launcher.launch(COLLIDING_CONFIG, "-p", "fwd", "-e", "b")
    assert r.returncode == 1, f"expected an abort, got {r.returncode}\n{r.stderr}"
    assert "SC_FWD_PROFILE is set by both" in r.stderr, r.stderr
    assert "profile [env].pass" in r.stderr and "[env.b.vars]" in r.stderr, r.stderr
    assert not launcher.argv_file.exists(), "safehouse was launched anyway"


def main() -> None:
    with tempfile.TemporaryDirectory() as profile_dir, tempfile.TemporaryDirectory() as work:
        sc = load_sc(profile_dir)
        test_claim_records_flag_source_and_value(sc)
        test_claim_prints_the_status_line(sc)
        test_two_sources_for_one_variable_abort(sc)
        test_apply_writes_the_environment_once(sc)
        launcher = Launcher(Path(work), Path(profile_dir))
        test_e2e_forwards_every_source(launcher)
        test_e2e_bundle_home_does_not_reach_op_or_security(launcher)
        test_e2e_bundle_colliding_with_profile_pass_aborts(launcher)
    print("OK")


if __name__ == "__main__":
    main()
