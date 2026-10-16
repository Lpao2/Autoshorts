# Manual end-to-end test

This procedure validates the current local pipeline with a real video. It does
not upload anything and does not use an AI or cloud provider.

## 1. Requirements

- Python 3.11 or newer.
- FFmpeg and FFprobe available in `PATH`, or their paths configured in `.env`.
- A local source video you are allowed to process.

Confirm the tools:

```text
python --version
ffmpeg -version
ffprobe -version
```

## 2. Install the project

### Windows PowerShell

```powershell
git clone https://github.com/Lpao2/Autoshorts.git
cd Autoshorts
git switch codex/resume-audit
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

If FFmpeg is not in `PATH`, edit `.env`:

```env
FFMPEG_PATH=C:\ffmpeg\bin\ffmpeg.exe
FFPROBE_PATH=C:\ffmpeg\bin\ffprobe.exe
```

### Linux

```bash
git clone https://github.com/Lpao2/Autoshorts.git
cd Autoshorts
git switch codex/resume-audit
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
```

## 3. Run the automated checks

Windows:

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\pytest.exe
```

Linux:

```bash
.venv/bin/ruff check .
.venv/bin/pytest
```

Expected result: all tests pass, including the synthetic FFmpeg integration
test.

## 4. Prepare a cut file

Create `my-cuts.txt`:

```text
# Empty lines and comments are allowed.
[00:15 - 00:35] - Opening explanation
[01:10 - 01:42] - Strong second moment
[00:02:05 - 00:02:45] - Final example
```

Every other non-empty line is treated as an error. This prevents malformed
model output from being silently ignored.

By default, AutoShorts adds six seconds after each selected interval. Set
`EXTRA_CLIP_SECONDS=0` in `.env` when exact boundaries are required.

## 5. Validate without rendering

Windows:

```powershell
.\.venv\Scripts\autoshorts.exe `
  --input "C:\Videos\source.mp4" `
  --cuts ".\my-cuts.txt" `
  --output ".\data\output" `
  --dry-run
```

Linux:

```bash
.venv/bin/autoshorts \
  --input /path/to/source.mp4 \
  --cuts ./my-cuts.txt \
  --output ./data/output \
  --dry-run
```

The dry run probes the source, validates every timestamp, calculates final
durations and filenames, and writes a `manifest-*.json`. It creates no MP4.

## 6. Render

Run the same command without `--dry-run`:

```powershell
.\.venv\Scripts\autoshorts.exe `
  --input "C:\Videos\source.mp4" `
  --cuts ".\my-cuts.txt" `
  --output ".\data\output" `
  --workers 2
```

Use `--workers 1` on lower-end machines. Each worker is a separate FFmpeg
process and can consume substantial CPU and memory.

## 7. Re-run behavior

The default policy is `error`, so existing clips are protected. Choose a policy
explicitly when re-running:

```text
--overwrite error    refuse to overwrite
--overwrite skip     validate and reuse existing outputs
--overwrite replace  render and atomically replace outputs
```

## 8. Verify the result

The output directory should contain:

```text
01_Opening_explanation.mp4
02_Strong_second_moment.mp4
03_Final_example.mp4
manifest-<run-id>.json
```

Check:

- video is vertical and displays the entire source over a blurred background;
- audio remains synchronized;
- start/end moments are appropriate;
- generated files open normally;
- the manifest reports every clip as `generated` or `skipped`;
- no `.part.mp4` or `.part.json` files remain.

If a command fails, preserve the terminal output and the generated manifest.
Do not share source media, credentials, or private paths in a public issue.
