# Neutral burned-in caption smoke test

This stage creates one captioned derivative from an existing rendered Short. It
does not replace the base Short, create a Publication, enqueue media, schedule a
job, or upload anything.

## Run

```console
.venv/bin/autoshorts-produce caption --source-video-id U4ZWhmwcLj8
```

The command reuses the persisted word timestamps, resolves the first exact font
available from the neutral fallback chain, creates an ASS file beside the base
Short, burns it into a new H.264 video while copying AAC audio, validates the full
decode, and extracts three PNG samples.

The fallback chain is Montserrat ExtraBold, Montserrat Bold, Inter ExtraBold,
Inter Bold, then DejaVu Sans Bold. Fonts are never downloaded by this flow.

Run the same command again. With unchanged input, transcript, font, and preset it
must report `Result: REUSED` and retain the same fingerprint, paths, and checksum.

## Validation

- captions use source word timestamps normalized by the selected segment start;
- every event satisfies `0 <= start < end <= Short duration`;
- no caption exceeds two lines or the configured horizontal safe width;
- ASS and manifest record font, sizing, outline, shadow, margins, safe area,
  transcript provenance, word timestamps, output checksum, and sample paths;
- output remains 1080x1920 H.264 with AAC audio and the base duration;
- the base Short and Publications remain unchanged.
