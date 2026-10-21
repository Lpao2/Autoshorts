"""Gemini implementation of comparative editorial candidate reranking."""

from __future__ import annotations

import os
import time
from threading import Event
from typing import Any

from autoshorts.analysis_cost import estimate_cost_usd
from autoshorts.automation_events import check_automation_cancelled
from autoshorts.gemini_analysis_provider import (
    DEFAULT_GEMINI_MODEL,
    GeminiAnalysisError,
    _usage_from_response,
)
from autoshorts.gemini_calls import call_gemini, create_gemini_client
from autoshorts.reranking import (
    CandidateReranker,
    RankedSelection,
    RerankCandidate,
    RerankResult,
    rerank_prompt_fingerprint,
)

RERANK_PROMPT = """Compare cinco candidatos de Shorts lado a lado e escolha os três
que você realmente publicaria. Não escolha apenas o trecho mais informativo.
Priorize o que prenderia uma pessoa nos primeiros segundos e funcionaria isolado.
Avalie hook, clareza sem contexto, payoff, energia, reação, humor, surpresa, tensão,
retenção, inteligibilidade, adequação ao tema, coerência da headline e diversidade
do conjunto final. Escolha somente IDs existentes e não altere os candidatos."""
RERANK_MODEL_ENV = "AUTOSHORTS_GEMINI_RERANK_MODEL"

RERANK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ranking"],
    "properties": {
        "ranking": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["candidate_id", "rank", "reason", "editorial_score"],
                "properties": {
                    "candidate_id": {"type": "string"},
                    "rank": {"type": "integer", "minimum": 1, "maximum": 3},
                    "reason": {"type": "string"},
                    "editorial_score": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        }
    },
}


class GeminiCandidateReranker(CandidateReranker):
    provider_name = "gemini"

    def __init__(self, *, model: str | None = None, client: Any) -> None:
        self.model = model or os.environ.get(RERANK_MODEL_ENV, DEFAULT_GEMINI_MODEL)
        self._client = client

    @classmethod
    def configured(cls, *, model: str | None = None):
        return cls(model=model, client=create_gemini_client())

    def prompt_hash(self, theme_prompt: str) -> str:
        return rerank_prompt_fingerprint(RERANK_PROMPT, theme_prompt)

    def rerank(
        self,
        candidates: tuple[RerankCandidate, ...],
        *,
        theme: str,
        theme_prompt: str,
        title: str,
        creator: str,
        youtube_id: str,
        cancel: Event | None = None,
    ) -> RerankResult:
        check_automation_cancelled(cancel)
        estimate_cost_usd(self.model, 0, 0)
        prompt = _build_rerank_prompt(
            candidates, theme, theme_prompt, title, creator, youtube_id
        )
        started = time.monotonic()
        response = call_gemini(
            operation="candidate_reranking",
            model=self.model,
            request=lambda: self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config={
                    "system_instruction": RERANK_PROMPT,
                    "response_mime_type": "application/json",
                    "response_json_schema": RERANK_SCHEMA,
                    "thinking_config": {"thinking_level": "low"},
                    "temperature": 0.2,
                },
            ),
        )
        usage = _usage_from_response(response, self.model, time.monotonic() - started)
        try:
            raw = response.parsed["ranking"]
            ranking = tuple(
                RankedSelection(
                    str(x["candidate_id"]),
                    int(x["rank"]),
                    str(x["reason"]),
                    float(x["editorial_score"]),
                )
                for x in raw
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GeminiAnalysisError(str(exc), usage) from exc
        return RerankResult(
            ranking,
            self.provider_name,
            self.model,
            self.prompt_hash(theme_prompt),
            usage,
        )


def _build_rerank_prompt(
    candidates: tuple[RerankCandidate, ...],
    theme: str,
    theme_prompt: str,
    title: str,
    creator: str,
    youtube_id: str,
) -> str:
    blocks = []
    for item in candidates:
        selection = item.selection
        blocks.append(
            f"ID: {item.candidate_id}\nHEADLINE: {selection.headline}\n"
            f"TITLE: {selection.title}\nREASON: {selection.reason}\n"
            f"DURATION: {item.duration_seconds:.2f}s\nTRANSCRIPT:\n"
            f"{item.transcript_text}"
        )
    return (
        f"VIDEO\nyoutube_id: {youtube_id}\ntitle: {title}\ncreator: {creator}\n"
        f"theme: {theme}\n\nTHEME GUIDANCE\n{theme_prompt.strip()}\n\nCANDIDATES\n\n"
        + "\n\n".join(blocks)
    )
