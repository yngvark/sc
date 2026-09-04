#!/usr/bin/env -S uv --quiet run --python >=3.11 --script
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for env bundles: `sc -e <name>` and the [env.<name>] tables in
config.toml. A bundle hands the sandbox plain variables (vars), 1Password
secrets resolved on the host (op), and forwarded host variables (pass).
Run directly: ./test_sc_env_bundles.py
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path


def load_sc(profile_dir: str):
    os.environ["PROFILE_DIR"] = profile_dir
    sc_path = Path(__file__).resolve().parent / "sc"
    loader = SourceFileLoader("sc_under_test", str(sc_path))
    spec = importlib.util.spec_from_loader("sc_under_test", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)  # does not run main() (guarded by __main__)
    return mod


def exits(fn, *args) -> str:
    """The message fail() printed, asserting FN(*ARGS) exited non-zero."""
    import io
    import sys
    buf = io.StringIO()
    old = sys.stderr
    sys.stderr = buf
    try:
        fn(*args)
    except SystemExit as e:
        assert e.code not in (0, None)
        return buf.getvalue()
    finally:
        sys.stderr = old
    raise AssertionError("expected fail()")


ENV_IDX = -2  # env_bundles sits right before passthrough in the parse_args tuple


def test_flag_parsing(sc):
    assert sc.parse_args([])[ENV_IDX] == []
    assert sc.parse_args(["-e", "datadog"])[ENV_IDX] == ["datadog"]
    assert sc.parse_args(["--env", "a", "-e", "b", "-e", "a"])[ENV_IDX] == ["a", "b"]
    parsed = sc.parse_args(["-e", "datadog", "--", "-c"])
    assert parsed[ENV_IDX] == ["datadog"] and parsed[-1] == ["-c"]
    assert "requires" in exits(sc.parse_args, ["-e"])


CONFIG = {
    "env": {
        "datadog": {
            "op_account": "acme.1password.eu",
            "vars": {"DD_SITE": "datadoghq.eu", "PUP_CONFIG_DIR": "$TMPDIR_UNDER_TEST/pup", "HOME_REL": "~/x"},
            "op": {"DD_API_KEY": "op://Vault/Datadog/api-key"},
            "pass": ["EDITOR"],
        },
        "plain": {"vars": {"FOO": "1"}},
    },
    "group": {"x": {"rw": ["~/nowhere"]}},
}


def test_bundle_lookup(sc):
    assert sc.env_bundle_names(CONFIG) == ["datadog", "plain"]
    assert sc.env_bundle(CONFIG, "plain") == {"vars": {"FOO": "1"}}
    msg = exits(sc.env_bundle, CONFIG, "nope")
    assert "[env.nope]" in msg and "datadog, plain" in msg
    msg = exits(sc.env_bundle, {}, "nope")
    assert "(none)" in msg


def test_unknown_key_in_bundle_aborts(sc):
    msg = exits(sc.env_bundle, {"env": {"b": {"set": {"A": "1"}}}}, "b")
    assert "unknown key" in msg and "set" in msg and "vars" in msg


def test_vars_expand_on_host(sc):
    os.environ["TMPDIR_UNDER_TEST"] = "/tmp/t"
    b = sc.env_bundle(CONFIG, "datadog")
    assert sc.bundle_env_vars(b, "datadog") == {
        "DD_SITE": "datadoghq.eu",
        "PUP_CONFIG_DIR": "/tmp/t/pup",
        "HOME_REL": str(Path("~/x").expanduser()),
    }
    assert sc.bundle_env_op(b, "datadog") == {"DD_API_KEY": "op://Vault/Datadog/api-key"}
    assert sc.bundle_env_pass(b, "datadog") == ["EDITOR"]
    assert sc.bundle_op_account(b) == "acme.1password.eu"
    assert sc.bundle_op_account({}) == ""


def test_malformed_tables_abort(sc):
    assert "strings" in exits(sc.bundle_env_vars, {"vars": {"A": 1}}, "b")
    assert "op://" in exits(sc.bundle_env_op, {"op": {"A": "not-a-ref"}}, "b")
    assert "list" in exits(sc.bundle_env_pass, {"pass": "EDITOR"}, "b")


def test_duplicate_and_reserved_names_abort(sc):
    sc.check_bundle_var_names({"a": ["X"], "b": ["Y"]})  # fine
    msg = exits(sc.check_bundle_var_names, {"a": ["X"], "b": ["X"]})
    assert "X is defined more than once" in msg and "[env.a]" in msg and "[env.b]" in msg
    msg = exits(sc.check_bundle_var_names, {"a": ["GITHUB_TOKEN"]})
    assert "sc manages itself" in msg


class FakeSecurity:
    """Stands in for subprocess.run so `security` and `op` never run."""

    def __init__(self, store: dict[tuple[str, str], str], op_value: str = "from-op"):
        self.store = store
        self.op_value = op_value
        self.op_calls = 0

    def __call__(self, cmd, **kw):
        if cmd[0] == "op":
            self.op_calls += 1
            return subprocess.CompletedProcess(cmd, 0, stdout=self.op_value + "\n", stderr="")
        assert cmd[0] == "security"
        key = (cmd[cmd.index("-s") + 1], cmd[cmd.index("-a") + 1])
        if cmd[1] == "find-generic-password":
            if key not in self.store:
                if kw.get("check"):
                    raise subprocess.CalledProcessError(44, cmd)
                return subprocess.CompletedProcess(cmd, 44, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout=self.store[key] + "\n", stderr="")
        if cmd[1] == "add-generic-password":
            self.store[key] = cmd[cmd.index("-w") + 1]
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[1] == "delete-generic-password":
            self.store.pop(key, None)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(cmd)


def test_secret_cache_round_trip(sc):
    if sc.sys.platform != "darwin":
        return
    fake = FakeSecurity({})
    sc.subprocess.run = fake
    assert sc.cached_secret("svc", "b/VAR", "Env: VAR") is None
    sc.cache_secret("svc", "b/VAR", "s3cret", "Env: VAR")
    assert fake.store[("svc", "b/VAR")].endswith(":s3cret")
    assert sc.cached_secret("svc", "b/VAR", "Env: VAR") == "s3cret"
    # Expired entries are dropped.
    fake.store[("svc", "b/VAR")] = f"{int(time.time()) - sc.TTL - 1}:old"
    assert sc.cached_secret("svc", "b/VAR", "Env: VAR") is None
    assert ("svc", "b/VAR") not in fake.store
    # The GitHub token wrappers keep their original service and account.
    sc.cache_token("jobb", "ghp_x")
    assert (sc.KEYCHAIN_SERVICE, "jobb") in fake.store
    assert sc.get_cached_token("jobb") == "ghp_x"


def test_resolve_bundle_secrets_prefers_cache(sc):
    if sc.sys.platform != "darwin":
        return
    fake = FakeSecurity({}, op_value="from-op")
    sc.subprocess.run = fake
    b = sc.env_bundle(CONFIG, "datadog")
    assert sc.resolve_bundle_secrets("datadog", b) == {"DD_API_KEY": "from-op"}
    assert fake.op_calls == 1
    assert (sc.ENV_SECRET_SERVICE, "datadog/DD_API_KEY") in fake.store
    assert sc.resolve_bundle_secrets("datadog", b) == {"DD_API_KEY": "from-op"}
    assert fake.op_calls == 1, "second resolve must come from the cache"
    # Bundle secrets never share a Keychain entry with a profile's GitHub token.
    assert sc.ENV_SECRET_SERVICE != sc.KEYCHAIN_SERVICE


def test_op_read_passes_account(sc):
    seen = {}

    def run(cmd, **_):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="v\n", stderr="")

    sc.subprocess.run = run
    assert sc.op_read("op://V/I/f", "acme.1password.eu", "thing") == "v"
    assert seen["cmd"] == ["op", "read", "op://V/I/f", "--account", "acme.1password.eu"]
    assert sc.op_read("op://V/I/f", "", "thing") == "v"
    assert seen["cmd"] == ["op", "read", "op://V/I/f"]


def test_usage_documents_bundles(sc):
    assert "-e, --env NAME" in sc.USAGE
    for key in ("vars", "op", "pass", "op_account"):
        assert key in sc.USAGE
    assert "[env.datadog]" in sc.USAGE


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        sc = load_sc(tmp)
        real_run = sc.subprocess.run
        tests = [
            test_flag_parsing,
            test_bundle_lookup,
            test_unknown_key_in_bundle_aborts,
            test_vars_expand_on_host,
            test_malformed_tables_abort,
            test_duplicate_and_reserved_names_abort,
            test_secret_cache_round_trip,
            test_resolve_bundle_secrets_prefers_cache,
            test_op_read_passes_account,
            test_usage_documents_bundles,
        ]
        for t in tests:
            sc.subprocess.run = real_run
            t(sc)
            print(f"ok {t.__name__}")
        print("All env bundle tests passed.")
