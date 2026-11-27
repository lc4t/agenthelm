"""演示项目的「技能与 MCP」清单：固定夹具，形状与 ``agenthelm inventory`` 的 JSON 一致（schema_version 1）。

全部是虚构的通用示例：路径用占位符，没有任何真实的值。MCP 条目只有名字、传输、指纹与变量名。
含：同名同指纹的跨客户端重复、同客户端不同作用域的漂移、证据未知（unknown）的副本、没有 description 的技能。
"""
from __future__ import annotations

import copy

SHA = {"review": "a1b2c3d4" * 8, "notes": "0f1e2d3c" * 8, "notes-old": "9a8b7c6d" * 8, "pdf": "5e4d3c2b" * 8, "plain": "77665544" * 8}


def _skill(name: str, client: str, scope: str, store: str, *, desc: int | None, chars: int | None = None,
           md_bytes: int = 1200, lines: int = 40, sha: str = "review", origin: dict | None = None,
           evidence: str = "file", **extra: object) -> dict:
    entry = {"name": name, "client": client, "scope": scope, "store": store, "evidence": evidence,
             "frontmatter": {"name": name, "description": ""}, "description_bytes": desc,
             "description_chars": chars if chars is not None else desc, "skill_md_bytes": md_bytes, "skill_md_lines": lines,
             "dir_bytes": md_bytes * 2, "file_count": 3, "content_sha256": SHA[sha], "is_symlink": False,
             "name_matches_dir": True, "frontmatter_ok": desc != 0,
             "origin": origin or {"type": "unknown", "id": None, "version": None, "latest_known": None, "evidence": "unknown"}}
    entry.update(extra)
    return entry


def _mcp(name: str, client: str, scope: str, store: str, fingerprint: str, *, transport: str = "stdio",
         env: tuple[str, ...] = (), evidence: str = "file", **extra: object) -> dict:
    entry = {"name": name, "client": client, "scope": scope, "store": store, "evidence": evidence, "transport": transport,
             "fingerprint": fingerprint, "fingerprint_fields": ["command", "args", "env_var_names"],
             "env_var_names": list(env), "has_inline_secret": False, "target_class": f"t-{fingerprint[:8]}.1"}
    entry.update(extra)
    return entry


INVENTORY = {
    "schema_version": 1, "project": "<project>", "clients": ["claude", "codex", "cursor", "claude-desktop"],
    "filters": {}, "summary": {"per_client": {}}, "duplicates": {"skills": [], "mcps": []}, "overlap_hints": [], "unknown": [],
    "skills": [
        _skill("code-review", "claude", "user", "~/.claude/skills", desc=312, md_bytes=1230,
               origin={"type": "plugin", "id": "example-plugin", "version": "1.4", "latest_known": "1.5", "evidence": "file"}),
        _skill("code-review", "claude", "project", ".claude/skills", desc=312, md_bytes=1230),
        _skill("pdf-notes", "claude", "user", "~/.claude/skills", desc=0, chars=0, md_bytes=900, sha="pdf"),
        _skill("long-guide", "claude", "user", "~/.claude/skills", desc=1800, chars=1800, md_bytes=9000, lines=620, sha="plain"),
        _skill("meeting-notes", "codex", "user", "~/.agents/skills", desc=210, md_bytes=800, sha="notes"),
        _skill("meeting-notes", "codex", "project", ".agents/skills", desc=210, md_bytes=840, sha="notes-old"),
        _skill("search-helper", "cursor", "project", ".agents/skills", desc=None, md_bytes=700, evidence="unknown", sha="plain"),
    ],
    "mcps": [
        _mcp("notes-server", "claude", "user", "~/.claude.json", "a1b2c3d4e5f60718", env=("NOTES_API_KEY",)),
        _mcp("notes-server", "codex", "user", "~/.codex/config.toml", "a1b2c3d4e5f60718", env=("NOTES_API_KEY",)),
        _mcp("search", "claude", "local", "~/.claude.json[<project>]", "c3d4e5f607182930"),
        _mcp("search", "claude", "user", "~/.claude.json", "e5f6071829304152"),
        _mcp("fetch", "cursor", "project", ".cursor/mcp.json", "77aa88bb99cc00dd", evidence="unknown"),
        _mcp("fetch", "cursor", "user", "~/.cursor/mcp.json", "77aa88bb99cc00dd", evidence="unknown"),
        _mcp("remote-docs", "claude", "project", ".mcp.json", "1122334455667788", transport="http",
             url_host="docs.example.com", env=("DOCS_TOKEN",)),
        _mcp("local-db", "claude-desktop", "desktop", "<app-config>/claude_desktop_config.json", "99887766aabbccdd",
             has_inline_secret=True, evidence="unknown"),
        # 指纹相同、目标不同（例如只有 cwd 不同）：不算重复，只出现在 mcp_hints
        _mcp("docs-a", "claude", "project", ".mcp.json", "5566778899aabbcc", target_class="t-55667788.1"),
        _mcp("docs-b", "claude", "project", ".mcp.json", "5566778899aabbcc", target_class="t-55667788.2"),
    ],
    "mcp_hints": [{"kind": "same-fingerprint-different-target", "client": "claude", "names": ["docs-a", "docs-b"],
                   "stores": [".mcp.json"], "fingerprint": "5566778899aabbcc", "target_relation": "different",
                   "differs_in": ["cwd"], "reasons": [], "refs": ["mcp:docs-a@claude:project", "mcp:docs-b@claude:project"]}],
}


SHADOW_SKILL = {"store": "~/.claude/skills", "rule": "claude-skill-personal-over-project", "evidence": "file",
                "source_url": "https://code.claude.com/docs/en/skills"}
SHADOW_MCP = {"store": "~/.claude.json[<project>]", "rule": "claude-mcp-local-over-user", "evidence": "file",
              "source_url": "https://code.claude.com/docs/en/mcp"}


def inventory() -> dict:
    """precedence / shadowed_by 与真实 inventory 一致：只在官方文档明确优先级的组合里给出结论。"""
    data = copy.deepcopy(INVENTORY)
    for entry in [*data["skills"], *data["mcps"]]:
        name, client, scope = entry["name"], entry["client"], entry["scope"]
        if name == "code-review" and client == "claude":
            entry["precedence"] = "winner" if scope == "user" else "shadowed"
            if scope == "project":
                entry["shadowed_by"] = SHADOW_SKILL
        elif name == "search" and client == "claude":
            entry["precedence"] = "winner" if scope == "local" else "shadowed"
            if scope == "user":
                entry["shadowed_by"] = SHADOW_MCP
        elif name == "meeting-notes":
            entry["precedence"] = "both-load"
        elif name == "fetch":
            entry["precedence"] = "unknown"
    return data
