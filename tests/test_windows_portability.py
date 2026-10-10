import os
import subprocess
import sys
from pathlib import Path


def test_schwab_module_import_does_not_require_fcntl():
    repo_root = Path(__file__).resolve().parents[1]
    script = r'''
import builtins
import sys
import types

portalocker = types.ModuleType("portalocker")
portalocker.Lock = object
portalocker.LOCK_EX = 1
portalocker.LOCK_NB = 2
portalocker.exceptions = types.SimpleNamespace(AlreadyLocked=type("AlreadyLocked", (Exception,), {}))
sys.modules["portalocker"] = portalocker
real_import = builtins.__import__
def import_without_fcntl(name, *args, **kwargs):
    if name == "fcntl":
        raise ModuleNotFoundError("No module named 'fcntl'")
    return real_import(name, *args, **kwargs)
builtins.__import__ = import_without_fcntl
import gex_client.schwab
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_private_mode_helpers_defer_to_inherited_windows_acl(monkeypatch, tmp_path):
    from gex_client import secure_storage

    monkeypatch.setattr(secure_storage, "POSIX_MODE_BITS", False)
    monkeypatch.setattr(
        secure_storage.os,
        "fchmod",
        lambda *_args: (_ for _ in ()).throw(AssertionError("fchmod called")),
        raising=False,
    )
    monkeypatch.setattr(
        secure_storage.os,
        "chmod",
        lambda *_args: (_ for _ in ()).throw(AssertionError("chmod called")),
    )

    descriptor = os.open(tmp_path / "private-file", os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        secure_storage.apply_private_file_mode(descriptor)
    finally:
        os.close(descriptor)
    secure_storage.apply_private_path_mode(tmp_path / "private-file")
    assert secure_storage.has_private_mode(tmp_path / "private-file", 0o077)
