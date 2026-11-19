# Candidate inbox (inbox)

**English** · [简体中文](inbox.md)

The inbox keeps opinions that the Agent noticed during work and that still await the user's decision. It is not a set of rules: apart from `adopt --apply` it never writes `OPINION.md`, a Profile or a lock, and it changes nothing the user has already confirmed. Stashing needs no confirmation; every status change needs the user's explicit decision.

## Location and privacy

- File: `.opinion/inbox.json` inside the project (a project-private directory, like `.opinion/profiles`).
- **It holds the user's own words, which may contain sensitive information.** Projects that use this feature should stay private, or at least stay out of public channels. If you want to make the repository public, sync it to a shared folder or let others see it, whether to exclude `.opinion/` and its contents is your decision. The tool only reminds you; it does not decide for you and never edits `.gitignore`, deletes or rewrites anything.
- When you are reminded: the first `add` that creates the inbox returns a `notice` (the Agent passes it on to you); each project gets one reminder when a session starts (the `nudge` hook); `inbox exposure` or `agenthelm nudge status` shows the current state at any time (ignored by Git / tracked by Git / not ignored / not a Git repository). Nothing is shown once Git ignores the file.
- Symbolic links are refused; read-modify-write holds `.opinion/.inbox.lock`, so several sessions can stash at the same time without losing entries.

## Statuses

- `pending`: waits for confirmation and is not in effect yet.
- `trial`: the user agreed to try it in this project. The hook injects the full text of trial candidates with the protocol and the Agent works by it; a trial that is not made formal within `nudge.trial_expire_days` (default 30) returns to `pending`.
- `formal`: the user has approved making it a formal rule. **"Approved" is not "in effect"**: `decide --to formal` only records the status. It does not write `OPINION.md`, and later sessions do not load the rule; the output says `in_opinion: false` and names the next step, `inbox adopt ...`. Only after `adopt --apply` has written and read back is the candidate both `formal` and in effect. The stored status name `formal` is unchanged (renaming it would break existing inbox data).
- `held`: put aside, no more reminders.
- `rejected`: discarded.

## Commands

All commands are `opinion_manager.py inbox <action>`, and `--project` is required. `--lang zh|en` selects the message language (by default `AGENTHELM_LANG`, then the system locale, then English). Output is JSON; `list --output markdown` gives a numbered list for the user.

- `add --title --text --scope --trigger --purpose [--exclude] [--relation] [--quote] [--client] [--session]`: stash a candidate. Title up to 120, text up to 1200, quote up to 600 and each other field up to 300 characters; at least one `--scope`. Candidates with the same content (ignoring whitespace and case) are merged and the new quote is added as evidence.
  - `--trigger` (when it applies) and `--purpose` (why) are filled in by the agent at stash time, so the user sees them once when deciding in a batch. In this version they are **soft-required**: when missing the stash still succeeds, returns `warnings` and `complete: false`, and listings show the candidate as incomplete. Rejecting missing fields is left to a later major (one switch in code: `STRICT_REQUIRED`).
  - A value that is passed but invalid is always rejected: blank, or a placeholder such as "all", "everything", "relevant", "n/a" (and the Chinese equivalents).
  - Global declaration: when the user explicitly asks for a global rule, `--trigger` must be the fixed standard value "All scenarios; the user asked for this to apply globally" (Chinese: 「全部场景，用户要求全局」) together with a non-empty `--quote`; listings mark it "Global (ask the user to confirm)". **The tool only checks the form and cannot verify who asked for it to be global**; the user confirms it when deciding.
  - `--exclude` states when it does not apply; `--relation` is the agent's own note on how this relates to existing rules, stored and shown verbatim, never interpreted.
  - When adopted, the custom line is `- **Title** (Applies to: ...; When: ...; Purpose: ...; Not for: ...): text` (older candidates without these fields render byte-for-byte as before). The labels on the line and the frame text of `OPINION.md` follow the **project file language** (`language` in the Project Profile, default zh), not the interface language; an `en` project file contains no Chinese except what the user wrote. For a project whose `language` is not zh, `opinion.lock.json` carries an optional `"language": "en"` so `verify` can reproduce the rendering; the lock format of zh projects is unchanged. Older runtimes cannot read a lock that carries `language`; upgrade first.
- `list [--status a,b] [--session ID] [--unpresented]`: list candidates. Each JSON item carries `relations` (mechanical relation hints; an empty list when none), and markdown shows them as "Possibly related" lines.
  - Hints compare against: other candidates (rejected ones excluded), the custom lines of `OPINION.md` (parsed in this tool's own two render formats), and the public rule texts selected in the Profile. Three kinds: equal normalized text (`same_text`), character-bigram Jaccard overlap >= 0.5 (`text_overlap`; both sides at least 6 characters after normalization), trigger overlap >= 0.4 (`condition_overlap`; global declarations excluded). Each carries an evidence snippet and "Mechanical comparison, for reference only", at most 5, never blocking; thresholds are conservative and prefer missing a hint. Semantic judgments (conflict, merge, widen/narrow, split) belong to the agent, who writes them into the `--relation` note; the tool stores and shows it verbatim and never interprets it.
- `check`: read-only, writes no file. Lists candidates (`pending`, `trial`) missing trigger / purpose and the custom lines of `OPINION.md`; lines in an unrecognized format are marked `unstructured` and only reported. Each candidate comes with a `revise_command` so the agent can propose values one by one. Completing lines in `OPINION.md` goes through the full `adopt` preview flow, confirmed by the user in one batch; there is no new write command.
- `revise <id> [--title --text --scope --trigger --purpose --exclude --relation --quote]`: revise a candidate. When the status is `pending` and it was never shown (`presented` is 0) no `--confirmed` is needed; in every other status `--confirmed` is required. `history` gets `{to: the unchanged status, note: revised, previous: old values of the changed fields}`; the status stays; the digest is recomputed and a collision with another candidate is an error; `--scope` replaces the list, `--quote` appends. Revising a `formal` candidate does not change `OPINION.md` (a warning is returned).
- `decide <ids...> --to pending|trial|formal|held|rejected --confirmed [--note]`: record a decision. Without `--confirmed` it is always refused; with several ids, either all succeed or none changes.
- `adopt <ids...> [--apply --confirm SHA256]`: the one-confirmation path from "adopt" to the written rule. Use it when the user has said "adopt" in the conversation; no separate `decide`.
  - Without `--apply`: preview only, nothing is written. The candidate text is appended as personal rules to the custom section of the project's existing Profile (a new `private.my-opinion@1.0.0` if there is none) and goes through the same preview as `profile` and `compose`. It returns the full `content` (what `OPINION.md` will contain), `blocked` (for example hand-written content that must be handled first) and `confirmation_sha256`.
  - The preview also returns `added_lines` (the lines that will be appended to the custom section, identical to those in `content`) and `shown_matches`: `true` when every candidate has a shown snapshot, each snapshot equals the line rendered now, nothing is `blocked` and there is no conflict; `shown` lists the result per candidate. It is a record of fact and does not relax the protection: writing still needs the preview fingerprint, and `shown_matches` loosens nothing.
  - With `--apply --confirm <the preview's confirmation_sha256>`: once the user has confirmed, it writes a new Profile version, `OPINION.md` and `opinion.lock.json` in one go, reads them back with `verify`, and only then marks the candidates `formal`. If candidates, the Profile or `OPINION.md` changed since the preview, the fingerprint no longer matches and nothing is written. It does not use `--confirmed`: the confirmation is the preview fingerprint.
  - Several ids share one preview and one confirmation, and either all succeed or none changes; a missing id or an id that is already `formal` fails the whole call and is named. If writing or the read-back fails, `OPINION.md`, the lock and the Profile file created by this run are restored and the candidate status is unchanged.
  - A candidate that `decide --to formal` already recorded but that is not written yet: move it back with `decide --to pending --confirmed`, then `adopt`.
- `present <ids...>`: record that the user has been shown these candidates (the hook calls it when it sends the confirmation request, so the same candidate is not raised again and again). It also records a **shown snapshot**: the digest of the rule line the candidate would be rendered as at that moment (`shown`: `at`, `language`, `line_sha256`). If the candidate is revised or the project language changes afterwards, the digest no longer matches. The output carries these candidates (with `relations`).
- `gate [--session ID] [--cooldown-hours N] [--max-per-session N] [--peek]`: ask throttling; it only does the bookkeeping and does not decide whether to ask. At most one ask per project within N hours (default 24) and at most `--max-per-session` asks per session (default 1); returns `allowed`, `reason` (`ok` / `cooldown` / `session_limit`) and `next_allowed_at`. An allowed ask is recorded only without `--peek`; `--peek` is read-only and creates no file. Without `--session` only the cooldown applies. State lives in the project-private `.opinion/ask-state.json` (the last 50 sessions are kept); symbolic links are refused and read-modify-write holds `.opinion/.ask.lock`; a malformed state file is an error, not an allow. The command only keeps counts; when to call it and how to word the question is up to the protocol and hook.
- `expire --days N`: return trials older than N days to `pending`. The return keeps `last_action_at`, so the item still counts as "untouched for a long time".
- `pending [--session ID]`: the pending-item provider, see below.
- `exposure`: read-only; reports the Git state of `.opinion/inbox.json` and the reminder text.
- `brief [--session ID] [--max-chars N]`: builds the protocol and the full text of trial candidates injected at session start; the default limit is 6000 characters.

## Pending-item provider contract

agent-pack's session-start check aggregates every source with the same structure (opinion candidates, and later SOP to-dos and so on). A provider returns:

```json
{"ok": true, "source": "opinion",
 "items": [{"id": "cand-20261029-001", "source": "opinion", "kind": "candidate", "title": "…",
            "status": "pending", "created_at": "…Z", "last_action_at": "…Z", "age_days": 3.2,
            "hint_command": "the command that handles this item"}],
 "counts": {"pending": 2, "trial": 1, "formal": 0, "held": 0, "rejected": 0},
 "oldest_pending_days": 3.2, "last_action_at": "…Z", "session_unpresented": ["cand-…"]}
```

Items whose `status` is `pending` count toward the reminder thresholds; `age_days` is measured from `last_action_at` (the user's last action, creation time when there is none). A provider is read-only and never changes state while reading.
