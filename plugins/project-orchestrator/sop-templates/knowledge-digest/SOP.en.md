---
schema_version: "1.0"
id: "{{id}}"
title: "Weekly knowledge digest"
status: "draft"
version: "1.0.0"
description: "Regularly tidy new notes and materials: merge duplicates, fill in links and distill rule candidates"
trigger: "The user wants the recent notes and materials tidied, or the fixed digest day has come"
exclude: "Only looking up one particular document"
schedule: "weekly:sat"
confirm: "ask"
creates_case: "always"
created: "{{date}}"
aliases:
  - "knowledge digest"
  - "tidy notes"
examples_hit:
  - "Tidy up my recent notes"
  - "Do a knowledge digest"
examples_miss:
  - "Find that report from last time"
on_events: []
outputs: []
sources: []
decided: []
---
# {{title}}

## Purpose

Bring the notes and materials added or changed over a period into a state where they are searchable, free of duplicates and linked to each other, and distill practices that keep recurring into rule candidates.

## Prerequisites

- Command: agenthelm | Purpose: find new materials by keyword and date (`agenthelm project find`) | Install: see agenthelm's INSTALL.md | If missing: Ask the user

## Steps

1. Settle the scope of the digest
   - Inputs: a directory, tags or a period
   - Checkpoint: the user states the scope explicitly
   - Needs confirmation: yes
   - Note: if the scope contains materials with personal progress, accounts or private material, tell the user first and let them decide whether to include them; by default this procedure tidies only shareable knowledge content
2. Collect new and changed materials
   - Tools: agenthelm
   - Inputs: the scope; the date of the last digest
   - Intermediates: a list of things to tidy
   - Checkpoint: the list holds only materials in scope
3. Merge duplicates and outdated content
   - Inputs: the list of things to tidy
   - Checkpoint: every merge or deletion is on the list
   - Needs confirmation: yes
   - Note: hand the list to the user for confirmation before merging or deleting, and never delete directly; keep the originals that were merged until the user confirms
4. Fill in links and indexes
   - Checkpoint: each new material is referenced by at least one index or related material, and no link is broken
5. Distill rule candidates
   - Inputs: practices that recurred during the digest and that the user has stated explicitly
   - Intermediates: a candidate list (each with the places it is based on)
   - Note: only form candidates and hand them to rule management (opinion-manager) for the user-confirmation flow; never write into OPINION.md yourself, and the number of recurrences is not the user's confirmation
6. Record the result
   - Final outputs: docs/digest-DATE.md inside this Case's directory (using the structure of templates/digest.md in this directory)
   - Checkpoint: merges, deletions, new links and candidates are each recorded

## Completion criteria

- The result is saved and registered as a deliverable
- Everything merged or deleted was confirmed by the user
- The rule candidates were handed to the user for a decision, or "no candidates this time" is recorded explicitly

## Exceptions and fallback

- Nothing new in scope: record it as it is, and do not change existing content just to have something to show
- Content that was already tidied turns out to be wrong: do not overwrite the original; point it out in the result and ask the user

## Revision log

None yet.
