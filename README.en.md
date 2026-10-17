# Screen Studio Smart Rough Cut · screenstudio-edit

[中文](README.md) | English

This is an automated editing skill that I use myself and keep iterating on. I wanted an automated editing workflow that actually works, so I can produce good content efficiently.

This version handles the first step: a rough cut of Screen Studio recordings, aimed at slide and whiteboard walkthrough videos. Next I will add smart editing for talking-head videos, and gradually bring every editing step I use for content into this skill.

It takes over a lot of repetitive, tedious editing: it removes dead air, failed retakes and verbal slips, and works out the cut points and screen changes for you. It then saves a copy of the project that you can keep refining in Screen Studio. The original project is never modified.

The heaviest work is done by local scripts, without the AI. The audio is transcribed to text first, and the AI edits based on that text, so it uses very few tokens. When you edit the same recording again, the transcript and detection results are reused.

It is an Agent Skill: any AI assistant that can read `SKILL.md` and run commands, such as Claude Code or Codex, can use it. Not affiliated with Screen Studio.

## What it does

- **Removes dead air**: pauses longer than 300 ms are trimmed down to a 180 ms breath. Soft word endings and anything the detectors disagree on are kept; it would rather cut too little than too much.
- **Removes retakes**: the AI reads the whole transcript first. When a sentence was said several times, it keeps the complete, fluent take. Anything deleted must be covered by what remains; one-off actions, numbers, qualifiers and reminders stay.
- **Respects your manual edits**: zooms, speed changes, layouts and volume you already set are kept as they are. Voice, screen and camera are cut at the same points, so audio and video stay in sync.
- **Picks up where you left off**: edit in Screen Studio, hand it back, and earlier decisions carry over. Only the affected parts are reviewed again, and nothing is re-transcribed.

Out of scope: plain MP4 editing, subtitles, background removal, swapping slide footage, merging pickup recordings, publishing.

## Requirements

- macOS and a Screen Studio 4 project (`.screenstudio`): one scene, one recording session, with microphone and screen recording; a camera is optional
- Python 3.11 or newer and ffmpeg: `brew install python ffmpeg`
- Transcription uses FunASR on Alibaba Cloud Bailian through the Bailian CLI:

  ```bash
  npm install -g bailian-cli
  bl auth login
  ```

  If you already have a transcript with word-level timestamps, you can import it and skip Bailian.

## Install

Claude Code:

```bash
git clone https://github.com/Jianing1117/screenstudio-edit.git ~/.claude/skills/screenstudio-edit
bash ~/.claude/skills/screenstudio-edit/scripts/setup.sh
```

Codex: replace `~/.claude/skills` with `~/.codex/skills`. For other assistants, put it wherever they load skills from.

`setup.sh` creates a separate Python environment (`.venv`) inside the skill folder and installs only numpy and onnxruntime. Update later with `git pull`.

## Usage

Tell your AI assistant:

- "Use screenstudio-edit to rough-cut this recording: `~/Screen Studio Projects/xxx.screenstudio`"
- "Only remove dead air, don't cut any content"
- After editing in Screen Studio: "Continue editing this version"

It works in four steps:

1. **Prepare**: opens the project read-only, transcribes the narration, and detects pauses and screen activity (clicks, key presses, screen changes). It asks for your consent before the first transcription.
2. **Write the plan**: the AI reads the transcript and decides which retakes to remove. Pauses with on-screen activity are checked one by one, and kept where viewers need time to read the screen.
3. **Dry run**: the scripts validate the plan, compute the final cut points, and report how much was removed and which uncertain spots were kept.
4. **Save a copy**: the result is saved next to the source project as `<name>-初剪-v1.screenstudio` (初剪 means "rough cut"); the next run produces v2. Open it in Screen Studio to keep refining.

The AI does not actually listen to the result. Please watch and listen to the whole cut in Screen Studio before you publish.

The skill instructions (`SKILL.md`) are written in Chinese; AI assistants follow them fine whatever language you talk to them in.

## Safety and privacy

- The original project is read-only. Results are saved as new copies and never overwrite existing files.
- Transcription is the only step that goes online: it uploads the microphone track to Alibaba Cloud Bailian. Detection and editing run on your machine.
- Keystroke logs are read for timing only, never for which keys were pressed.
- No keys or credential files are read. Bailian authorization is handled by `bl` itself.
- Transcripts and detection results are cached in `~/.screenstudio-edit` (change it with the `SCREENSTUDIO_EDIT_HOME` environment variable). Copying a project or changing file times does not invalidate the cache; delete the folder and the next run transcribes again.

## Current limitations

- Transcription only supports Bailian FunASR and has only been tested on Mandarin recordings.
- A basic preview MP4 can be generated, but it does not reproduce Screen Studio's zoom animations, cursor or styling. It is only for a quick look at the cut points.
- Projects with a separate voice-over track, multiple recording sessions or unmuted system audio are refused rather than cut blindly.

## Development

See [references/development.md](references/development.md) (in Chinese) for the structure and how to run the tests.

## Credits and license

Voice activity detection uses [Silero VAD](https://github.com/snakers4/silero-vad) (MIT); the model file ships in `assets/`.

Released under the MIT License. Designed and maintained by Jianing ([@Jianing1117](https://github.com/Jianing1117)).
