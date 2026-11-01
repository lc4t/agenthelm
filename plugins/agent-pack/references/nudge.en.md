# nudge: automatic opinion capture and reminders about unconfirmed items

**English** · [简体中文](nudge.md)

`agenthelm nudge` is called by the hooks of all three clients so that the opinion mechanism works even when the user does not bring it up: at session start it injects the protocol and the trial rules; during work it reminds the Agent to stash lasting preferences; when a task is done it asks the user to confirm candidates together; and when a new session starts it reports candidates that have been waiting too long, according to thresholds. It only reads and writes opinion-manager's inbox and local machine state, and never writes `OPINION.md`. The inbox and its status flow are described in opinion-manager's `references/inbox.en.md`.

## Behavior per client

- **Claude Code**: the plugin ships `hooks/hooks.json`, effective after installation. SessionStart (`startup|resume|clear|compact`) injects the protocol; UserPromptSubmit reminds when a keyword matches; Stop uses `additionalContext` to make the Agent confirm with the user when the conditions are met.
- **Codex**: reads the same `hooks/hooks.json` (Codex also provides `CLAUDE_PLUGIN_ROOT`). By Codex's rules a plugin hook runs only after the user reviews and trusts it in `/hooks`. Stop uses `decision: "block"` with a `reason`, and the reason text becomes the next turn's continuation prompt.
- **Cursor**: has no prompt-level injection (`beforeSubmitPrompt` can only allow or block). Run `agenthelm nudge install-hooks --client cursor` to preview and add `--apply` to write sessionStart and stop into `~/.cursor/hooks.json` (`--scope project` writes the project's `.cursor/hooks.json`; `--remove` undoes it and deletes only the entries agenthelm wrote). Cursor's `stop` prompt appears as a "user message", so it is off by default. The community reports cases where sessionStart's `additional_context` does not reach the model (see the Cursor forum), so the protocol is also written in opinion-manager's Skill and can be triggered on demand without the hook.

All three clients share these properties: the hook always exits 0 and stays silent on any error; it produces output only in projects that use opinion-manager (the Project Profile's `opinion.provider` is `opinion-manager`, or `.opinion/` or `opinion.lock.json` exists), so other directories never notice it.

## Triggers

- **Session start**: always injects the protocol and the full text of trial candidates (up to 6000 characters); after `compact` it only re-injects. When the number of pending candidates reaches `pending_threshold`, or the oldest one is older than `stale_days`, and the last reminder is older than `reminder_repeat_hours`, the user also gets one visible line.
- **User message**: the message is at most 1500 characters and contains any word from `keywords` (case-insensitive), at most `capture_hints_per_session` reminders per session. Keywords only raise the hit rate; the semantic judgement stays with the Agent. Long messages are mostly pasted content and are skipped.
- **End of turn**: candidates from this session exist that have not been shown yet, `stop_prompt` allows it, `max_prompts_per_session` is not exceeded and (with `on-completion`) the last reply contains one of `completion_keywords`. Continuation turns (`stop_hook_active`, or Cursor's `loop_count`) never trigger it. The candidates are marked as shown when the request is sent.

## Configuration

Locations (later overrides earlier): built-in defaults → the Project Profile's `opinion.nudge` (synced with the project, shared by the team) → the user config `nudge` in `~/.config/agenthelm/config.json` (a personal override). Defaults apply when nothing is set, and the Profile does not need to declare anything. Invalid values are ignored and reported by `agenthelm nudge status`.

- `enabled`: default `true`.
- `language`: message language, `auto` / `zh` / `en`, default `auto`. See the next section.
- `pending_threshold`: remind when at least this many candidates are pending, default `5`.
- `stale_days`: remind when the oldest pending candidate is older than this, default `7`.
- `max_prompts_per_session`: upper limit of end-of-turn confirmation requests per session, default `1`.
- `capture_hints_per_session`: upper limit of keyword reminders per session, default `3`.
- `trial_expire_days`: a trial not made formal within this many days returns to pending, default `30`.
- `reminder_repeat_hours`: shortest interval between visible session-start reminders, default `24`.
- `stop_prompt`: `off` / `on-completion` / `always`, or per client, default `{"claude": "on-completion", "codex": "on-completion", "cursor": "off"}`.
- `keywords`, `completion_keywords`: arrays of strings that replace the built-in lists.

The environment variable `AGENTHELM_NO_NUDGE=1` temporarily silences all output; `AGENTHELM_NUDGE_DEBUG=1` prints internal hook exceptions to stderr. `agenthelm nudge status [--project P]` shows the effective configuration, the current summary and whether a new session would remind.

## Message language

The messages shown to the user and the protocol injected into the model use the same language; Chinese (`zh`) and English (`en`) are supported. The language is decided in this order: the config `nudge.language` (when it is not `auto`) → the environment variable `AGENTHELM_LANG` → the `language` key of the user config `~/.config/agenthelm/config.json` (shared by every agenthelm command) → the system locale (`LC_ALL`, `LC_MESSAGES`, `LANG`) → English. A Chinese locale gets Chinese; a locale of any other language gets English for now; `C` / `POSIX` / empty means "not specified", so the next variable is consulted, and English applies when none names a language. The user config's `nudge.language` overrides the Project Profile, so you can override the team's setting with your own language.

- The resolved language is passed on to opinion-manager's `inbox` (`--lang`), so the protocol, the privacy notice, the pending list and the hook messages share one language. The protocol also tells the Agent to relay and confirm in the language the user is using, so the conversation language settles any mismatch with the system language.
- A candidate's title, rule and the user's quote are the user's own words and are never translated. The keyword lists are data; they contain both Chinese and English by default and can be edited.
- To add a language, add one entry per message in `MESSAGES` in `runtime/nudge_text.py` and in opinion-manager's `runtime/inbox_text.py`. The tests require every language in each file to have identical keys and placeholders, English text to contain no Chinese, and both files to resolve languages the same way.

## Local machine state

Session counters and reminder times are stored in `$XDG_STATE_HOME/agenthelm/nudge.json` (default `~/.local/state`). It holds only session ids and timestamps, for 14 days and at most 200 sessions. The candidates themselves live in the project.

## Verification status

Unit tests cover each client's output format, thresholds, rate limits, keywords, config merging and the Cursor hooks.json merge. The following is written from the official documentation and has not yet been verified in real clients: Codex's trust flow for plugin hooks and its `systemMessage` field; Cursor's sessionStart injection and the actual loading of `~/.cursor/hooks.json`; how Claude Code's `Stop` `additionalContext` behaves across versions.

## Privacy: the inbox holds the user's own words

The evidence for a candidate is the user's own words, stored in `.opinion/inbox.json` inside the project; they may contain sensitive information (customer names, internal systems, people's names, business details). Projects that use this feature should stay private, or at least stay out of public channels. If you want to make the repository public or place it in a synced or shared folder, whether to exclude `.opinion/` is the user's decision.

nudge only reminds and never decides for the user: the first `inbox add` returns a `notice`, each project gets one visible line when its first session starts (nothing is shown when Git already ignores the file), and `agenthelm nudge status` shows the current state at any time. It never edits `.gitignore` and never deletes, rewrites or refuses to store anything.
