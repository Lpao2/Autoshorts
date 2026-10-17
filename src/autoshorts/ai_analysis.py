"""Provider-neutral parsing for the legacy clip-analysis response format."""

from __future__ import annotations

import re
from dataclasses import dataclass

from autoshorts.timestamp_parser import Clip, parse_cuts


@dataclass(frozen=True, slots=True)
class ClipAnalysis:
    clips: tuple[Clip, ...]
    raw_response: str


def parse_analysis_response(response: str) -> ClipAnalysis:
    """Parse marked legacy responses into the current Clip domain objects."""
    match = re.search(
        r"INICIODARESPOSTA\s*(.*?)\s*FIMDARESPOSTA",
        response,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        raise ValueError(
            "AI response does not contain INICIODARESPOSTA/FIMDARESPOSTA markers."
        )
    lines: list[str] = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^corte\s*\d+\s*:\s*", "", line, flags=re.IGNORECASE)
        lines.append(line)
    clips = tuple(parse_cuts("\n".join(lines)))
    return ClipAnalysis(clips=clips, raw_response=response)
