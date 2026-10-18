from types import SimpleNamespace

import pytest

from autoshorts.analysis_cost import estimate_cost_usd
from autoshorts.gemini_analysis_provider import GeminiAnalysisProvider
from autoshorts.transcript_analysis import (
    AnalysisResult,
    ClipSelection,
    analysis_from_dict,
    analysis_to_dict,
    make_resolved_analysis,
    resolve_clip_selections,
)
from autoshorts.transcription import Transcript, TranscriptSegment, TranscriptWord


def transcript():
    return Transcript(
        "pt",
        0.99,
        120,
        "texto",
        tuple(
            TranscriptSegment(
                index * 10,
                (index + 1) * 10,
                f"fala {index}",
                (TranscriptWord(index * 10, index * 10 + 1, f"palavra{index}"),),
            )
            for index in range(12)
        ),
        "faster-whisper",
        "small",
    )


def selections():
    return (
        ClipSelection(
            0, 2, "Terceiro", "Uma chamada natural para o terceiro", "motivo 3", 0.7
        ),
        ClipSelection(
            6, 8, "Primeiro", "A melhor jogada aconteceu bem aqui", "motivo 1", 0.95
        ),
        ClipSelection(
            3, 5, "Segundo", "Ele não esperava por essa reação", "motivo 2", 0.8
        ),
    )


def test_resolver_uses_segment_boundaries_and_score_order():
    clips = resolve_clip_selections(
        transcript(), AnalysisResult(selections(), "test", "model")
    )
    assert [(item.start_seconds, item.end_seconds) for item in clips.clips] == [
        (60, 90),
        (30, 60),
        (0, 30),
    ]


@pytest.mark.parametrize(
    "replacement",
    [
        ClipSelection(0, 0, "curto", "Headline válida para esse corte", "x", 0.5),
        ClipSelection(0, 2, "x", "", "x", 0.5),
        ClipSelection(0, 2, "x", "x" * 101, "x", 0.5),
    ],
)
def test_invalid_duration_or_headline_is_rejected(replacement):
    result = AnalysisResult((replacement, *selections()[1:]), "test", "model")
    with pytest.raises(ValueError):
        resolve_clip_selections(transcript(), result)


def test_requires_exactly_three_selections():
    with pytest.raises(ValueError, match="exatamente 3"):
        resolve_clip_selections(
            transcript(), AnalysisResult(selections()[:2], "test", "model")
        )


@pytest.mark.parametrize(
    "replacement",
    [
        ClipSelection(2, 4, "overlap", "Headline válida para overlap", "x", 0.6),
        ClipSelection(20, 22, "fora", "Headline válida para índice", "x", 0.6),
        ClipSelection(0, 2, "", "Headline válida para título", "x", 0.6),
        ClipSelection(0, 2, "score", "Headline válida para score", "x", 1.1),
    ],
)
def test_semantic_validation_rejects_overlap_index_title_and_score(replacement):
    result = AnalysisResult((selections()[0], replacement, selections()[2]), "p", "m")
    with pytest.raises(ValueError):
        resolve_clip_selections(transcript(), result)


def test_analysis_artifact_preserves_headline():
    resolved = make_resolved_analysis(
        youtube_id="video",
        theme="lol",
        theme_prompt="rage",
        transcript=transcript(),
        result=AnalysisResult(selections(), "test", "model"),
    )
    restored = analysis_from_dict(
        analysis_to_dict(resolved), transcript(), youtube_id="video"
    )
    assert {item.headline for item in restored.result.selections} == {
        item.headline for item in selections()
    }


class FakeModels:
    def __init__(self):
        self.kwargs = None

    def generate_content(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            parsed={
                "selections": [
                    {
                        "start_segment": item.start_segment,
                        "end_segment": item.end_segment,
                        "title": item.title,
                        "headline": item.headline,
                        "reason": item.reason,
                        "score": item.score,
                    }
                    for item in selections()
                ]
            },
            usage_metadata=SimpleNamespace(
                prompt_token_count=1000,
                candidates_token_count=100,
                thoughts_token_count=50,
                total_token_count=1150,
            ),
        )


def test_gemini_uses_structured_output_theme_prompt_and_real_usage():
    models = FakeModels()
    provider = GeminiAnalysisProvider(client=SimpleNamespace(models=models))
    result = provider.analyze(
        transcript(),
        title="Título",
        creator="Criador",
        theme="lol",
        theme_prompt="Priorize humor",
    )
    assert (
        models.kwargs["config"]["response_json_schema"]["properties"]["selections"][
            "minItems"
        ]
        == 3
    )
    assert "Priorize humor" in models.kwargs["contents"]
    assert "palavra0" not in models.kwargs["contents"]
    assert result.usage.total_tokens == 1150
    assert result.usage.estimated_cost_usd == estimate_cost_usd(
        "gemini-3.8-flash", 1000, 150
    )
