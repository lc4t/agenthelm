# Gemini CLI fixture

Everything here is synthetic (no real Gemini CLI output, no real paths).

- `bin/gemini`: a stub of the client that records how it was started (arguments, whether it got a terminal, whether anything
  arrived on its standard input) and, for `extensions install` / `uninstall`, writes or removes the extension directory.
- `home/.gemini/`: an installed extension (`extensions/agent-pack`, version 1.0.0, with the enablement file the client writes next
  to it) and a user-level skill (`skills/linked-demo`).
- `package/plugins/<name>/`: a plugin directory that carries `gemini-extension.json`, the manifest Gemini CLI requires
  (the release of this agenthelm version does not ship it yet, see the `shipped` flag of the declaration).
