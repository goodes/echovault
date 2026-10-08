"""Tests for agent setup module."""

import json
import os

import pytest


@pytest.fixture
def claude_home(tmp_path):
    """Create a temporary ~/.claude directory."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    return claude_dir


@pytest.fixture
def claude_home_with_settings(claude_home):
    """Create ~/.claude with an existing settings.json."""
    settings = {"permissions": {"allow": ["Bash(memory:*)"]}}
    (claude_home / "settings.json").write_text(json.dumps(settings, indent=2))
    return claude_home


def _mcp_json_path(claude_home):
    """Return the .mcp.json path (project root = parent of .claude)."""
    return claude_home.parent / ".mcp.json"


class TestClaudeCodeSetup:
    def test_writes_mcp_server_config(self, claude_home):
        from memory.setup import setup_claude_code
        setup_claude_code(str(claude_home), project=True)
        mcp_path = _mcp_json_path(claude_home)
        assert mcp_path.exists()
        data = json.loads(mcp_path.read_text())
        assert "mcpServers" in data
        assert "echovault" in data["mcpServers"]
        mcp = data["mcpServers"]["echovault"]
        assert mcp["command"] == "memory"
        assert mcp["args"] == ["mcp"]

    def test_does_not_write_hooks(self, claude_home):
        from memory.setup import setup_claude_code
        setup_claude_code(str(claude_home), project=True)
        settings_path = claude_home / "settings.json"
        if settings_path.exists():
            settings = json.loads(settings_path.read_text())
            assert "hooks" not in settings or "UserPromptSubmit" not in settings.get("hooks", {})

    def test_preserves_existing_settings(self, claude_home):
        from memory.setup import setup_claude_code
        (claude_home / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(memory:*)"]}}, indent=2))
        setup_claude_code(str(claude_home), project=True)
        settings = json.loads((claude_home / "settings.json").read_text())
        assert settings["permissions"]["allow"] == ["Bash(memory:*)"]

    def test_does_not_duplicate_mcp_config(self, claude_home):
        from memory.setup import setup_claude_code
        setup_claude_code(str(claude_home), project=True)
        setup_claude_code(str(claude_home), project=True)
        data = json.loads(_mcp_json_path(claude_home).read_text())
        assert "echovault" in data["mcpServers"]

    def test_removes_old_hooks_on_setup(self, claude_home):
        from memory.setup import setup_claude_code
        old_settings = {
            "hooks": {
                "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "memory context --project"}]}],
                "Stop": [{"hooks": [{"type": "command", "command": "echo | memory auto-save"}]}],
            }
        }
        (claude_home / "settings.json").write_text(json.dumps(old_settings, indent=2))
        setup_claude_code(str(claude_home), project=True)
        settings = json.loads((claude_home / "settings.json").read_text())
        assert "hooks" not in settings or "UserPromptSubmit" not in settings.get("hooks", {})
        data = json.loads(_mcp_json_path(claude_home).read_text())
        assert "mcpServers" in data

    def test_migrates_mcp_from_settings_to_mcp_json(self, claude_home):
        from memory.setup import setup_claude_code
        old_settings = {
            "mcpServers": {"echovault": {"command": "memory", "args": ["mcp"], "type": "stdio"}},
            "permissions": {"allow": []}
        }
        (claude_home / "settings.json").write_text(json.dumps(old_settings, indent=2))
        setup_claude_code(str(claude_home), project=True)
        # Should be removed from settings.json
        settings = json.loads((claude_home / "settings.json").read_text())
        assert "mcpServers" not in settings
        # Should be in .mcp.json
        data = json.loads(_mcp_json_path(claude_home).read_text())
        assert "echovault" in data["mcpServers"]

    def test_refreshes_old_skill_on_setup(self, claude_home):
        from memory.setup import setup_claude_code
        skill_dir = claude_home / "skills" / "echovault"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("old skill")
        setup_claude_code(str(claude_home), project=True)
        skill_path = skill_dir / "SKILL.md"
        assert skill_path.exists()
        content = skill_path.read_text()
        assert "old skill" not in content
        assert '"agent": "claude-code"' in content
        assert "<current user request>" in content

    def test_installs_task_aware_skill(self, claude_home):
        from memory.setup import setup_claude_code

        setup_claude_code(str(claude_home), project=True)

        content = (claude_home / "skills" / "echovault" / "SKILL.md").read_text()
        assert "memory_context" in content
        assert "--agent claude-code" in content
        assert "token_budget" in content

    def test_second_setup_keeps_current_skill_unchanged(self, claude_home):
        from memory.setup import setup_claude_code

        setup_claude_code(str(claude_home), project=True)
        skill_path = claude_home / "skills" / "echovault" / "SKILL.md"
        first = skill_path.read_text()
        result = setup_claude_code(str(claude_home), project=True)

        assert skill_path.read_text() == first
        assert result["message"] == "Already installed"

    def test_packaged_fallback_skill_is_task_aware(self, claude_home, monkeypatch):
        import memory.setup as setup_module

        monkeypatch.setattr(setup_module, "_get_skill_md_path", lambda: "")
        setup_module.setup_claude_code(str(claude_home), project=True)

        content = (claude_home / "skills" / "echovault" / "SKILL.md").read_text()
        assert 'agent="claude-code"' in content
        assert "--agent claude-code" in content
        assert "<current user request>" in content

    def test_returns_success_result(self, claude_home):
        from memory.setup import setup_claude_code
        result = setup_claude_code(str(claude_home), project=True)
        assert result["status"] == "ok"

    def test_global_install_writes_to_claude_json(self, tmp_path, monkeypatch):
        from memory.setup import setup_claude_code
        claude_home = tmp_path / ".claude"
        claude_home.mkdir()
        claude_json = tmp_path / ".claude.json"
        monkeypatch.setenv("HOME", str(tmp_path))
        # Patch expanduser to use tmp_path
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path) if p == "~" else p)
        setup_claude_code(str(claude_home), project=False)
        assert claude_json.exists()
        data = json.loads(claude_json.read_text())
        assert "echovault" in data["mcpServers"]
        # .mcp.json should NOT be created for global install
        assert not (tmp_path / ".mcp.json").exists()


@pytest.fixture
def cursor_home(tmp_path):
    """Create a temporary .cursor directory."""
    cursor_dir = tmp_path / ".cursor"
    cursor_dir.mkdir()
    return cursor_dir


@pytest.fixture
def cursor_home_with_hooks(cursor_home):
    """Create .cursor with an existing hooks.json."""
    hooks = {
        "version": 1,
        "hooks": {
            "afterFileEdit": [
                {"command": "./format.sh"}
            ]
        }
    }
    (cursor_home / "hooks.json").write_text(json.dumps(hooks, indent=2))
    return cursor_home


class TestCursorSetup:
    def test_writes_mcp_config(self, cursor_home):
        from memory.setup import setup_cursor
        setup_cursor(str(cursor_home))
        mcp_path = cursor_home / "mcp.json"
        assert mcp_path.exists()
        data = json.loads(mcp_path.read_text())
        assert "mcpServers" in data
        assert "echovault" in data["mcpServers"]

    def test_does_not_duplicate(self, cursor_home):
        from memory.setup import setup_cursor
        setup_cursor(str(cursor_home))
        setup_cursor(str(cursor_home))
        data = json.loads((cursor_home / "mcp.json").read_text())
        assert "echovault" in data["mcpServers"]

    def test_returns_success_result(self, cursor_home):
        from memory.setup import setup_cursor
        result = setup_cursor(str(cursor_home))
        assert result["status"] == "ok"


@pytest.fixture
def codex_home(tmp_path):
    """Create a temporary ~/.codex directory."""
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    return codex_dir


@pytest.fixture
def codex_home_with_agents_md(codex_home):
    """Create ~/.codex with an existing AGENTS.md."""
    (codex_home / "AGENTS.md").write_text("# My Codex Rules\n\nBe concise.\n")
    return codex_home


class TestCodexSetup:
    def test_creates_agents_md_if_missing(self, codex_home):
        from memory.setup import setup_codex

        setup_codex(str(codex_home))

        agents_path = codex_home / "AGENTS.md"
        assert agents_path.exists()
        content = agents_path.read_text()
        assert "memory context --project" in content
        assert "memory save" in content

    def test_appends_to_existing_agents_md(self, codex_home_with_agents_md):
        from memory.setup import setup_codex

        setup_codex(str(codex_home_with_agents_md))

        content = (codex_home_with_agents_md / "AGENTS.md").read_text()
        assert "# My Codex Rules" in content
        assert "Be concise." in content
        assert "memory context --project" in content

    def test_does_not_duplicate_section(self, codex_home):
        from memory.setup import setup_codex

        setup_codex(str(codex_home))
        setup_codex(str(codex_home))

        content = (codex_home / "AGENTS.md").read_text()
        assert content.count("## EchoVault") == 1

    def test_installs_skill_md(self, codex_home):
        from memory.setup import setup_codex

        setup_codex(str(codex_home))

        skill_path = codex_home / "skills" / "echovault" / "SKILL.md"
        assert skill_path.exists()

    def test_returns_success_result(self, codex_home):
        from memory.setup import setup_codex

        result = setup_codex(str(codex_home))

        assert result["status"] == "ok"


@pytest.fixture
def no_opencode_binary(monkeypatch):
    """Keep schema detection independent of any locally installed OpenCode."""
    monkeypatch.setattr("memory.setup._opencode_major_version", lambda: None)


class TestOpenCodeSetup:
    def test_creates_opencode_json_with_mcp(self, tmp_path, monkeypatch, no_opencode_binary):
        from memory.setup import setup_opencode
        monkeypatch.chdir(tmp_path)
        result = setup_opencode(project=True)
        assert result["status"] == "ok"
        path = tmp_path / "opencode.json"
        assert path.exists()
        data = json.loads(path.read_text())
        # A fresh config targets OpenCode 2, which nests servers under mcp.servers.
        assert "echovault" in data["mcp"]["servers"]
        cfg = data["mcp"]["servers"]["echovault"]
        assert cfg["type"] == "local"
        assert cfg["command"] == ["memory", "mcp"]

    def test_idempotent(self, tmp_path, monkeypatch):
        from memory.setup import setup_opencode
        monkeypatch.chdir(tmp_path)
        setup_opencode(project=True)
        result = setup_opencode(project=True)
        assert result["message"] == "Already installed"

    def test_preserves_existing_config(self, tmp_path, monkeypatch):
        from memory.setup import setup_opencode
        monkeypatch.chdir(tmp_path)
        existing = {"theme": "dark", "mcp": {"other-tool": {"type": "local", "command": ["other"]}}}
        (tmp_path / "opencode.json").write_text(json.dumps(existing, indent=2))
        setup_opencode(project=True)
        data = json.loads((tmp_path / "opencode.json").read_text())
        assert data["theme"] == "dark"
        assert "other-tool" in data["mcp"]
        assert "echovault" in data["mcp"]

    def test_uninstall_removes_entry(self, tmp_path, monkeypatch):
        from memory.setup import setup_opencode, uninstall_opencode
        monkeypatch.chdir(tmp_path)
        setup_opencode(project=True)
        result = uninstall_opencode(project=True)
        assert result["status"] == "ok"
        assert "Removed" in result["message"]
        # File should be removed since it was empty besides mcp
        assert not (tmp_path / "opencode.json").exists()

    def test_uninstall_preserves_other_config(self, tmp_path, monkeypatch):
        from memory.setup import setup_opencode, uninstall_opencode
        monkeypatch.chdir(tmp_path)
        existing = {"theme": "dark"}
        (tmp_path / "opencode.json").write_text(json.dumps(existing, indent=2))
        setup_opencode(project=True)
        uninstall_opencode(project=True)
        data = json.loads((tmp_path / "opencode.json").read_text())
        assert data["theme"] == "dark"
        assert "mcp" not in data

    def test_uninstall_noop_when_not_installed(self, tmp_path, monkeypatch):
        from memory.setup import uninstall_opencode
        monkeypatch.chdir(tmp_path)
        result = uninstall_opencode(project=True)
        assert result["message"] == "Nothing to remove"

    def test_global_path(self, tmp_path, monkeypatch, no_opencode_binary):
        from memory.setup import setup_opencode
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path) if p == "~" else p)
        result = setup_opencode(project=False)
        assert result["status"] == "ok"
        path = tmp_path / ".config" / "opencode" / "opencode.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert "echovault" in data["mcp"]["servers"]


class TestOpenCodeSchemaSelection:
    """OpenCode 1 keys servers off `mcp`; OpenCode 2 nests them under `mcp.servers`."""

    def test_existing_v1_config_keeps_v1_shape(self, tmp_path, monkeypatch, no_opencode_binary):
        from memory.setup import setup_opencode
        monkeypatch.chdir(tmp_path)
        existing = {"mcp": {"other-tool": {"type": "local", "command": ["other"]}}}
        (tmp_path / "opencode.json").write_text(json.dumps(existing))
        result = setup_opencode(project=True)
        assert "OpenCode 1 schema" in result["message"]
        data = json.loads((tmp_path / "opencode.json").read_text())
        assert "echovault" in data["mcp"]
        assert "servers" not in data["mcp"]
        assert "other-tool" in data["mcp"]

    def test_existing_v2_config_keeps_v2_shape(self, tmp_path, monkeypatch, no_opencode_binary):
        from memory.setup import setup_opencode
        monkeypatch.chdir(tmp_path)
        existing = {"mcp": {"servers": {"other": {"type": "local", "command": ["x"]}}}}
        (tmp_path / "opencode.json").write_text(json.dumps(existing))
        result = setup_opencode(project=True)
        assert "OpenCode 2 schema" in result["message"]
        data = json.loads((tmp_path / "opencode.json").read_text())
        assert "echovault" in data["mcp"]["servers"]
        assert "other" in data["mcp"]["servers"]

    def test_explicit_version_overrides_detection(self, tmp_path, monkeypatch, no_opencode_binary):
        from memory.setup import setup_opencode
        monkeypatch.chdir(tmp_path)
        existing = {"mcp": {"other-tool": {"type": "local", "command": ["other"]}}}
        (tmp_path / "opencode.json").write_text(json.dumps(existing))
        result = setup_opencode(project=True, opencode_version=2)
        assert "OpenCode 2 schema" in result["message"]
        data = json.loads((tmp_path / "opencode.json").read_text())
        assert "echovault" in data["mcp"]["servers"]

    def test_falls_back_to_installed_binary_version(self, tmp_path, monkeypatch):
        from memory.setup import setup_opencode
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("memory.setup._opencode_major_version", lambda: 1)
        result = setup_opencode(project=True)
        assert "OpenCode 1 schema" in result["message"]
        data = json.loads((tmp_path / "opencode.json").read_text())
        assert "echovault" in data["mcp"]

    def test_uninstall_handles_v1_shape(self, tmp_path, monkeypatch, no_opencode_binary):
        from memory.setup import setup_opencode, uninstall_opencode
        monkeypatch.chdir(tmp_path)
        setup_opencode(project=True, opencode_version=1)
        assert uninstall_opencode(project=True)["status"] == "ok"
        assert not (tmp_path / "opencode.json").exists()

    def test_uninstall_handles_v2_shape(self, tmp_path, monkeypatch, no_opencode_binary):
        from memory.setup import setup_opencode, uninstall_opencode
        monkeypatch.chdir(tmp_path)
        setup_opencode(project=True, opencode_version=2)
        assert "Removed" in uninstall_opencode(project=True)["message"]
        assert not (tmp_path / "opencode.json").exists()

    def test_idempotent_in_v2_shape(self, tmp_path, monkeypatch, no_opencode_binary):
        from memory.setup import setup_opencode
        monkeypatch.chdir(tmp_path)
        setup_opencode(project=True, opencode_version=2)
        assert setup_opencode(project=True)["message"] == "Already installed"


class TestConfigIsNeverDestroyed:
    """A config that cannot be parsed must not be replaced.

    `_read_json` previously swallowed JSONDecodeError and returned {}, so an
    install rebuilt the file from nothing and discarded everything in it.
    """

    JSONC = """{
  "$schema": "https://opencode.ai/config.json",
  "theme": "tokyonight",
  // servers I configured myself
  "mcp": {
    "servers": {
      "github": {
        "type": "local",
        "command": ["npx", "-y", "server-github"],
      },
    },
  },
}
"""

    def test_jsonc_config_is_parsed_and_preserved(self, tmp_path, monkeypatch, no_opencode_binary):
        from memory.setup import setup_opencode
        monkeypatch.chdir(tmp_path)
        (tmp_path / "opencode.json").write_text(self.JSONC, encoding="utf-8")
        result = setup_opencode(project=True)
        assert result["status"] == "ok"
        data = json.loads((tmp_path / "opencode.json").read_text(encoding="utf-8"))
        assert data["theme"] == "tokyonight"
        assert data["$schema"] == "https://opencode.ai/config.json"
        assert "github" in data["mcp"]["servers"]
        assert "echovault" in data["mcp"]["servers"]

    def test_overwriting_leaves_a_backup(self, tmp_path, monkeypatch, no_opencode_binary):
        from memory.setup import setup_opencode
        monkeypatch.chdir(tmp_path)
        (tmp_path / "opencode.json").write_text(self.JSONC, encoding="utf-8")
        setup_opencode(project=True)
        backup = tmp_path / "opencode.json.bak"
        assert backup.exists()
        # The backup keeps the original byte-for-byte, comments included.
        assert backup.read_text(encoding="utf-8") == self.JSONC

    def test_unparseable_config_is_left_alone(self, tmp_path, monkeypatch, no_opencode_binary):
        from memory.setup import setup_opencode
        monkeypatch.chdir(tmp_path)
        garbage = '{ "theme": "dark", this is not valid '
        (tmp_path / "opencode.json").write_text(garbage, encoding="utf-8")
        result = setup_opencode(project=True)
        assert result["status"] == "error"
        assert "left untouched" in result["message"]
        assert (tmp_path / "opencode.json").read_text(encoding="utf-8") == garbage

    def test_unparseable_claude_config_is_left_alone(self, tmp_path):
        """The guard covers every agent, not just OpenCode."""
        from memory.setup import setup_claude_code
        garbage = "{ not json "
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text(garbage, encoding="utf-8")
        result = setup_claude_code(str(tmp_path / ".claude"), project=True)
        assert result["status"] == "error"
        assert mcp_path.read_text(encoding="utf-8") == garbage

    def test_dropped_comments_are_reported(self, tmp_path, monkeypatch, no_opencode_binary):
        """Rewriting JSONC as JSON loses comments; say so rather than hide it."""
        from memory.setup import setup_opencode
        monkeypatch.chdir(tmp_path)
        (tmp_path / "opencode.json").write_text(self.JSONC, encoding="utf-8")
        result = setup_opencode(project=True)
        assert "comments" in result["message"]
        assert "opencode.json.bak" in result["message"]

    def test_plain_json_config_reports_no_comment_loss(self, tmp_path, monkeypatch, no_opencode_binary):
        from memory.setup import setup_opencode
        monkeypatch.chdir(tmp_path)
        (tmp_path / "opencode.json").write_text(
            json.dumps({"mcp": {"servers": {}}}), encoding="utf-8"
        )
        result = setup_opencode(project=True)
        assert "comments" not in result["message"]


class TestCodexTomlMcp:
    def test_setup_creates_config_toml(self, codex_home):
        from memory.setup import setup_codex
        setup_codex(str(codex_home))
        toml_path = codex_home / "config.toml"
        assert toml_path.exists()
        content = toml_path.read_text()
        assert "mcp_servers" in content
        assert "echovault" in content
        assert "memory" in content

    def test_setup_creates_both_agents_md_and_config_toml(self, codex_home):
        from memory.setup import setup_codex
        setup_codex(str(codex_home))
        assert (codex_home / "AGENTS.md").exists()
        assert (codex_home / "config.toml").exists()

    def test_uninstall_removes_config_toml_entry(self, codex_home):
        from memory.setup import setup_codex, uninstall_codex
        setup_codex(str(codex_home))
        uninstall_codex(str(codex_home))
        toml_path = codex_home / "config.toml"
        if toml_path.exists():
            content = toml_path.read_text()
            assert "echovault" not in content

    def test_toml_preserves_other_sections(self, codex_home):
        from memory.setup import _read_toml, _write_toml, _install_toml_mcp, _uninstall_toml_mcp
        toml_path = str(codex_home / "config.toml")
        # Write initial config with another section
        _write_toml(toml_path, {"model": "gpt-4", "mcp_servers": {"other": {"command": "other", "args": []}}})
        _install_toml_mcp(toml_path)
        data = _read_toml(toml_path)
        assert "echovault" in data["mcp_servers"]
        assert "other" in data["mcp_servers"]
        _uninstall_toml_mcp(toml_path)
        data = _read_toml(toml_path)
        assert "echovault" not in data.get("mcp_servers", {})
        assert "other" in data["mcp_servers"]


class TestTomlRoundtrip:
    def test_read_write_preserves_structure(self, tmp_path):
        from memory.setup import _read_toml, _write_toml
        path = str(tmp_path / "test.toml")
        original = {
            "model": "gpt-4",
            "mcp_servers": {
                "echovault": {"command": "memory", "args": ["mcp"]},
                "other": {"command": "other", "args": ["--flag"]},
            },
        }
        _write_toml(path, original)
        result = _read_toml(path)
        assert result["model"] == "gpt-4"
        assert result["mcp_servers"]["echovault"]["command"] == "memory"
        assert result["mcp_servers"]["echovault"]["args"] == ["mcp"]
        assert result["mcp_servers"]["other"]["command"] == "other"

    def test_empty_file_returns_empty_dict(self, tmp_path):
        from memory.setup import _read_toml
        path = str(tmp_path / "empty.toml")
        (tmp_path / "empty.toml").write_text("")
        assert _read_toml(path) == {}

    def test_missing_file_returns_empty_dict(self, tmp_path):
        from memory.setup import _read_toml
        assert _read_toml(str(tmp_path / "missing.toml")) == {}


class TestUninstall:
    def test_uninstall_claude_code_removes_mcp_config(self, claude_home):
        from memory.setup import setup_claude_code, uninstall_claude_code
        setup_claude_code(str(claude_home), project=True)
        uninstall_claude_code(str(claude_home), project=True)
        mcp_path = _mcp_json_path(claude_home)
        if mcp_path.exists():
            data = json.loads(mcp_path.read_text())
            assert "echovault" not in data.get("mcpServers", {})

    def test_uninstall_claude_code_removes_old_hooks(self, claude_home):
        from memory.setup import uninstall_claude_code
        old_settings = {
            "hooks": {
                "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "memory context"}]}],
                "PreToolUse": [{"hooks": [{"type": "command", "command": "echo hi"}]}],
            }
        }
        (claude_home / "settings.json").write_text(json.dumps(old_settings, indent=2))
        uninstall_claude_code(str(claude_home), project=True)
        settings = json.loads((claude_home / "settings.json").read_text())
        assert "UserPromptSubmit" not in settings.get("hooks", {})
        assert "PreToolUse" in settings.get("hooks", {})

    def test_uninstall_cursor_removes_mcp_config(self, cursor_home):
        from memory.setup import setup_cursor, uninstall_cursor
        setup_cursor(str(cursor_home))
        uninstall_cursor(str(cursor_home))
        mcp_path = cursor_home / "mcp.json"
        if mcp_path.exists():
            data = json.loads(mcp_path.read_text())
            assert "echovault" not in data.get("mcpServers", {})
        # File may be removed entirely if no other config remains

    def test_uninstall_codex_removes_section(self, codex_home):
        from memory.setup import setup_codex, uninstall_codex
        setup_codex(str(codex_home))
        uninstall_codex(str(codex_home))
        content = (codex_home / "AGENTS.md").read_text()
        assert "## EchoVault" not in content

    def test_uninstall_noop_when_not_installed(self, claude_home):
        from memory.setup import uninstall_claude_code
        result = uninstall_claude_code(str(claude_home), project=True)
        assert result["status"] == "ok"

