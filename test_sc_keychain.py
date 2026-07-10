#!/usr/bin/env -S uv --quiet run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for the macOS Keychain deny fragment.

sc appends a sandbox profile fragment that denies Keychain access (securityd
IPC + keychain DB files), which safehouse's claude agent profile would
otherwise auto-allow — exposing e.g. gh's keyring OAuth token and defeating
the restricted GITHUB_TOKEN. Opt-out: sc --keychain.
Run directly: ./test_sc_keychain.py
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
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


def test_fragment_denies_credential_services(sc):
    frag = sc.keychain_deny_profile_text()
    # Every mach service that decrypts or mediates keychain secrets must be denied.
    for name in (
        "com.apple.SecurityServer",
        "com.apple.securityd.xpc",
        "com.apple.secd",
        "com.apple.security.agent",
        "com.apple.security.authhost",
    ):
        assert f'(global-name "{name}")' in frag, f"missing deny for {name}"
    assert "(deny mach-lookup" in frag, frag


def test_fragment_leaves_trustd_alone(sc):
    # trustd does TLS trust evaluation, not credential storage; denying it
    # would break HTTPS for tools inside the sandbox.
    assert "trustd" not in sc.keychain_deny_profile_text()


def test_fragment_denies_keychain_files(sc):
    frag = sc.keychain_deny_profile_text()
    assert "(deny file-read* file-write*" in frag, frag
    home = str(Path.home())
    assert f'(subpath "{home}/Library/Keychains")' in frag, frag


def test_write_profile_writes_stable_file(sc, tmp_dir: str):
    # Redirect the fragment away from the real temp dir: in a live sc session
    # safehouse write-protects loaded --append-profile files, so the stable
    # path is not writable from inside the sandbox.
    sc.tempfile.gettempdir = lambda: tmp_dir
    path = sc.write_keychain_deny_profile()
    if sc.sys.platform == "darwin":
        assert path is not None, "expected a fragment path on darwin"
        assert Path(path).is_file(), path
        assert Path(path).read_text() == sc.keychain_deny_profile_text()
        # Stable path: a second call overwrites the same file, no litter.
        assert sc.write_keychain_deny_profile() == path
    else:
        assert path is None, "non-darwin must not append a macOS profile"


def test_keychain_flag_parsing(sc):
    # keychain is the 5th field of the parse_args tuple; deny is the default.
    assert sc.parse_args([])[4] is False
    assert sc.parse_args(["-k"])[4] is True
    assert sc.parse_args(["--keychain"])[4] is True


def main() -> None:
    with tempfile.TemporaryDirectory() as profile_dir, tempfile.TemporaryDirectory() as tmp_dir:
        sc = load_sc(profile_dir)
        test_fragment_denies_credential_services(sc)
        test_fragment_leaves_trustd_alone(sc)
        test_fragment_denies_keychain_files(sc)
        test_write_profile_writes_stable_file(sc, tmp_dir)
        test_keychain_flag_parsing(sc)
    print("OK")


if __name__ == "__main__":
    main()
