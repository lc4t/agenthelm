// Copy agent-pack's Python runtime into the package so `npx @sakanano-moe/agenthelm`
// runs exactly the code that ships in the plugin, with no second copy to maintain.
import { cpSync, mkdirSync, readdirSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = join(here, "..", "..", "..", "plugins", "agent-pack", "runtime");
const target = join(here, "..", "runtime");
rmSync(target, { recursive: true, force: true });
mkdirSync(target, { recursive: true });
for (const name of readdirSync(source)) {
  if (name.endsWith(".py")) cpSync(join(source, name), join(target, name));
}
