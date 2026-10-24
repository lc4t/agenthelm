#!/usr/bin/env node
// Thin launcher: every command is implemented by agent-pack's agenthelm.py.
"use strict";
const { spawnSync } = require("node:child_process");
const { existsSync } = require("node:fs");
const { join } = require("node:path");

const script = join(__dirname, "..", "runtime", "plugins", "agent-pack", "runtime", "agenthelm.py");
if (!existsSync(script)) {
  console.error("agenthelm: runtime is missing from this package; reinstall @sakanano-moe/agenthelm.");
  process.exit(2);
}

function pythonAtLeast311(command) {
  const probe = spawnSync(command, ["-c", "import sys; print(int(sys.version_info >= (3, 11)))"], {
    encoding: "utf8",
  });
  return probe.status === 0 && probe.stdout.trim() === "1";
}

const python = [process.env.AGENTHELM_PYTHON, "python3", "python"].find(
  (candidate) => candidate && pythonAtLeast311(candidate),
);
if (!python) {
  console.error("agenthelm 需要 Python 3.11 或更高版本（python3）。安装后重试，或设置 AGENTHELM_PYTHON。");
  process.exit(2);
}
const result = spawnSync(python, [script, ...process.argv.slice(2)], { stdio: "inherit" });
process.exit(result.status === null ? 1 : result.status);
