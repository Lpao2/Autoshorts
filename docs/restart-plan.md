# AutoShorts restart plan

## Product direction

AutoShorts should become a personal, local-first production library for turning
different categories of long-form video into reviewable, channel-ready shorts.
Its advantage should be control, reusable theme configuration, privacy,
reproducibility, and provider choice—not a claim that every clip is
automatically viral.

The core must work without AI. Transcription, automatic selection, tracking,
and publishing should be optional adapters around a deterministic media engine.
Themes, prompts, render presets, metadata rules, and YouTube channel targets
must be data-driven rather than implemented as separate code paths.

See [product-vision.md](product-vision.md) for the domain model and intended
personal workflow.

## Competitive landscape

| Product | What it demonstrates | Implication for AutoShorts |
| --- | --- | --- |
| [OpusClip](https://www.opus.pro/) | Automatic clipping, captions, AI reframing, publishing, and API access | Full SaaS feature parity is not a sensible first milestone |
| [Klap](https://docs.klap.app/) | Video-to-shorts API, captions, reframing, presets, and OAuth integrations | Keep provider adapters possible; do not couple the core to one model |
| [AutoClip](https://github.com/zhouxiaoka/autoclip) | Mature local/open-source desktop, CLI, Docker, transcription, analysis, subtitles, and publishing | Differentiate through a smaller, auditable engine and excellent tests |
| [OpenShorts](https://github.com/mutonby/openshorts) | Open-source AI selection, face tracking, subtitles, dubbing, API, and MCP | Subject tracking and agent APIs are later capabilities, not foundation work |
| [Medusa Clip](https://github.com/Edualnog/medusa-clip) | Local gameplay-oriented highlight generation | Content type materially changes candidate-selection strategy |

## Phase 0 — make the current core trustworthy

Deliverables:

- Add `pyproject.toml` with an installable `autoshorts` CLI.
- Define supported Python and FFmpeg versions.
- Add typed configuration with one source of truth.
- Add unit tests for timestamps, filenames, settings, and filter generation.
- Generate a tiny synthetic video for integration tests.
- Probe inputs with FFprobe and validate requested clip boundaries.
- Render to temporary files, validate output, then rename atomically.
- Replace unconditional overwrite with `error`, `skip`, and `replace` modes.
- Produce a JSON manifest for every run.
- Add structured logging and actionable error types.

Exit criterion: a clean machine can install the project, run one command, and
produce a validated 9:16 MP4 from a synthetic fixture in CI.

## Phase 1 — usable local workflow

Deliverables:

- Progress events and clear batch summaries.
- Safe bounded parallel rendering.
- Resume and retry from a run manifest.
- Configurable layout profiles: contain/blur, crop, split-screen, and source
  aspect presets.
- Optional SRT/ASS subtitle burn-in with reusable styles.
- Preview frames or contact sheets before full rendering.
- Dry-run mode that prints and validates the render plan.
- Project library with SQLite-backed state and immutable configuration snapshots.
- Reusable themes, prompt sets, render presets, and metadata presets.

Exit criterion: a user can prepare, preview, render, interrupt, and resume a
batch without losing work or overwriting outputs unexpectedly.

## Phase 2 — assisted clip discovery

Deliverables:

- Transcript adapter interface.
- Local Whisper-compatible transcription adapter.
- Candidate-provider interface supporting manual timestamps, transcript
  heuristics, and model-assisted selection.
- Candidate scoring with explicit reasons and confidence.
- Deduplication and overlap resolution.
- Human review file or lightweight UI before rendering.
- Versioned prompt sets selected by theme and content type.
- Metadata generation for titles, descriptions, tags, and upload checklists.

Exit criterion: automatic suggestions improve speed while every final cut
remains inspectable and editable.

## Phase 3 — intelligent framing and presentation

Deliverables:

- Face/person/object tracking adapter.
- Smoothed crop paths with safe zones for captions and platform UI.
- Speaker-aware or gameplay-aware framing profiles.
- Dynamic captions, keyword emphasis, branding templates, and thumbnails.
- Objective visual checks for black frames, frozen output, clipping, and audio
  loudness.

## Phase 4 — controlled distribution

Deliverables:

- Stable uploader protocol and mock implementation.
- Ready-to-post export packages as the first publishing milestone.
- Multiple YouTube channel profiles through the official API.
- Platform adapters only through supported APIs or explicit export packages.
- Idempotency, scheduling, retry, audit logs, and approval before publish.
- Secrets stored through environment or OS credential facilities, never Git.

## First implementation sprint

1. Package the project and add development tooling.
2. Write parser and renderer unit tests around current behavior.
3. Add FFprobe domain models and source/clip validation.
4. Add atomic output and overwrite policies.
5. Add an end-to-end synthetic-video test.
6. Replace the unused JSON/environment split with one documented config path.

## Theme strategy for Phase 2

The engine is intentionally multi-theme, but automatic selection should be
validated one profile at a time. Talking-head content can prioritize transcript
coherence while gameplay can combine audio peaks, motion, OCR, and visual
change. Both use the same candidate interface, review flow, renderer, and
publication-package model.
