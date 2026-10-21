# Single-winner production smoke test

This flow produces one neutral vertical master from an already downloaded source.
It does not create a Publication, enqueue media, upload, caption, or add branding.

## Preconditions

- The source is registered as `DOWNLOADED` and its validated local file exists.
- `config/automation.local.json` contains the source theme.
- The local Whisper and Gemini analysis extras are installed and configured.
- FFmpeg and FFprobe are available.

## Dry-run

```console
.venv/bin/autoshorts-produce run \
  --source-video-id U4ZWhmwcLj8 \
  --dry-run
```

The dry-run may create reusable transcript, analysis, and selection artifacts. It
must print the exact interval, reason, transcript context, and planned output, then
stop before rendering. The video state becomes `SEGMENT_SELECTED`.

## Render

```console
.venv/bin/autoshorts-produce run --source-video-id U4ZWhmwcLj8
```

The command renders only the persisted winner as 1080x1920 H.264/AAC, validates
it with FFprobe and a full FFmpeg decode, computes SHA-256, atomically persists one
Short, and writes one deterministic `production.json` manifest.

Run the same command again. It must report `Result: REUSED`; transcript, analysis,
selection, output, manifest, and the database Short row must not be duplicated.

## Required checks

- source video state: `RENDERED` (not `USED`, `QUEUED`, or `COMPLETED`);
- exactly one transcript, analysis, selection, production manifest, and Short;
- exactly one MP4 in the source creator output directory;
- no new Publication and no upload;
- output streams: 1080x1920 H.264 video and AAC audio;
- output checksum matches `production.json`.
