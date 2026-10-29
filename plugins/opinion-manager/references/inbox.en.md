# Candidate inbox (inbox)

**English** · [简体中文](inbox.md)

The inbox keeps opinions that the Agent noticed during work and that still await the user's decision. It is not a set of rules: it never writes `OPINION.md`, a Profile or a lock, and it changes nothing the user has already confirmed. Stashing needs no confirmation; every status change needs the user's explicit decision.

## Location and privacy

- File: `.opinion/inbox.json` inside the project (a project-private directory, like `.opinion/profiles`).
- **It holds the user's own words, which may contain sensitive information.** Projects that use this feature should stay private, or at least stay out of public channels. If you want to make the repository public, sync it to a shared folder or let others see it, whether to exclude `.opinion/` and its contents is your decision. The tool only reminds you; it does not decide for you and never edits `.gitignore`, deletes or rewrites anything.
- When you are reminded: the first `add` that creates the inbox returns a `notice` (the Agent passes it on to you); each project gets one reminder when a session starts (the `nudge` hook); `inbox exposure` or `agenthelm nudge status` shows the current state at any time (ignored by Git / tracked by Git / not ignored / not a Git repository). Nothing is shown once Git ignores the file.
- Symbolic links are refused; read-modify-write holds `.opinion/.inbox.lock`, so several sessions can stash at the same time without losing entries.

## Statuses

- `pending`: waits for confirmation and is not in effect yet.
- `trial`: the user agreed to try it in this project. The hook injects the full text of trial candidates with the protocol and the Agent works by it; a trial that is not made formal within `nudge.trial_expire_days` (default 30) returns to `pending`.
- `formal`: the user agreed to make it a formal rule. This only records the intent; writing it into `OPINION.md` still goes through the full preview and confirmation.
- `held`: put aside, no more reminders.
- `rejected`: discarded.

## Commands

All commands are `opinion_manager.py inbox <action>`, and `--project` is required. `--lang zh|en` selects the message language (by default `AGENTHELM_LANG`, then the system locale, then English). Output is JSON; `list --output markdown` gives a numbered list for the user.

- `add --title --text --scope [--quote] [--client] [--session]`: stash a candidate. Title up to 120, text up to 1200 and quote up to 600 characters; at least one `--scope`. Candidates with the same content (ignoring whitespace and case) are merged and the new quote is added as evidence.
- `list [--status a,b] [--session ID] [--unpresented]`: list candidates.
- `decide <ids...> --to pending|trial|formal|held|rejected --confirmed [--note]`: record a decision. Without `--confirmed` it is always refused; with several ids, either all succeed or none changes.
- `present <ids...>`: record that the user has been shown these candidates (the hook calls it when it sends the confirmation request, so the same candidate is not raised again and again).
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
