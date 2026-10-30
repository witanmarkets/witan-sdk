"""The editor plugins this repository ships (Claude Code and Cursor): manifests that load, sources
that exist, one skill kept identical in both, and an MCP connection to WITAN's /mcp. No network."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not (ROOT / ".claude-plugin").exists(), reason="the plugins are not part of the sdist")
KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
EDITORS = {
    "claude": (ROOT / ".claude-plugin" / "marketplace.json", ".claude-plugin"),
    "cursor": (ROOT / ".cursor-plugin" / "marketplace.json", ".cursor-plugin"),
}


def plugins(editor: str) -> list[tuple[dict, Path]]:
    manifest, folder = EDITORS[editor]
    market = json.loads(manifest.read_text(encoding="utf-8"))
    assert KEBAB.match(market["name"]) and market["owner"]["name"]
    # the marketplace has a name of its own: "witan" alone is what other marketplaces may call themselves
    assert market["name"] == "witan-markets"
    out = []
    for entry in market["plugins"]:
        root = (ROOT / entry["source"]).resolve()
        assert root.is_relative_to(ROOT), entry["source"]
        plugin = json.loads((root / folder / "plugin.json").read_text(encoding="utf-8"))
        assert plugin["name"] == entry["name"] and KEBAB.match(plugin["name"])
        out.append((plugin, root))
    return out


@pytest.mark.parametrize("editor", sorted(EDITORS))
def test_marketplace_and_plugin_load(editor):
    assert plugins(editor)


def skill(root: Path) -> str:
    text = (root / "skills" / "witan" / "SKILL.md").read_text(encoding="utf-8")
    front = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert front and re.search(r"^name: witan$", front.group(1), re.M) and re.search(r"^description: .{40,}", front.group(1), re.M)
    return text


def test_one_skill_in_both_plugins():
    (_, claude), = plugins("claude")
    (_, cursor), = plugins("cursor")
    assert skill(claude) == skill(cursor)


def test_both_connect_to_the_mcp_endpoint():
    (claude_plugin, _), = plugins("claude")
    server = claude_plugin["mcpServers"]["witan"]
    assert server["type"] == "http" and server["url"].endswith("/mcp") and "${WITAN_API_KEY" in server["headers"]["Authorization"]
    (_, cursor), = plugins("cursor")
    cursor_server = json.loads((cursor / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["witan"]
    assert server["url"] == "${WITAN_BASE_URL:-https://witan.markets}/mcp"  # the public service unless told otherwise
    assert cursor_server["url"] == "https://witan.markets/mcp"  # Cursor has no variable defaults
    assert "headers" not in cursor_server  # no key: Cursor signs in with OAuth (an empty Bearer would stop it)


def test_repository_manifests_for_directories_connect_without_a_key():
    """.mcp.json (cursor.directory, Claude Code in a clone) and gemini-extension.json (the Gemini CLI
    gallery) sit at the repository root and name the public /mcp with no header: search and free reads
    work without a key, and a client that signs in does it with OAuth when a tool needs it."""
    project = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert project == {"witan": {"type": "http", "url": "https://witan.markets/mcp"}}
    gemini = json.loads((ROOT / "gemini-extension.json").read_text(encoding="utf-8"))
    assert KEBAB.match(gemini["name"]) and gemini["description"]
    assert gemini["mcpServers"] == {"witan": {"httpUrl": "https://witan.markets/mcp"}}
    (claude_plugin, _), = plugins("claude")
    assert gemini["version"] == claude_plugin["version"]  # released with the editor plugins


def test_claude_plugin_error_match_hook_is_opt_in():
    """hooks/hooks.json: the error-match hook (README.md, "Error match") runs on failed shell commands only, is off
    unless WITAN_ERROR_MATCH or the error_match option turns it on, and its option defaults to false. Its behaviour
    is tested in error_match_hook.test.mjs (node --test), run here when node is installed."""
    (claude_plugin, root), = plugins("claude")
    hooks = json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert list(hooks) == ["PostToolUseFailure"]
    (entry,) = hooks["PostToolUseFailure"]
    assert entry["matcher"] == "Bash|PowerShell"
    (hook,) = entry["hooks"]
    assert hook["type"] == "command" and hook["timeout"] <= 3
    assert "${CLAUDE_PLUGIN_ROOT}/hooks/error-match.mjs" in hook["command"] and hook["command"].endswith("exit 0")
    for name in ("error-match.mjs", "lib/scrub.mjs", "lib/log.mjs"):
        assert (root / "hooks" / name).is_file(), name
    option = claude_plugin["userConfig"]["error_match"]
    assert option["type"] == "boolean" and option["default"] is False and "witan.markets" in option["title"]
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "## Error match" in readme and "What leaves your machine" in readme


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_error_match_hook_node_tests():
    done = subprocess.run(["node", "--test", str(Path(__file__).with_name("error_match_hook.test.mjs"))], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
