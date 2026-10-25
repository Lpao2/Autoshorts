<div align="center">

# AutoShorts

### From long-form video to publish-ready Shorts.

An automated, local-first pipeline for discovering, understanding, editing,
scheduling, and publishing short-form video across multiple YouTube channels.

![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white)
![FFmpeg](https://img.shields.io/badge/FFmpeg-007808?style=for-the-badge&logo=ffmpeg&logoColor=white)
![Gemini](https://img.shields.io/badge/Gemini-8E75B2?style=for-the-badge&logo=googlegemini&logoColor=white)
![YouTube](https://img.shields.io/badge/YouTube_API-FF0000?style=for-the-badge&logo=youtube&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?style=for-the-badge&logo=sqlite&logoColor=white)
![Tests](https://img.shields.io/badge/tests-493_passing-2EA44F?style=for-the-badge)

</div>

---

## About

**AutoShorts** turns authorized long-form videos into vertical, captioned, and
reviewable YouTube Shorts.

It combines local media processing with AI-assisted editorial analysis and a
stateful publishing workflow. Every important step is persisted, auditable, and
safe to resume after interruption — from source ingestion to confirmed upload.

The platform was designed to operate a multi-channel content network without
sacrificing editorial control, channel identity, or upload safety.

## Pipeline

<div align="center">

```text
Source discovery
      ↓
Download & media validation
      ↓
Local transcription with Faster Whisper
      ↓
Gemini analysis, candidate validation & ranking
      ↓
Vertical render, theme crop & synchronized captions
      ↓
Metadata review or controlled auto-approval
      ↓
Scheduling & idempotent YouTube publishing
```

</div>

## Main features

### Intelligent video production

- Source discovery and authenticated collection with `yt-dlp`
- Sampled FFmpeg validation for faster ingestion
- Local transcription with Faster Whisper and reusable cache
- Structured Gemini analysis and editorial reranking
- Quality gates for timestamps, duration, transcript, context, and score
- 9:16 rendering with theme-specific crop profiles and burned-in captions

### Multi-channel operation

- Catalog designed for **20 themes and up to 60 channels**
- Independent editorial, visual, and posting profiles
- Versioned Brand Plans, logos, banners, checksums, and provenance
- Inventory-based replenishment with fairness between channels
- Manual review and configurable `AUTO_APPROVE` policies

### YouTube integration

- Multi-account OAuth with strict Channel ID validation
- Persistent browser sessions isolated by Google Identity
- Idempotent channel provisioning and reconciliation
- Explicit audience, synthetic-media, paid-promotion, category, language,
  license, embedding, statistics, and privacy metadata
- Persistent scheduling through market-aware posting windows

### Reliability and safety

- SQLite-backed state machine with incremental migrations
- Recovery without repeating downloads, transcription, or rendering
- Duplicate protection for channels, Publications, jobs, and uploads
- Ambiguous uploads stop at `UPLOAD_UNCERTAIN` instead of retrying blindly
- Local media is removed only after confirmed publication
- Health checks, backups, retention jobs, and continuous `systemd` services

## Tech stack

### Core

- Python 3.11+
- FFmpeg and FFprobe
- SQLite
- Tkinter desktop GUI

### AI and media

- Faster Whisper
- Google Gemini
- yt-dlp

### YouTube automation

- YouTube Data API v3
- Google OAuth 2.0
- Playwright
- Brave / Chromium persistent profiles

### Operations

- systemd
- Ruff
- pytest

## Current status

AutoShorts currently supports the complete production lifecycle: ingestion,
transcription, analysis, candidate selection, rendering, captions, branding,
review, scheduling, publishing, recovery, and post-publication retention.

The current state schema is **30**, backed by **493 automated tests** covering the
domain, persistence, GUI, media pipeline, AI integrations, branding, OAuth,
provisioning, scheduler, publishing, orchestration, and recovery.

## Quick start

```bash
git clone https://github.com/Lpao2/Autoshorts.git
cd Autoshorts
python -m venv .venv
.venv/bin/pip install -e '.[dev,automation,transcription,analysis,youtube-oauth]'
cp .env.example .env
```

Launch the desktop interface:

```bash
.venv/bin/autoshorts-gui
```

Run the quality gate:

```bash
.venv/bin/ruff check .
.venv/bin/pytest
.venv/bin/python -m compileall src
git diff --check
```

## Documentation

- [Production cycle](docs/production-cycle.md)
- [Local GUI](docs/local-gui.md)
- [Gemini analysis](docs/gemini-analysis.md)
- [Metadata review](docs/metadata-review.md)
- [YouTube OAuth](docs/youtube-oauth.md)
- [Channel provisioning](docs/youtube-channel-provisioning.md)
- [Scheduler](docs/scheduler.md)
- [Recovery](docs/recovery.md)

## Repository

This public repository is the sanitized portfolio edition of AutoShorts.

Credentials, OAuth tokens, browser profiles, cookies, production databases,
private channel configuration, and generated media are never versioned. The
private repository contains the active operational history and deployment state.

## Author

Developed by **Lucas Patrocínio**.

[![GitHub](https://img.shields.io/badge/GitHub-Lpao2-181717?style=for-the-badge&logo=github)](https://github.com/Lpao2)

## Code usage

This repository is available for portfolio and demonstration purposes.

No open-source license is provided. Copying, redistributing, modifying, or using
the project commercially without explicit authorization is not permitted.

---

<div align="center">
  <sub>AutoShorts — automated production with editorial control.</sub>
</div>
