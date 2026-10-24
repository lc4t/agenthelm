// Copy the release content into the package: index.json and the four plugins
// (without tests). `npx @sakanano-moe/agenthelm` runs exactly the code that
// ships in the plugins, and `agenthelm runtime install` installs this same
// content as the user-level runtime, with no second copy to maintain.
import { cpSync, mkdirSync, rmSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..", "..");
const target = join(here, "..", "runtime");
const plugins = ["agent-pack", "agents-init", "opinion-manager", "project-orchestrator"];
const skipped = new Set(["tests", "__pycache__", ".DS_Store", "node_modules"]);

rmSync(target, { recursive: true, force: true });
mkdirSync(join(target, "plugins"), { recursive: true });
cpSync(join(repo, "LICENSE"), join(here, "..", "LICENSE"));
cpSync(join(repo, "index.json"), join(target, "index.json"));
for (const name of plugins) {
  cpSync(join(repo, "plugins", name), join(target, "plugins", name), {
    recursive: true,
    filter: (source) => !skipped.has(basename(source)) && !source.endsWith(".pyc"),
  });
}
