"""Agent setup — installs hooks, skills, and configuration for supported agents."""

import functools
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Optional


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

class ConfigParseError(Exception):
    """An existing agent config could not be parsed.

    Raised instead of returning an empty dict, because callers merge their own
    entry into whatever was read and write the result back. Treating an
    unreadable config as empty silently replaced the user's entire file.
    """

    def __init__(self, path: str):
        super().__init__(
            f"Could not parse {path}, so it was left untouched rather than "
            f"discarding your configuration. Fix the file's syntax (or move it "
            f"aside) and run this command again."
        )
        self.path = path


def _guard_config_errors(func):
    """Report a refusal to overwrite an unparseable config as a result dict."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ConfigParseError as exc:
            return {"status": "error", "message": str(exc)}

    return wrapper


def _strip_jsonc(text: str) -> str:
    """Strip comments and trailing commas from JSONC, ignoring string contents.

    Agent configs are often JSONC rather than strict JSON -- OpenCode's own
    documented example uses both line comments and trailing commas.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            if text[i + 1] == "/":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if text[i + 1] == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2
                continue
        out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _read_json_config(path: str) -> tuple[dict, bool]:
    """Read a JSON or JSONC config file.

    Returns the parsed config and whether JSONC handling was needed to read it.
    The flag matters because writing back emits strict JSON, which drops any
    comments the file had.

    Returns an empty dict only when the file is genuinely absent or empty.
    Raises ConfigParseError when the file exists but cannot be understood, so
    that callers never overwrite a config they failed to read.
    """
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        return {}, False
    except (UnicodeDecodeError, OSError) as exc:
        raise ConfigParseError(path) from exc
    if not text.strip():
        return {}, False
    try:
        return json.loads(text) or {}, False
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_strip_jsonc(text)) or {}, True
    except json.JSONDecodeError as exc:
        raise ConfigParseError(path) from exc


def _read_json(path: str) -> dict:
    """Read a JSON or JSONC config file, discarding the format detail."""
    return _read_json_config(path)[0]


def _write_json(path: str, data: dict) -> None:
    """Write a dict as formatted JSON, keeping a .bak of any existing file.

    Writing normalizes JSONC input to plain JSON, which drops the user's
    comments and formatting; the backup is what makes that recoverable.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# TOML helpers
# ---------------------------------------------------------------------------

def _read_toml(path: str) -> dict:
    """Read a TOML file, returning empty dict if missing or empty."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return {}
    if not data.strip():
        return {}
    if sys.version_info >= (3, 11):
        import tomllib
        return tomllib.loads(data.decode())
    else:
        import tomli
        return tomli.loads(data.decode())


def _write_toml(path: str, data: dict) -> None:
    """Write a dict as TOML.

    Only supports the subset we need: top-level key/value pairs and
    one level of nested tables with string/list-of-string values.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines: list[str] = []

    # Write top-level scalar keys first
    for key, value in data.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {_toml_value(value)}")

    # Write tables
    for key, value in data.items():
        if isinstance(value, dict):
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(f"[{key}]")
            for k, v in value.items():
                if isinstance(v, dict):
                    lines.append("")
                    lines.append(f"[{key}.{k}]")
                    for kk, vv in v.items():
                        lines.append(f"{kk} = {_toml_value(vv)}")
                else:
                    lines.append(f"{k} = {_toml_value(v)}")

    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def _toml_value(v: object) -> str:
    """Format a Python value as a TOML literal."""
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        items = ", ".join(_toml_value(i) for i in v)
        return f"[{items}]"
    return f'"{v}"'


# ---------------------------------------------------------------------------
# Shared MCP install/uninstall helpers
# ---------------------------------------------------------------------------

MCP_CONFIG = {
    "command": "memory",
    "args": ["mcp"],
    "type": "stdio",
}

OPENCODE_MCP_CONFIG = {
    "type": "local",
    "command": ["memory", "mcp"],
}

# Schema written when nothing indicates which OpenCode generation is in use.
OPENCODE_DEFAULT_SCHEMA = 2


def _install_mcp_servers(path: str) -> bool:
    """Install echovault into a JSON file under the ``mcpServers`` key.

    Used by Claude Code and Cursor.  Returns True if the entry was added.
    """
    data = _read_json(path)
    servers = data.setdefault("mcpServers", {})
    if "echovault" in servers:
        return False
    servers["echovault"] = MCP_CONFIG
    _write_json(path, data)
    return True


def _uninstall_mcp_servers(path: str) -> bool:
    """Remove echovault from a JSON ``mcpServers`` key.  Returns True if removed."""
    if not os.path.exists(path):
        return False
    data = _read_json(path)
    servers = data.get("mcpServers", {})
    if "echovault" not in servers:
        return False
    del servers["echovault"]
    if not servers:
        del data["mcpServers"]
    if data:
        _write_json(path, data)
    else:
        os.remove(path)
    return True


def _install_toml_mcp(path: str) -> bool:
    """Install echovault into a TOML ``[mcp_servers.echovault]`` table.

    Used by Codex.  Returns True if the entry was added.
    If the existing file can't be parsed (e.g. Codex writes non-standard
    TOML keys), falls back to appending the section directly.
    """
    try:
        data = _read_toml(path)
    except Exception:
        # File exists but has non-standard TOML — append directly
        return _append_toml_mcp_section(path)

    servers = data.setdefault("mcp_servers", {})
    if "echovault" in servers:
        return False
    servers["echovault"] = {"command": "memory", "args": ["mcp"]}
    _write_toml(path, data)
    return True


def _append_toml_mcp_section(path: str) -> bool:
    """Append [mcp_servers.echovault] to a TOML file without parsing it."""
    try:
        with open(path) as f:
            content = f.read()
    except FileNotFoundError:
        content = ""

    if "mcp_servers.echovault" in content:
        return False

    section = '\n[mcp_servers.echovault]\ncommand = "memory"\nargs = ["mcp"]\n'

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(section)
    return True


def _uninstall_toml_mcp(path: str) -> bool:
    """Remove echovault from a TOML ``[mcp_servers]`` table.  Returns True if removed."""
    import re

    if not os.path.exists(path):
        return False

    try:
        data = _read_toml(path)
        servers = data.get("mcp_servers", {})
        if "echovault" not in servers:
            return False
        del servers["echovault"]
        if not servers:
            del data["mcp_servers"]
        _write_toml(path, data)
        return True
    except Exception:
        # Non-standard TOML — use regex removal
        with open(path) as f:
            content = f.read()
        if "mcp_servers.echovault" not in content:
            return False
        cleaned = re.sub(
            r"\n*\[mcp_servers\.echovault\]\n(?:(?!\[)[^\n]*\n?)*",
            "",
            content,
        )
        with open(path, "w") as f:
            f.write(cleaned)
        return True


def _detect_opencode_schema(data: dict) -> Optional[int]:
    """Infer the OpenCode config generation from an existing config.

    OpenCode 1 keyed servers directly off ``mcp``; OpenCode 2 nests them under
    ``mcp.servers``. Returns 2 or 1, or None when the config shows neither.
    """
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        return None
    if isinstance(mcp.get("servers"), dict):
        return 2
    for value in mcp.values():
        if isinstance(value, dict) and {"command", "type", "url"} & set(value):
            return 1
    return None


def _opencode_major_version() -> Optional[int]:
    """Return the installed ``opencode`` major version, or None if unknown."""
    executable = shutil.which("opencode")
    if not executable:
        return None
    try:
        proc = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.\d+", f"{proc.stdout or ''}{proc.stderr or ''}")
    return int(match.group(1)) if match else None


def _resolve_opencode_schema(data: dict, override: Optional[int] = None) -> int:
    """Decide which OpenCode config schema to write.

    An explicit choice wins; otherwise the shape of an existing config is the
    most reliable signal, then the installed binary. A config being written
    from scratch on a machine without OpenCode falls back to the current
    generation, since that is what a new install will be.
    """
    if override is not None:
        return override
    detected = _detect_opencode_schema(data)
    if detected is not None:
        return detected
    major = _opencode_major_version()
    if major is not None:
        return 2 if major >= 2 else 1
    return OPENCODE_DEFAULT_SCHEMA


def _opencode_container(mcp: dict, schema: int, *, create: bool) -> dict:
    """Return the dict holding server entries for the given schema."""
    if schema != 2:
        return mcp
    if create:
        return mcp.setdefault("servers", {})
    servers = mcp.get("servers")
    return servers if isinstance(servers, dict) else {}


def _install_opencode_mcp(
    path: str, schema: Optional[int] = None
) -> tuple[Optional[int], bool]:
    """Install echovault into an OpenCode config.

    Returns the schema generation written (None if already present) and whether
    the config had to be read as JSONC, meaning the rewrite dropped comments.
    """
    data, was_jsonc = _read_json_config(path)
    schema = _resolve_opencode_schema(data, schema)
    mcp = data.setdefault("mcp", {})
    container = _opencode_container(mcp, schema, create=True)
    if "echovault" in container:
        return None, was_jsonc
    container["echovault"] = dict(OPENCODE_MCP_CONFIG)
    _write_json(path, data)
    return schema, was_jsonc


def _uninstall_opencode_mcp(path: str) -> bool:
    """Remove echovault from an OpenCode config, in either schema."""
    if not os.path.exists(path):
        return False
    data = _read_json(path)
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        return False
    removed = False
    for schema in (2, 1):
        container = _opencode_container(mcp, schema, create=False)
        if "echovault" in container:
            del container["echovault"]
            removed = True
    if not removed:
        return False
    if isinstance(mcp.get("servers"), dict) and not mcp["servers"]:
        del mcp["servers"]
    if not mcp:
        del data["mcp"]
    if data:
        _write_json(path, data)
    else:
        os.remove(path)
    return True


def _remove_old_hooks(settings: dict) -> list[str]:
    """Remove legacy EchoVault hooks from settings. Returns list of removed event names."""
    hooks = settings.get("hooks", {})
    removed = []
    memory_fragments = ("memory context", "memory auto-save")

    for event in list(hooks.keys()):
        event_hooks = hooks[event]
        filtered = [
            group for group in event_hooks
            if not any(
                any(frag in h.get("command", "") for frag in memory_fragments)
                for h in group.get("hooks", [])
            )
        ]
        if len(filtered) != len(event_hooks):
            removed.append(event)
            if filtered:
                hooks[event] = filtered
            else:
                del hooks[event]

    if not hooks and "hooks" in settings:
        del settings["hooks"]

    return removed


def _get_skill_md_path() -> str:
    """Get the path to the bundled SKILL.md file."""
    # Walk up from this file to find skills/echovault/SKILL.md in the package root.
    # In an installed package, use importlib.resources; for dev, use relative path.
    this_dir = os.path.dirname(os.path.abspath(__file__))
    # Try dev layout: src/memory/setup.py -> ../../skills/echovault/SKILL.md
    dev_path = os.path.join(this_dir, "..", "..", "skills", "echovault", "SKILL.md")
    if os.path.exists(dev_path):
        return os.path.abspath(dev_path)
    # Try installed layout: check package data
    try:
        from importlib.resources import files
        pkg_path = str(files("memory").joinpath("skill", "SKILL.md"))
        if os.path.exists(pkg_path):
            return pkg_path
    except (ImportError, TypeError):
        pass
    return ""


def _install_skill(agent_home: str, agent_name: str = "agent") -> bool:
    """Install the echovault SKILL.md into an agent's skills directory.

    Args:
        agent_home: Path to the agent's config directory (e.g. ~/.claude).

    Returns:
        True if skill was installed or refreshed, False if already current.
    """
    skill_dir = os.path.join(agent_home, "skills", "echovault")
    skill_path = os.path.join(skill_dir, "SKILL.md")

    source = _get_skill_md_path()
    if source:
        with open(source) as f:
            content = f.read()
    else:
        content = _FALLBACK_SKILL_MD
    content = content.replace("{{AGENT_NAME}}", agent_name)

    try:
        with open(skill_path) as f:
            if f.read() == content:
                return False
    except FileNotFoundError:
        pass

    os.makedirs(skill_dir, exist_ok=True)
    with open(skill_path, "w") as f:
        f.write(content)

    return True


def _uninstall_skill(agent_home: str) -> bool:
    """Remove the echovault skill from an agent's skills directory.

    Returns:
        True if skill was removed, False if not found.
    """
    skill_dir = os.path.join(agent_home, "skills", "echovault")
    if os.path.islink(skill_dir):
        os.remove(skill_dir)
        return True
    if os.path.exists(skill_dir):
        shutil.rmtree(skill_dir)
        return True
    return False


_FALLBACK_SKILL_MD = """\
---
name: echovault
description: Local-first memory for coding agents. You MUST retrieve task-aware context before substantive work and save durable learnings before session end.
---

# EchoVault — Agent Memory System

You have persistent memory across sessions. USE IT.

## Task-aware context — MANDATORY

Before feature, planning, debugging, or architecture work, use `memory_context`
with `agent="{{AGENT_NAME}}"` and `query` set to the current user request.

CLI fallback:

```bash
memory context --project --agent {{AGENT_NAME}} --query "<current user request>"
```

If policy reports disabled, continue normally. Use explicit search when the
context pack is insufficient.

## Session end — MANDATORY

Before ending your response to ANY task that involved making changes, debugging, deciding, or learning something, you MUST save a memory. This is not optional. If you did meaningful work, save it.

```bash
memory save \\
  --title "Short descriptive title" \\
  --what "What happened or was decided" \\
  --why "Reasoning behind it" \\
  --impact "What changed as a result" \\
  --tags "tag1,tag2,tag3" \\
  --category "<category>" \\
  --related-files "path/to/file1,path/to/file2" \\
  --source "{{AGENT_NAME}}" \\
  --details "Context:

             Options considered:
             - Option A
             - Option B

             Decision:
             Tradeoffs:
             Follow-up:"
```

Categories: `decision`, `bug`, `pattern`, `learning`, `context`, `playbook`,
`known_fix`, `constraint`, `project_state`, `active_work`.

Use `--source {{AGENT_NAME}}` to identify the agent.

### What to save

You MUST save when any of these happen:

- You made an architectural or design decision
- You fixed a bug (include root cause and solution)
- You discovered a non-obvious pattern or gotcha
- You set up infrastructure, tooling, or configuration
- You chose one approach over alternatives
- You learned something about the codebase that isn't in the code
- The user corrected you or clarified a requirement

### What NOT to save

- Trivial changes (typo fixes, formatting)
- Information that's already obvious from reading the code
- Duplicate of an existing memory (search first)

## Other commands

```bash
memory config       # show current configuration
memory sessions     # list session files
memory reindex      # rebuild search index
memory delete <id>  # remove a memory
```

## Rules

- Retrieve before working. Save before finishing. No exceptions.
- Always capture thorough details — write for a future agent with no context.
- Never include API keys, secrets, or credentials.
- Wrap sensitive values in `<redacted>` tags.
- Search before saving to avoid duplicates.
- One memory per distinct decision or event. Don't bundle unrelated things.
"""


def _get_claude_mcp_path(claude_home: str, project: bool) -> str:
    """Return the correct MCP config path for Claude Code.

    Project scope: <project_root>/.mcp.json
    Global scope:  ~/.claude.json
    """
    if project:
        project_root = os.path.dirname(claude_home)
        return os.path.join(project_root, ".mcp.json")
    return os.path.join(os.path.expanduser("~"), ".claude.json")


@_guard_config_errors
def setup_claude_code(claude_home: str, *, project: bool = False) -> dict[str, str]:
    """Install EchoVault MCP server into Claude Code."""
    installed = []

    # Clean old hooks from settings.json if present
    settings_path = os.path.join(claude_home, "settings.json")
    if os.path.exists(settings_path):
        settings = _read_json(settings_path)
        removed = _remove_old_hooks(settings)
        if removed:
            installed.append(f"removed old hooks: {', '.join(removed)}")
        # Remove mcpServers from settings.json (moved to dedicated config)
        if "mcpServers" in settings and "echovault" in settings["mcpServers"]:
            del settings["mcpServers"]["echovault"]
            if not settings["mcpServers"]:
                del settings["mcpServers"]
            installed.append("migrated mcpServers from settings.json")
        _write_json(settings_path, settings)

    # Install or refresh behavioral instructions alongside native MCP tools.
    if _install_skill(claude_home, "claude-code"):
        installed.append("skill")

    # Add MCP server config
    mcp_path = _get_claude_mcp_path(claude_home, project)
    if _install_mcp_servers(mcp_path):
        scope = ".mcp.json" if project else "~/.claude.json"
        installed.append(f"mcpServers in {scope}")

    if installed:
        return {"status": "ok", "message": f"Installed: {', '.join(installed)}"}
    return {"status": "ok", "message": "Already installed"}


@_guard_config_errors
def setup_cursor(cursor_home: str) -> dict[str, str]:
    """Install EchoVault MCP server into Cursor mcp.json."""
    installed = []

    # Remove old hooks if present
    old_hooks_path = os.path.join(cursor_home, "hooks.json")
    if os.path.exists(old_hooks_path):
        old_data = _read_json(old_hooks_path)
        hooks = old_data.get("hooks", {})
        for event in list(hooks.keys()):
            event_hooks = hooks[event]
            filtered = [h for h in event_hooks if "memory context" not in h.get("command", "")]
            if len(filtered) != len(event_hooks):
                installed.append(f"removed old hook: {event}")
                if filtered:
                    hooks[event] = filtered
                else:
                    del hooks[event]
        _write_json(old_hooks_path, old_data)

    # Remove old skill if present
    _uninstall_skill(cursor_home)

    # Add MCP server config
    mcp_path = os.path.join(cursor_home, "mcp.json")
    if _install_mcp_servers(mcp_path):
        installed.append("mcpServers")

    if installed:
        return {"status": "ok", "message": f"Installed: {', '.join(installed)}"}
    return {"status": "ok", "message": "Already installed"}


CODEX_AGENTS_MD_SECTION = """\

## EchoVault — Persistent Memory

You have persistent memory across sessions. Use it.

### Session start — MANDATORY

Before planning or implementing a feature, retrieve task-relevant context (unless
`memory config context` reports `off`):

```bash
memory context --project --agent codex --query "<current task or feature request>"
```

Search for relevant memories:

```bash
memory search "<relevant terms>"
```

When results show "Details: available", fetch them:

```bash
memory details <memory-id>
```

### Session end — MANDATORY

Before finishing any task that involved changes, debugging, decisions, or learning, save a memory:

```bash
memory save \\
  --title "Short descriptive title" \\
  --what "What happened or was decided" \\
  --why "Reasoning behind it" \\
  --impact "What changed as a result" \\
  --tags "tag1,tag2,tag3" \\
  --category "decision" \\
  --related-files "path/to/file1,path/to/file2" \\
  --source "codex" \\
  --details "Context:

             Options considered:
             - Option A
             - Option B

             Decision:
             Tradeoffs:
             Follow-up:"
```

Categories: `decision`, `bug`, `pattern`, `learning`, `context`.

### Rules

- Retrieve before working. Save before finishing. No exceptions.
- Never include API keys, secrets, or credentials.
- Search before saving to avoid duplicates.
"""


@_guard_config_errors
def setup_codex(codex_home: str) -> dict[str, str]:
    """Install EchoVault into Codex (AGENTS.md + MCP config).

    Writes memory instructions to AGENTS.md as a fallback and installs
    ``[mcp_servers.echovault]`` into config.toml for native MCP support.

    Args:
        codex_home: Path to the .codex directory (e.g. ~/.codex).

    Returns:
        Dict with 'status' and 'message' keys.
    """
    installed = []

    # AGENTS.md (fallback for agents that don't use MCP tools)
    agents_path = os.path.join(codex_home, "AGENTS.md")
    existing = ""
    try:
        with open(agents_path) as f:
            existing = f.read()
    except FileNotFoundError:
        pass

    if "## EchoVault" not in existing:
        os.makedirs(os.path.dirname(agents_path), exist_ok=True)
        with open(agents_path, "w") as f:
            f.write(existing.rstrip("\n") + "\n" + CODEX_AGENTS_MD_SECTION)
        installed.append("AGENTS.md")

    # MCP config in config.toml
    toml_path = os.path.join(codex_home, "config.toml")
    if _install_toml_mcp(toml_path):
        installed.append("config.toml")

    # Skill (legacy)
    if _install_skill(codex_home, "codex"):
        installed.append("skill")

    if not installed:
        return {"status": "ok", "message": "Already installed"}

    msg = f"Installed: {', '.join(installed)}"
    msg += "\nNote: Auto-persist (Stop hook) is only available for Claude Code. Codex relies on AGENTS.md instructions for saving."
    return {"status": "ok", "message": msg}


@_guard_config_errors
def uninstall_claude_code(claude_home: str, *, project: bool = False) -> dict[str, str]:
    """Remove EchoVault from Claude Code."""
    removed = []

    # Remove from the target scope
    mcp_path = _get_claude_mcp_path(claude_home, project)
    if _uninstall_mcp_servers(mcp_path):
        removed.append(f"mcpServers from {os.path.basename(mcp_path)}")

    # Also clean legacy locations
    settings_path = os.path.join(claude_home, "settings.json")
    if os.path.exists(settings_path):
        settings = _read_json(settings_path)
        if "mcpServers" in settings and "echovault" in settings["mcpServers"]:
            del settings["mcpServers"]["echovault"]
            if not settings["mcpServers"]:
                del settings["mcpServers"]
            removed.append("legacy mcpServers from settings.json")
        old_removed = _remove_old_hooks(settings)
        removed.extend(old_removed)
        _write_json(settings_path, settings)

    # Remove old skill
    if _uninstall_skill(claude_home):
        removed.append("skill")

    if removed:
        return {"status": "ok", "message": f"Removed: {', '.join(removed)}"}
    return {"status": "ok", "message": "Nothing to remove"}


@_guard_config_errors
def uninstall_cursor(cursor_home: str) -> dict[str, str]:
    """Remove EchoVault from Cursor (MCP config + old hooks)."""
    removed = []

    mcp_path = os.path.join(cursor_home, "mcp.json")
    if _uninstall_mcp_servers(mcp_path):
        removed.append("mcpServers")

    # Remove old hooks
    old_hooks_path = os.path.join(cursor_home, "hooks.json")
    if os.path.exists(old_hooks_path):
        old_data = _read_json(old_hooks_path)
        hooks = old_data.get("hooks", {})
        for event in list(hooks.keys()):
            event_hooks = hooks[event]
            filtered = [h for h in event_hooks if "memory context" not in h.get("command", "")]
            if len(filtered) != len(event_hooks):
                removed.append(event)
                if filtered:
                    hooks[event] = filtered
                else:
                    del hooks[event]
        _write_json(old_hooks_path, old_data)

    if _uninstall_skill(cursor_home):
        removed.append("skill")

    if removed:
        return {"status": "ok", "message": f"Removed: {', '.join(removed)}"}
    return {"status": "ok", "message": "Nothing to remove"}


@_guard_config_errors
def uninstall_codex(codex_home: str) -> dict[str, str]:
    """Remove EchoVault from Codex (AGENTS.md + config.toml)."""
    import re

    removed = []

    # Remove AGENTS.md section
    agents_path = os.path.join(codex_home, "AGENTS.md")
    try:
        with open(agents_path) as f:
            content = f.read()
    except FileNotFoundError:
        content = ""

    if "## EchoVault" in content:
        cleaned = re.sub(
            r"\n*## EchoVault[^\n]*\n.*?(?=\n## |\Z)",
            "",
            content,
            flags=re.DOTALL,
        )
        with open(agents_path, "w") as f:
            f.write(cleaned.strip() + "\n")
        removed.append("AGENTS.md")

    # Remove config.toml MCP entry
    toml_path = os.path.join(codex_home, "config.toml")
    if _uninstall_toml_mcp(toml_path):
        removed.append("config.toml")

    if _uninstall_skill(codex_home):
        removed.append("skill")

    if removed:
        return {"status": "ok", "message": f"Removed: {', '.join(removed)}"}
    return {"status": "ok", "message": "Nothing to remove"}


# ---------------------------------------------------------------------------
# OpenCode
# ---------------------------------------------------------------------------

def _get_opencode_mcp_path(project: bool) -> str:
    """Return the correct MCP config path for OpenCode.

    Project scope: <cwd>/opencode.json
    Global scope:  ~/.config/opencode/opencode.json
    """
    if project:
        return os.path.join(os.getcwd(), "opencode.json")
    return os.path.join(os.path.expanduser("~"), ".config", "opencode", "opencode.json")


@_guard_config_errors
def setup_opencode(
    *, project: bool = False, opencode_version: Optional[int] = None
) -> dict[str, str]:
    """Install EchoVault MCP server into OpenCode.

    The config schema differs between OpenCode 1 and 2; it is detected unless
    ``opencode_version`` forces one.
    """
    mcp_path = _get_opencode_mcp_path(project)
    schema, was_jsonc = _install_opencode_mcp(mcp_path, opencode_version)
    if schema is None:
        return {"status": "ok", "message": "Already installed"}
    scope = "opencode.json" if project else "~/.config/opencode/opencode.json"
    message = f"Installed: mcp in {scope} (OpenCode {schema} schema)"
    if was_jsonc:
        message += (
            f"\nNote: {scope} contained comments, which are not preserved when "
            f"the file is rewritten. The original is kept at {scope}.bak."
        )
    return {"status": "ok", "message": message}


@_guard_config_errors
def uninstall_opencode(*, project: bool = False) -> dict[str, str]:
    """Remove EchoVault from OpenCode."""
    mcp_path = _get_opencode_mcp_path(project)
    if _uninstall_opencode_mcp(mcp_path):
        scope = "opencode.json" if project else "~/.config/opencode/opencode.json"
        return {"status": "ok", "message": f"Removed: mcp from {scope}"}
    return {"status": "ok", "message": "Nothing to remove"}
