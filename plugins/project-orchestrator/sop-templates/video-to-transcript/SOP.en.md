---
schema_version: "1.0"
id: "{{id}}"
title: "Video to transcript"
status: "draft"
version: "1.0.0"
description: "Turn a video (a link or a local file) into a transcript"
trigger: "The user wants a video turned into a transcript, subtitles or searchable text"
exclude: "The user already has a transcript and only wants it summarized or rewritten"
confirm: "ask"
creates_case: "always"
created: "{{date}}"
aliases:
  - "video to text"
  - "video transcription"
examples_hit:
  - "Turn this video into a transcript"
  - "Transcribe the video at this link"
examples_miss:
  - "Summarize this transcript"
on_events: []
outputs: []
sources: []
decided: []
---
# {{title}}

## Purpose

Turn a video into a readable, searchable transcript that keeps its source information.

## Prerequisites

A set of common choices; the names are examples: confirm, replace or delete each one according to the tools you actually use. Items marked "Only if" are needed only when this run's input matches the condition.

- Command: yutto | Purpose: download Bilibili videos (BV / av IDs, bilibili.com links); `--audio-only` fetches just the audio, `-d` sets the output directory | Install: `pipx install yutto` (needs FFmpeg configured) | Only if: the input is a Bilibili video | If missing: Ask the user
- Command: yt-dlp | Purpose: download online videos from other sites | Install: `pipx install yt-dlp` or the system package manager | Only if: the input is an online video from a site other than Bilibili | If missing: Ask the user
- Command: ffmpeg | Purpose: merge Bilibili audio and video streams, extract audio from a video, convert to the format the transcription tool needs | Install: the system package manager (for example `brew install ffmpeg`) | If missing: Ask the user
- Command: whisper-cli | Purpose: transcription with a local model | Install: follow the official instructions of the local transcription tool you use | Alternative: TRANSCRIBE_API_KEY | If missing: Use the alternative
- Path: ~/models/whisper.bin | Purpose: the model file whisper-cli needs; change it to your actual location | If missing: Ask the user
- Environment variable: TRANSCRIBE_API_KEY | Purpose: the key for a cloud transcription API, if you switch to one (write only the variable name) | If missing: Ask the user

## Steps

1. Confirm the input and options
   - Inputs: the video link or local file path; the language; whether timestamps are needed
   - Checkpoint: the input is clear, and a local file really exists
2. Get the video file
   - Tools: yutto, yt-dlp
   - Inputs: the video link (skip this step for a local file)
   - Intermediates: the downloaded video or audio file
   - Checkpoint: the file exists and is larger than 0 bytes
   - On failure: Ask the user (when a link is dead, needs a login or is region-locked, do not work around it yourself)
   - Needs confirmation: yes
   - Note: use yutto for Bilibili links and yt-dlp for other sites. For content that needs a login, the user logs in and saves the login information themselves (see yutto's official documentation for how); credentials are written neither into this SOP nor into the Case. Downloading a large file is a slow step, so the "run conventions" decide whether to ask first
3. Extract the audio
   - Tools: ffmpeg
   - Inputs: the video or audio file from the previous step
   - Intermediates: audio.wav (mono, 16 kHz; follow the transcription tool if it needs another format)
   - Checkpoint: the audio length differs from the video length by at most 1 second
4. Transcribe
   - Tools: whisper-cli, TRANSCRIBE_API_KEY
   - Inputs: audio.wav; the language
   - Intermediates: the raw transcription result
   - Checkpoint: the text is not empty and its length roughly matches the audio length
   - Note: prefer local transcription; switch to a cloud API only when the local one is unavailable and the user agrees, because using an API sends the audio to a third-party service, which needs the user's consent first
5. Tidy up and save
   - Inputs: the raw transcription result
   - Final outputs: the transcript (see the completion criteria for where to save it)
   - Checkpoint: spot-check one passage each from the start, the middle and the end against the audio
6. Clean up
   - Note: handle the intermediates according to the "run conventions"; clean up only files registered in the Case during this run

## Completion criteria

- The transcript is saved at: [TO FILL IN]
- The start of the transcript records the source (link or file name), the transcription tool and the date
- The intermediates were handled as agreed, and the outcome is noted in the Case's work log

## Exceptions and fallback

- The video has no audio track: stop and tell the user; do not make up text
- The audio is very long: transcribe it in segments and merge them, then check that the passages join up

## Revision log

None yet.
