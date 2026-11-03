"""Security checks shared by native MCP bridge implementations."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.codex_native import bridge as codex_native_bridge


def test_secure_dir_rejects_owned_omnigent_state_root_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bridge credentials must not traverse a symlinked state-root ancestor."""
    home = tmp_path / "home"
    state_root = tmp_path / "state" / ".omnigent"
    home.mkdir()
    state_root.mkdir(mode=0o700, parents=True)
    os.chmod(state_root, 0o700)
    (home / ".omnigent").symlink_to(state_root, target_is_directory=True)
    monkeypatch.delenv("OMNIGENT_DATA_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", home / ".omnigent" / "codex-native")

    bridge_dir = home / ".omnigent" / "codex-native" / "session"
    with pytest.raises(RuntimeError, match="is a symlink"):
        claude_native_bridge._ensure_secure_dir(bridge_dir)

    assert not bridge_dir.exists()


def test_configured_physical_data_root_keeps_bridge_paths_and_security(tmp_path: Path) -> None:
    """A physical data root preserves files while every bridge keeps strict validation."""
    home = tmp_path / "home"
    parent = tmp_path / "physical"
    root = parent / ".omnigent"
    home.mkdir(mode=0o700)
    parent.mkdir(mode=0o700)
    root.mkdir(mode=0o700)
    (home / ".omnigent").symlink_to(root, target_is_directory=True)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "OMNIGENT_DATA_DIR": str(root),
    }
    script = """
import os
import tomllib
from pathlib import Path
from omnigent.harnesses.claude_native import bridge as shared
from omnigent.harnesses.codex_native import app_server, bridge as codex
from omnigent.harnesses.pi_native import bridge as pi
from omnigent.harnesses.opencode_native import bridge as opencode
from omnigent.harnesses.opencode_native.provider import build_opencode_omnigent_mcp_server
from omnigent.harnesses.antigravity_native import bridge as antigravity

root = Path(os.environ['OMNIGENT_DATA_DIR'])
for module in (codex, pi, opencode, antigravity):
    bridge_root = module.bridge_root()
    assert bridge_root.parent == root
    shared._ensure_secure_dir(bridge_root / 'probe')
    assert (Path.home() / '.omnigent' / bridge_root.name).samefile(bridge_root)
relay = shared.build_mcp_config(pi.bridge_root() / 'probe')['mcpServers']['omnigent']
assert relay['env']['OMNIGENT_DATA_DIR'] == str(root)
relay = antigravity.build_mcp_config(antigravity.bridge_root() / 'probe')['mcpServers']['omnigent']
assert relay['env']['OMNIGENT_DATA_DIR'] == str(root)
relay = build_opencode_omnigent_mcp_server(opencode.bridge_root() / 'probe')['omnigent']
assert relay['environment']['OMNIGENT_DATA_DIR'] == str(root)
section = app_server._codex_mcp_server_config_section(codex.bridge_root() / 'probe')
assert tomllib.loads(section)['mcp_servers']['omnigent']['env']['OMNIGENT_DATA_DIR'] == str(root)
assert any('OMNIGENT_DATA_DIR=' in value for value in codex.codex_mcp_config_overrides(root))
"""
    subprocess.run([sys.executable, "-c", script], env=environment, check=True, timeout=60)


def test_configured_data_root_rejects_untrusted_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "unsafe"
    parent.mkdir(mode=0o700)
    root = parent / "data"
    root.mkdir(mode=0o700)
    parent.chmod(0o777)
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(root))
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", root / "codex-native")

    with pytest.raises(RuntimeError, match="not writable"):
        claude_native_bridge._ensure_secure_dir(root / "codex-native" / "probe")


def test_configured_data_root_still_rejects_symlinks(tmp_path: Path) -> None:
    """Explicit configuration cannot bypass native bridge ancestor checks."""
    parent = tmp_path / "parent"
    target = tmp_path / "target"
    parent.mkdir(mode=0o700)
    target.mkdir(mode=0o700)
    link = parent / "data"
    link.symlink_to(target, target_is_directory=True)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "OMNIGENT_DATA_DIR": str(link),
    }
    script = """
from omnigent.harnesses.claude_native.bridge import _ensure_secure_dir
from omnigent.harnesses.codex_native.bridge import bridge_root
try:
    _ensure_secure_dir(bridge_root() / 'probe')
except RuntimeError as error:
    assert 'is a symlink' in str(error)
else:
    raise AssertionError('configured symlink was accepted')
"""
    subprocess.run([sys.executable, "-c", script], env=environment, check=True, timeout=60)
