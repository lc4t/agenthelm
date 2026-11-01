---
schema_version: "1.0"
id: "{{id}}"
title: "{{title}}"
status: "draft"
version: "1.0.0"
description: "[TO FILL IN] One sentence: what this procedure does and when to think of it"
trigger: "[TO FILL IN] When this procedure should be used"
exclude: ""
confirm: "ask"
creates_case: "always"
created: "{{date}}"
aliases: []
examples_hit: []
examples_miss: []
on_events: []
outputs: []
sources: []
decided: []
---
# {{title}}

## Purpose

[TO FILL IN] What this procedure should achieve, in a sentence or two.

## Prerequisites

One item per line: `- Kind: name | Purpose: … | Install: … | Alternative: name of another item | If missing: Ask the user`.
Kinds: Command, Module (a Python module), Environment variable, Path, Skill, MCP, Manual check. If missing: Ask the user, Stop, Skip the step, Use the alternative.
For API keys write only the environment variable name, never the key itself. If no tool is needed at all, write "None".

- Command: [TO FILL IN] | Purpose: [TO FILL IN] | Install: [TO FILL IN] | If missing: Ask the user

## Steps

One number per step, with indented sub-items below it. Sub-items: Tools, Inputs, Intermediates, Final outputs, Checkpoint, On failure, Needs confirmation, Note.
Every name under "Tools" must be declared in the prerequisites above.

1. [TO FILL IN] What the first step does
   - Tools: [TO FILL IN]
   - Inputs: [TO FILL IN]
   - Intermediates: [TO FILL IN]
   - Checkpoint: [TO FILL IN] How to tell the step went right
   - On failure: [TO FILL IN] (if left out, the "run conventions" apply)

## Completion criteria

- [TO FILL IN] How to tell the whole procedure is done (a condition that can be checked, for example "the output file exists at …")

## Exceptions and fallback

- [TO FILL IN] Common exceptions and how to handle them; write "None" if there are none.

## Revision log

None yet.
