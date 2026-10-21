# Single-winner production smoke test

This flow takes one downloaded source through selection, neutral base rendering,
burned-in captions, and technical validation to `READY`. It does not create a
Publication, enqueue media, upload, or add branding.

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

The command renders only the persisted winner as a 1080x1920 H.264/AAC base,
creates or reuses the neutral caption derivative, validates it with FFprobe and a
full FFmpeg decode, computes SHA-256, and persists the final READY artifact.

Run the same command again. It must report `Result: REUSED` and `State: READY`;
download, transcript, analysis, selection, base render, captions, manifests, and
the database rows must not be duplicated.

## Required checks

- source video state: `READY` (not `QUEUED`, `PUBLISHED`, or `COMPLETED`);
- exactly one transcript, analysis, selection, production manifest, and Short;
- exactly one MP4 in the source creator output directory;
- no new Publication and no upload;
- final output streams: 1080x1920 H.264 video and AAC audio;
- `ready_artifacts` identifies one final path, manifest, subtitle, fingerprint,
  validation timestamp, and checksum for the source video.
