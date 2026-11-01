---
schema_version: "1.0"
id: "{{id}}"
title: "Weekly review"
status: "draft"
version: "1.0.0"
description: "Look back on the progress of chosen items each week, write a review and plan the next week"
trigger: "The user wants a weekly review or to look back on the week, or the fixed review day has come"
exclude: "Only asking where one item stands right now"
schedule: "weekly:sun"
confirm: "ask"
creates_case: "always"
created: "{{date}}"
aliases:
  - "weekly review"
examples_hit:
  - "Do this week's review"
  - "Look back on what I did this week"
examples_miss:
  - "Where is this item right now"
on_events: []
outputs: []
sources: []
decided: []
---
# {{title}}

## Purpose

Look back on what the chosen items actually achieved this week and where they got stuck, collect the lessons, and settle the few most important things for next week.

## Prerequisites

- Command: agenthelm | Purpose: read the progress of the chosen items (`agenthelm project progress --output json`) | Install: see agenthelm's INSTALL.md | If missing: Ask the user

## Steps

1. Settle the scope of the review
   - Inputs: the items to include (tags, item names or IDs)
   - Checkpoint: the user states the scope explicitly; do not include every item by default
   - Needs confirmation: yes
   - Note: the review reads the work logs of the chosen items, which may hold personal progress. Items the user did not include are never read and never written into the review; the review document stays in the project that runs it, and if that project may become public, do not use this procedure to review personal progress
2. Collect the progress of the chosen items
   - Tools: agenthelm
   - Inputs: this week's start and end dates; the scope from the previous step
   - Intermediates: a progress list (item, status, this week's work log)
   - Checkpoint: only items in scope are covered, and all of them that are in progress or were closed this week
3. Compare with last week's plan
   - Inputs: the "next week's plan" in the previous review document
   - Checkpoint: every planned item has a verdict: done / not done, with the reason
4. Write the review document
   - Inputs: the results of the steps above
   - Final outputs: docs/review-DATE.md inside this Case's directory (using the structure of templates/review.md in this directory)
   - Checkpoint: every section has content and the conclusions have grounds (pointing to an item or a file); the scope of this review is stated at the top
5. Plan next week
   - Inputs: the unfinished items and blockers in the review document
   - Checkpoint: no more than the number the user set; every item has a clear next action
   - Needs confirmation: yes
   - Note: the user decides next week's plan; only suggest
6. Register the deliverable
   - Note: register the review document as a deliverable of the Case

## Completion criteria

- The review document is saved inside this Case's directory, states its scope at the top and is registered as a deliverable
- Next week's plan was confirmed by the user

## Exceptions and fallback

- The chosen items have no records at all this week: say "no records this week" as it is, and ask the user whether something went unrecorded
- The user later wants to publish or share the review: the user first confirms, section by section, what may be public, and then it is copied out; the original document is not moved

## Revision log

None yet.
