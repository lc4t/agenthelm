"""Build a throwaway agent-pack project for acceptance cases.

usage: python3 -I mkfixture.py <kind> <outdir> <extracted-package-dir>

Run it on a GitHub tarball extraction directory (the source archive of a tag),
not on the npm package: it imports ``make_project`` from
``plugins/agent-pack/tests`` and the runtime from ``plugins/agent-pack/runtime``
under <extracted-package-dir>, and the tests directory is not shipped in the
npm package. Use ``python3 -I`` so the current directory and user site are not
put on sys.path. The project is created in <outdir>/<name>/project; nothing
outside <outdir> is touched. The project root is printed on the last line.

kinds (the acceptance case each one serves):
  clean        base project only (no extra branch); baseline / no-finding case
  token        .agents/mcp.json holds a token-shaped env value; secret-leak
               detection and redaction case
  home         ``${HOME}`` in an MCP arg plus a credential env file without
               the referenced key; home-path portability case
  multisource  two mcp_sources, ``${PLUGIN_ROOT}`` resolved from the first;
               multi-source import / root resolution case
  credname     launcher-wrapped entry with a ``${SERVICE_KEY}`` env, project
               directory named ``credential-present``; credential-name case
  wrapped      launcher-wrapped entry whose arg is ``${PLUGIN_ROOT}/...``;
               wrapped-launcher path case

exit codes: 0 success, 2 bad usage (unknown kind, missing or invalid args).
"""
import json
import os
import sys
from pathlib import Path

KINDS = ("clean", "token", "home", "multisource", "credname", "wrapped")
USAGE = ("usage: python3 -I mkfixture.py <kind> <outdir> <extracted-package-dir>\n"
         "kinds: " + " | ".join(KINDS))


def usage_error(msg):
    print("mkfixture: " + msg, file=sys.stderr)
    print(USAGE, file=sys.stderr)
    sys.exit(2)


if len(sys.argv) != 4:
    usage_error("expected 3 arguments, got %d" % (len(sys.argv) - 1))
kind, out, pkg = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
if kind not in KINDS:
    usage_error("unknown kind %r" % kind)
if not (pkg / "plugins/agent-pack/tests/test_agent_pack_config.py").is_file():
    usage_error("%s is not an extracted GitHub tarball directory "
                "(plugins/agent-pack/tests is missing)" % pkg)

os.environ["AGENTHELM_LANG"] = "en"
sys.path.insert(0, str(pkg / "plugins/agent-pack/runtime"))
sys.path.insert(0, str(pkg / "plugins/agent-pack/tests"))
import agent_pack_config as c
from test_agent_pack_config import make_project, write_json
name = "credential-present" if kind == "credname" else "proj"
root = make_project(out / name / "project", "demo")
prof = root / ".agents" / c.PROJECT_PROFILE_NAMESPACE / "project.json"
def caps(**kw):
    d = json.loads(prof.read_text()); d["capabilities"].update(kw); write_json(prof, d)
def mcp(path, servers):
    write_json(root / path, {"$schema": c.AGENT_PLUGIN_MCP_SCHEMA, "mcpServers": servers})
def env(text="SERVICE_KEY=value\n"):
    (root / ".env").write_text(text); (root / ".env").chmod(0o600)
if kind == "token":      # token-shaped value
    mcp(".agents/mcp.json", {"leaky": {"type": "stdio", "command": "python3", "env": {"API_TOKEN": "sk-abcdefghijklmnopqrstuvwxyz0123456789"}}})
elif kind == "home":     # ${HOME} in args
    caps(credential_env_file=".env"); env("OTHER_KEY=1\n")
    mcp(".agents/mcp.json", {"example": {"type": "stdio", "command": "node", "args": ["${HOME}/tools/example/server.js"]}})
elif kind == "multisource":  # two sources, ${PLUGIN_ROOT} from the first
    caps(mcp_sources=["tools/p/mcp.json", "tools/own/mcp.json"])
    write_json(root / "tools/p/mcp.json", {"mcpServers": {"pserver": {"type": "stdio", "command": "python3", "args": ["${PLUGIN_ROOT}/runtime/server.py"]}}})
    (root / "tools/p/runtime").mkdir(parents=True); (root / "tools/p/runtime/server.py").write_text("print(1)\n")
    write_json(root / "tools/own/mcp.json", {"mcpServers": {"own": {"type": "stdio", "command": "python3", "args": ["-m", "own"]}}})
elif kind in ("credname", "wrapped"):  # launcher-wrapped entry
    caps(credential_env_file=".env"); env()
    args = ["${PLUGIN_ROOT}/skills/sample-skill/SKILL.md"] if kind == "wrapped" else ["-m", "checker"]
    mcp(".agents/mcp.json", {"needs-key": {"type": "stdio", "command": "python3", "args": args, "env": {"SERVICE_KEY": "${SERVICE_KEY}"}}})
print(root)
