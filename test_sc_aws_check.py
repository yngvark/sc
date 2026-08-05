#!/usr/bin/env -S uv --quiet run --python >=3.11 --script
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for the -a/--aws readiness gate.

`sc -a` mounts ~/.aws and forwards AWS_PROFILE. A set-but-expired AWS_PROFILE is
worthless — inside the sandbox that only surfaces later as an opaque credentials
error — so sc refuses to launch. An unset AWS_PROFILE only gets a note: sharing
the credentials dir is the point of -a, and the profile can change in-session.

Run directly: ./test_sc_aws_check.py

The end-to-end checks drive the real `sc` against stub `aws` and `safehouse`
binaries, so they need neither an AWS login nor a particular safehouse.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

SC = Path(__file__).resolve().parent / "sc"


def load_sc(profile_dir: str):
    os.environ["PROFILE_DIR"] = profile_dir
    loader = SourceFileLoader("sc_under_test", str(SC))
    spec = importlib.util.spec_from_loader("sc_under_test", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)  # does not run main() (guarded by __main__)
    return mod


def test_error_text_is_trimmed_to_the_aws_message(sc):
    # Shape of real aws v2 output: leading blank line, then a prefixed error.
    raw = "\naws: [ERROR]: Unable to retrieve credentials: no credentials found\n"
    assert sc.aws_error_text(raw) == "Unable to retrieve credentials: no credentials found"
    expired = "aws: [ERROR]: The SSO session associated with this profile has expired"
    assert sc.aws_error_text(expired) == (
        "The SSO session associated with this profile has expired"
    )
    # Unprefixed output is kept as-is; empty output still says something usable.
    assert sc.aws_error_text("boom\n") == "boom"
    assert "no usable credentials" in sc.aws_error_text("")


def _stub_aws(tmp_dir: str, *, exit_code: int, stderr: str = "") -> str:
    """A stub `aws` in its own dir (to be put first on PATH) that reports
    EXIT_CODE. On success it prints credentials-shaped JSON, like the real one."""
    bin_dir = Path(tmp_dir) / "aws-bin"
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / "aws"
    path.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" >> "$(dirname "$0")/args.log"\n'
        f'if [ {exit_code} -eq 0 ]; then\n'
        '  echo \'{"AccessKeyId":"AKIAFAKE","SecretAccessKey":"s3cret"}\'\n'
        'else\n'
        f'  printf "\\naws: [ERROR]: {stderr}\\n" >&2\n'
        'fi\n'
        f"exit {exit_code}\n"
    )
    path.chmod(0o755)
    return str(bin_dir)


def test_login_error_reflects_the_aws_exit_code(sc, tmp_dir: str) -> None:
    original = os.environ["PATH"]
    try:
        os.environ["PATH"] = _stub_aws(tmp_dir, exit_code=0) + os.pathsep + original
        assert sc.aws_login_error("some-profile") is None
        os.environ["PATH"] = (
            _stub_aws(tmp_dir, exit_code=253, stderr="no credentials found")
            + os.pathsep + original
        )
        assert sc.aws_login_error("some-profile") == "no credentials found"
    finally:
        os.environ["PATH"] = original


def test_missing_aws_binary_fails_open(sc, tmp_dir: str) -> None:
    """Never block a launch because the check itself could not run."""
    original = os.environ["PATH"]
    try:
        os.environ["PATH"] = str(Path(tmp_dir) / "empty-bin")
        assert sc.aws_login_error("some-profile") is None
    finally:
        os.environ["PATH"] = original


def _stub_safehouse(tmp_dir: str) -> str:
    """A safehouse that answers --version with a new-enough version and exits 99
    for anything else, so a launch that got past every gate is distinguishable
    from one blocked by them."""
    path = Path(tmp_dir) / "safehouse"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "Agent Safehouse 99.0.0"; exit 0; fi\n'
        "exit 99\n"
    )
    path.chmod(0o755)
    return str(path)


def _run_sc(
    tmp_dir: str, *, aws_profile: str | None, aws_exit: int, aws_stderr: str = "",
) -> subprocess.CompletedProcess:
    # PROFILE_DIR redirects sc's config/history writes away from the real ~;
    # TMPDIR redirects the --append-profile fragments, which a live sc session
    # write-protects in the real temp dir (safehouse locks loaded profiles).
    sc_tmp = Path(tmp_dir) / "tmp"
    sc_tmp.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "SAFEHOUSE_BIN": _stub_safehouse(tmp_dir),
        "PROFILE_DIR": str(Path(tmp_dir) / "config"),
        "TMPDIR": str(sc_tmp),
        "PATH": _stub_aws(tmp_dir, exit_code=aws_exit, stderr=aws_stderr)
                + os.pathsep + os.environ["PATH"],
    }
    if aws_profile is None:
        env.pop("AWS_PROFILE", None)
    else:
        env["AWS_PROFILE"] = aws_profile
    return subprocess.run(
        [str(SC), "--no-profile", "-a", "--", "--version"],
        capture_output=True, text=True, env=env, timeout=120,
    )


def test_e2e_unset_aws_profile_still_launches(tmp_dir: str) -> None:
    """-a is about sharing ~/.aws; the profile may be chosen inside the session,
    so an unset AWS_PROFILE is reported and the launch proceeds."""
    r = _run_sc(tmp_dir, aws_profile=None, aws_exit=253, aws_stderr="no credentials found")
    assert r.returncode == 99, f"gate blocked the launch: {r.returncode}\n{r.stderr}"
    assert "AWS_PROFILE unset" in r.stderr, r.stderr
    # With no profile to name, there is nothing to check — `aws` is never run.
    assert not (Path(tmp_dir) / "aws-bin" / "args.log").exists(), "aws was run anyway"


def test_e2e_empty_aws_profile_is_treated_as_unset(tmp_dir: str) -> None:
    r = _run_sc(tmp_dir, aws_profile="   ", aws_exit=253, aws_stderr="no credentials found")
    assert r.returncode == 99, r.stderr
    assert "AWS_PROFILE unset" in r.stderr, r.stderr


def test_e2e_expired_login_blocks_launch(tmp_dir: str) -> None:
    r = _run_sc(
        tmp_dir, aws_profile="my-sso", aws_exit=253,
        aws_stderr="The SSO session associated with this profile has expired",
    )
    assert r.returncode == 1, f"expected the gate to abort: {r.returncode}\n{r.stderr}"
    # The message must name the profile, what aws said, and the way out.
    assert "my-sso" in r.stderr, r.stderr
    assert "SSO session" in r.stderr, r.stderr
    assert "aws sso login --profile my-sso" in r.stderr, r.stderr
    assert "Launching:" not in r.stderr, r.stderr


def test_e2e_logged_in_passes_gate(tmp_dir: str) -> None:
    """A resolving profile must reach exec — exit 99 is the stub safehouse,
    meaning the gate let sc through rather than aborting at it."""
    r = _run_sc(tmp_dir, aws_profile="my-sso", aws_exit=0)
    assert r.returncode == 99, f"gate did not let sc through: {r.returncode}\n{r.stderr}"
    assert "AWS_PROFILE=my-sso (credentials OK)" in r.stderr, r.stderr
    # The check was made against the requested profile, not the ambient default.
    args = (Path(tmp_dir) / "aws-bin" / "args.log").read_text().split()
    assert args == ["configure", "export-credentials", "--profile", "my-sso"], args
    # Credentials the check printed must never be echoed by sc.
    assert "s3cret" not in r.stderr and "AKIAFAKE" not in r.stderr, r.stderr


def test_e2e_without_the_flag_there_is_no_aws_gate(tmp_dir: str) -> None:
    """No -a means no ~/.aws mount and nothing to check, even with a broken login."""
    env = {
        **os.environ,
        "SAFEHOUSE_BIN": _stub_safehouse(tmp_dir),
        "PROFILE_DIR": str(Path(tmp_dir) / "config"),
        "TMPDIR": str(Path(tmp_dir) / "tmp"),
        "PATH": _stub_aws(tmp_dir, exit_code=253, stderr="no credentials found")
                + os.pathsep + os.environ["PATH"],
    }
    env.pop("AWS_PROFILE", None)
    Path(env["TMPDIR"]).mkdir(exist_ok=True)
    r = subprocess.run(
        [str(SC), "--no-profile", "--", "--version"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert r.returncode == 99, f"non-aws launch was blocked: {r.returncode}\n{r.stderr}"
    assert "AWS" not in r.stderr, r.stderr


def main() -> None:
    with tempfile.TemporaryDirectory() as profile_dir, tempfile.TemporaryDirectory() as tmp_dir:
        sc = load_sc(profile_dir)
        test_error_text_is_trimmed_to_the_aws_message(sc)
        test_login_error_reflects_the_aws_exit_code(sc, tmp_dir)
        test_missing_aws_binary_fails_open(sc, tmp_dir)

    for name in (
        test_e2e_unset_aws_profile_still_launches,
        test_e2e_empty_aws_profile_is_treated_as_unset,
        test_e2e_expired_login_blocks_launch,
        test_e2e_logged_in_passes_gate,
        test_e2e_without_the_flag_there_is_no_aws_gate,
    ):
        # A fresh dir per case: the stub `aws` and its args.log are rewritten
        # per scenario, and the pass case asserts on exactly one invocation.
        with tempfile.TemporaryDirectory() as tmp_dir:
            name(tmp_dir)
    print("OK")


if __name__ == "__main__":
    main()
