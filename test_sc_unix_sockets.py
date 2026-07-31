#!/usr/bin/env -S uv --quiet run --python >=3.11 --script
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for the temp-dir unix-socket allow (terraform providers etc.).

safehouse's network-bind is ip-only, so a helper process that listens on a
unix socket in $TMPDIR dies at bind(). sc appends a fragment re-allowing the
socket names in TEMP_UNIX_SOCKET_BASENAMES.

Run directly: ./test_sc_unix_sockets.py

The end-to-end checks shell out to sandbox-exec and are skipped when already
inside a sandbox (nested sandbox_apply is denied) or when safehouse is absent.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

# Binds, listens on and connects to $TMPDIR/<basename>, then cleans up.
BIND_PROBE = """
import os, socket, sys, tempfile
p = os.path.join(tempfile.gettempdir(), sys.argv[1])
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    s.bind(p)
    s.listen(1)
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.connect(p)
    print("BIND_OK")
except OSError as e:
    print("BIND_FAIL", e)
    sys.exit(1)
finally:
    try:
        os.unlink(p)
    except OSError:
        pass
"""


def load_sc(profile_dir: str):
    os.environ["PROFILE_DIR"] = profile_dir
    sc_path = Path(__file__).resolve().parent / "sc"
    loader = SourceFileLoader("sc_under_test", str(sc_path))
    spec = importlib.util.spec_from_loader("sc_under_test", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)  # does not run main() (guarded by __main__)
    return mod


def test_fragment_allows_both_directions(sc):
    frag = sc.temp_unix_socket_profile_text()
    # The listener needs bind+inbound, the client needs outbound. Missing
    # either one still breaks the handshake.
    assert "(allow network-bind network-inbound" in frag, frag
    assert "(allow network-outbound" in frag, frag
    assert "local unix-socket" in frag, frag
    assert "remote unix-socket" in frag, frag


def test_fragment_covers_go_plugin_sockets(sc):
    frag = sc.temp_unix_socket_profile_text()
    assert "plugin[0-9]+" in frag, frag
    # Machine-independent: match the temp dir by shape, never a literal path
    # (this is a public repo — no local paths in the source).
    assert r"/var/folders/[^/]+/[^/]+/T/" in frag, frag
    assert os.path.realpath(tempfile.gettempdir()) not in frag, frag


def test_fragment_is_name_scoped_not_whole_temp_dir(sc):
    """A blanket temp-dir allow would override safehouse's deliberate denies on
    vscode-git-*.sock / vscode-ipc-*.sock (Seatbelt: last match wins)."""
    frag = sc.temp_unix_socket_profile_text(["plugin[0-9]+"])
    assert "vscode" not in frag
    # Anchored on the basename, so unlisted names in the same dir stay denied.
    assert r"(plugin[0-9]+)$" in frag, frag


def test_fragment_is_data_driven(sc):
    frag = sc.temp_unix_socket_profile_text(["aaa[0-9]+", "bbb"])
    assert "(aaa[0-9]+|bbb)" in frag, frag
    assert "plugin" not in frag, "basenames must come from the list, not be hardcoded"


def test_write_profile_writes_stable_file(sc, tmp_dir: str):
    # Redirect away from the real temp dir: in a live sc session safehouse
    # write-protects loaded --append-profile files. sc.tempfile is this
    # module's tempfile, so restore it for the tests that follow.
    real_gettempdir = sc.tempfile.gettempdir
    sc.tempfile.gettempdir = lambda: tmp_dir
    try:
        path = sc.write_temp_unix_socket_profile()
        if sys.platform == "darwin":
            assert path is not None
            assert Path(path).read_text() == sc.temp_unix_socket_profile_text()
            assert sc.write_temp_unix_socket_profile() == path, "path must be stable"
        else:
            assert path is None, "non-darwin must not append a macOS profile"
    finally:
        sc.tempfile.gettempdir = real_gettempdir


def test_generated_policy_compiles(sc, tmp_dir: str) -> None:
    """A Seatbelt syntax error in the fragment would break every sc launch.
    sandbox-exec compiles the policy before applying it, so this check works
    even from inside a sandbox (where the apply step is denied)."""
    frag = Path(tmp_dir) / "sc-temp-unix-sockets.sb"
    frag.write_text(sc.temp_unix_socket_profile_text())
    policy = _policy(str(frag))
    r = subprocess.run(
        ["sandbox-exec", "-f", policy, "/usr/bin/true"],
        capture_output=True, text=True,
    )
    out = (r.stdout + r.stderr).strip()
    # 0 = ran; 71 (sandbox_apply EPERM) = compiled but nested. A compile error
    # exits 65 with a "line N, column M" diagnostic.
    assert r.returncode in (0, 71), out
    assert "sandbox_apply" in out or out == "", out


def _skip_reason() -> str | None:
    if sys.platform != "darwin":
        return "not macOS"
    if not shutil.which("safehouse"):
        return "safehouse not installed"
    if os.environ.get("APP_SANDBOX_CONTAINER_ID"):
        return "already inside a sandbox (nested sandbox_apply is denied)"
    if not os.path.realpath(tempfile.gettempdir()).startswith("/private/var/folders/"):
        return f"$TMPDIR is not a per-user temp dir: {tempfile.gettempdir()}"
    return None


def _policy(append: str | None) -> str:
    args = ["safehouse", "--enable=ssh"]
    if append:
        args.append(f"--append-profile={append}")
    args.append("--stdout")
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    path = Path(tempfile.mkdtemp()) / "policy.sb"
    path.write_text(out)
    return str(path)


def _probe(policy: str, basename: str) -> str:
    r = subprocess.run(
        ["sandbox-exec", "-f", policy, "/usr/bin/python3", "-c", BIND_PROBE, basename],
        capture_output=True, text=True,
    )
    return (r.stdout + r.stderr).strip()


def test_e2e_bind_denied_without_fragment() -> None:
    """Guards the diagnosis: plain safehouse policy denies the bind."""
    out = _probe(_policy(None), f"plugin{os.getpid()}")
    assert "BIND_FAIL" in out and "not permitted" in out, out


def test_e2e_bind_allowed_with_fragment(sc, tmp_dir: str) -> None:
    frag = Path(tmp_dir) / "sc-temp-unix-sockets.sb"
    frag.write_text(sc.temp_unix_socket_profile_text())
    out = _probe(_policy(str(frag)), f"plugin{os.getpid()}")
    assert "BIND_OK" in out, out


def test_e2e_unlisted_socket_name_stays_denied(sc, tmp_dir: str) -> None:
    frag = Path(tmp_dir) / "sc-temp-unix-sockets.sb"
    frag.write_text(sc.temp_unix_socket_profile_text())
    out = _probe(_policy(str(frag)), f"scunlisted{os.getpid()}")
    assert "BIND_FAIL" in out and "not permitted" in out, out


def main() -> None:
    with tempfile.TemporaryDirectory() as profile_dir, tempfile.TemporaryDirectory() as tmp_dir:
        sc = load_sc(profile_dir)
        test_fragment_allows_both_directions(sc)
        test_fragment_covers_go_plugin_sockets(sc)
        test_fragment_is_name_scoped_not_whole_temp_dir(sc)
        test_fragment_is_data_driven(sc)
        test_write_profile_writes_stable_file(sc, tmp_dir)

        if sys.platform == "darwin" and shutil.which("safehouse"):
            test_generated_policy_compiles(sc, tmp_dir)
            print("generated policy compiles")

        skip = _skip_reason()
        if skip:
            print(f"SKIP bind checks: {skip}")
        else:
            test_e2e_bind_denied_without_fragment()
            test_e2e_bind_allowed_with_fragment(sc, tmp_dir)
            test_e2e_unlisted_socket_name_stays_denied(sc, tmp_dir)
            print("end-to-end sandbox checks passed")
    print("OK")


if __name__ == "__main__":
    main()
