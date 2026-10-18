"""Gemini transcript analysis adapter using the official google-genai SDK."""

from __future__ import annotations

import os
import time
from threading import Event
from typing import Any

from autoshorts.analysis_cost import estimate_cost_usd
from autoshorts.automation_events import check_automation_cancelled
from autoshorts.transcript_analysis import (
    AnalysisResult,
    AnalysisUsage,
    ClipSelection,
)
from autoshorts.transcription import Transcript

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"


class GeminiAnalysisError(ValueError):
    def __init__(self, message: str, usage: AnalysisUsage) -> None:
        super().__init__(message)
        self.usage = usage


BASE_PROMPT = """Você seleciona momentos de vídeos para Shorts.
Escolha exatamente 3 momentos pelos IDs dos segmentos. Priorize retenção, clareza
isolada e diversidade. Evite introduções lentas, dependência excessiva de contexto
e três cortes parecidos. Cada trecho deve durar 20–60 segundos; prefira 30–50
segundos sem alongamento artificial. A headline deve usar o idioma predominante,
ser natural, honesta e curiosa (idealmente 30–70 caracteres), sem hashtags, emojis,
clickbait falso, spoiler completo ou fatos ausentes. Não invente timestamps."""

RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["selections"],
    "properties": {
        "selections": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "start_segment",
                    "end_segment",
                    "title",
                    "headline",
                    "reason",
                    "score",
                ],
                "properties": {
                    "start_segment": {"type": "integer", "minimum": 0},
                    "end_segment": {"type": "integer", "minimum": 0},
                    "title": {"type": "string"},
                    "headline": {"type": "string"},
                    "reason": {"type": "string"},
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        }
    },
}


class GeminiAnalysisProvider:
    provider_name = "gemini"

    def __init__(self, *, model: str | None = None, client: Any | None = None) -> None:
        self.model = model or os.environ.get(
            "AUTOSHORTS_GEMINI_MODEL", DEFAULT_GEMINI_MODEL
        )
        api_key = os.environ.get("GEMINI_API_KEY")
        if client is None and not api_key:
            raise RuntimeError("Gemini não configurado. Defina GEMINI_API_KEY.")
        if client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise RuntimeError(
                    "Gemini exige o extra analysis: pip install -e '.[analysis]'"
                ) from exc
            client = genai.Client(api_key=api_key)
        self._client = client

    def analyze(
        self,
        transcript: Transcript,
        *,
        title: str,
        creator: str,
        theme: str,
        theme_prompt: str,
        cancel: Event | None = None,
    ) -> AnalysisResult:
        check_automation_cancelled(cancel)
        estimate_cost_usd(self.model, 0, 0)
        prompt = _build_prompt(transcript, title, creator, theme, theme_prompt)
        started = time.monotonic()
        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={
                "system_instruction": BASE_PROMPT,
                "response_mime_type": "application/json",
                "response_json_schema": RESPONSE_SCHEMA,
                "thinking_config": {"thinking_level": "low"},
                "temperature": 0.3,
            },
        )
        duration = time.monotonic() - started
        usage = _usage_from_response(response, self.model, duration)
        try:
            parsed = response.parsed
            if not isinstance(parsed, dict):
                raise ValueError("Gemini retornou structured output inválido.")
            raw = parsed.get("selections")
            if not isinstance(raw, list):
                raise ValueError("Gemini não retornou a lista selections.")
            selections = tuple(
                ClipSelection(
                    item["start_segment"],
                    item["end_segment"],
                    item["title"],
                    item["headline"],
                    item["reason"],
                    item["score"],
                )
                for item in raw
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GeminiAnalysisError(str(exc), usage) from exc
        return AnalysisResult(selections, self.provider_name, self.model, usage)


def _build_prompt(
    transcript: Transcript, title: str, creator: str, theme: str, theme_prompt: str
) -> str:
    guidance = theme_prompt.strip() or "(sem orientação adicional)"
    segments = "\n".join(
        f"[{index}] {segment.start:.2f}-{segment.end:.2f} | {segment.text.strip()}"
        for index, segment in enumerate(transcript.segments)
    )
    return (
        f"VIDEO\ntitle: {title}\ncreator: {creator}\ntheme: {theme}\n"
        f"duration: {transcript.duration_seconds:.2f}s\n\nTHEME GUIDANCE\n{guidance}"
        f"\n\nTRANSCRIPT\n{segments}"
    )


def _usage_from_response(response: Any, model: str, duration: float) -> AnalysisUsage:
    metadata = getattr(response, "usage_metadata", None)
    prompt = int(getattr(metadata, "prompt_token_count", 0) or 0)
    output = int(getattr(metadata, "candidates_token_count", 0) or 0)
    thoughts = int(getattr(metadata, "thoughts_token_count", 0) or 0)
    total = int(
        getattr(metadata, "total_token_count", 0) or (prompt + output + thoughts)
    )
    billed_output = max(output + thoughts, total - prompt)
    cost = estimate_cost_usd(model, prompt, billed_output)
    return AnalysisUsage(prompt, output, thoughts, total, duration, cost)
