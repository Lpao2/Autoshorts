# AutoShorts — Portfolio Edition

Automated local pipeline for transforming selected moments from long-form videos into vertical short-form content.

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![FFmpeg](https://img.shields.io/badge/FFmpeg-Video%20Processing-007808?logo=ffmpeg&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white)
![Status](https://img.shields.io/badge/Status-Functional-success)

> This repository contains the portfolio edition of a larger private automation project. It demonstrates the local media-processing pipeline while excluding credentials, authenticated sessions and production-specific integrations.

---

## About the project

AutoShorts receives a source video and a list of selected timestamps, cuts each moment with FFmpeg and exports vertical videos ready for short-form platforms.

The public edition focuses on the core engineering work:

- Parsing clip definitions
- Validating timestamps
- Cutting source videos
- Converting horizontal media to a vertical layout
- Creating a blurred background
- Preserving optional audio
- Loading configuration from environment variables
- Running the complete flow through a command-line interface

Production account integrations, authenticated browser sessions and private automation logic remain outside this repository.

## Pipeline

```mermaid
flowchart LR
    A[Source video] --> B[Timestamp file]
    B --> C[Clip parser]
    C --> D[Validation]
    D --> E[FFmpeg processing]
    E --> F[Vertical 1080x1920 layout]
    F --> G[Generated short videos]
```

## Example result

The foreground preserves the original video proportions while a blurred copy fills the vertical background.

Add a demonstration image to:

```text
docs/images/pipeline-demo.png
```

Then uncomment this line:

```markdown
<!-- ![AutoShorts processing demonstration](docs/images/pipeline-demo.png) -->
```

## Technologies

| Area | Technology |
|---|---|
| Language | Python |
| Media processing | FFmpeg |
| Configuration | python-dotenv |
| Interface | Command-line interface |
| Data formats | Text and JSON |
| Version control | Git and GitHub |

## Project structure

```text
autoshorts/
├── config/
│   └── pipeline.example.json
├── docs/
├── samples/
│   ├── cuts.demo.txt
│   └── cuts.example.txt
├── src/
│   └── autoshorts/
│       ├── __init__.py
│       ├── config.py
│       ├── main.py
│       ├── timestamp_parser.py
│       └── video_processor.py
├── tests/
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## Requirements

- Python 3.11 or newer
- FFmpeg available in the system `PATH`
- Windows PowerShell

## Installation

Clone the repository:

```powershell
git clone https://github.com/Lpao2/autoshorts.git
cd autoshorts
```

Create a virtual environment:

```powershell
python -m venv .venv
```

Install the dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Create the local configuration file:

```powershell
Copy-Item .env.example .env
```

The default settings use FFmpeg from the system `PATH` and write generated videos to `data/output`.

## Clip definition format

Create a text file using one clip per line:

```text
[00:15 - 00:42] - First gameplay highlight
[01:10 - 01:48] - Unexpected comeback
[00:02:05 - 00:02:47] - Final match-winning play
```

Accepted timestamp formats:

```text
MM:SS
HH:MM:SS
```

## Usage

Configure the Python source path:

```powershell
$env:PYTHONPATH = "$PWD\src"
```

Run the pipeline:

```powershell
.\.venv\Scripts\python.exe -m autoshorts.main `
  --input "path\to\source-video.mp4" `
  --cuts "samples\cuts.example.txt"
```

Use a custom output folder:

```powershell
.\.venv\Scripts\python.exe -m autoshorts.main `
  --input "path\to\source-video.mp4" `
  --cuts "samples\cuts.example.txt" `
  --output "path\to\output"
```

Generated files follow this pattern:

```text
01_First_gameplay_highlight.mp4
02_Unexpected_comeback.mp4
03_Final_match-winning_play.mp4
```

## Configuration

The `.env.example` file documents the available local settings:

```env
FFMPEG_PATH=C:\ffmpeg\bin\ffmpeg.exe
INPUT_DIR=./data/input
OUTPUT_DIR=./data/output
MAX_WORKERS=2
OUTPUT_WIDTH=1080
OUTPUT_HEIGHT=1920
MAX_CLIP_DURATION=60
EXTRA_CLIP_SECONDS=6
```

The real `.env` file is ignored by Git.

## Privacy and security

This repository does not include:

- Account credentials
- Browser cookies
- Authenticated sessions
- Channel identifiers
- Personal directories
- Production upload integrations
- Generated media files
- Private automation strategies

Sensitive and generated files are excluded through `.gitignore`.

## Status

The public local-processing pipeline is functional.

Current capabilities:

- Timestamp parsing
- Input validation
- Automatic filename sanitization
- Sequential clip processing
- Vertical video generation
- Blurred background composition
- Optional audio preservation
- CLI execution
- External configuration

Possible future improvements:

- Automated tests
- Subtitle rendering
- Parallel clip processing
- Progress reporting
- Optional content-analysis adapters
- Pluggable uploader interfaces

---

Developed by **[Lucas Patrocínio](https://github.com/Lpao2)**.
