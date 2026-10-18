from types import SimpleNamespace

import pytest

from autoshorts.analysis_cost import estimate_cost_usd
from autoshorts.gemini_analysis_provider import GeminiAnalysisProvider
from autoshorts.transcript_analysis import (
    AnalysisResult,
    ClipPolicy,
    ClipSelection,
    analysis_from_dict,
    analysis_to_dict,
    make_resolved_analysis,
    resolve_clip_selections,
    select_top_candidates,
)
from autoshorts.transcription import Transcript, TranscriptSegment, TranscriptWord


def transcript():
    return Transcript(
        "pt",
        0.99,
        180,
        "texto",
        tuple(
            TranscriptSegment(
                index * 10,
                (index + 1) * 10,
                f"fala {index}",
                (TranscriptWord(index * 10, index * 10 + 1, f"palavra{index}"),),
            )
            for index in range(18)
        ),
        "faster-whisper",
        "small",
    )


def selections():
    return (
        ClipSelection(
            0, 2, "Terceiro", "Uma chamada natural para o terceiro", "motivo 3", 0.77
        ),
        ClipSelection(
            6, 8, "Primeiro", "A melhor jogada aconteceu bem aqui", "motivo 1", 0.95
        ),
        ClipSelection(
            3, 5, "Segundo", "Ele não esperava por essa reação", "motivo 2", 0.8
        ),
    )


def candidate_pool():
    return selections() + (
        ClipSelection(
            9, 11, "Quarto", "Uma quarta chamada bem diferente", "motivo 4", 0.79
        ),
        ClipSelection(
            12, 14, "Quinto", "Uma quinta chamada para o momento", "motivo 5", 0.76
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


def test_candidate_pool_selects_three_best_valid_candidates():
    selected = select_top_candidates(transcript(), candidate_pool())
    assert [item.score for item in selected] == [0.95, 0.8, 0.79]


def test_candidate_pool_requires_three_above_threshold():
    with pytest.raises(ValueError, match="Qualidade insuficiente"):
        select_top_candidates(
            transcript(), candidate_pool(), ClipPolicy(min_score=0.81)
        )


def test_candidate_pool_filters_overlap_and_duplicate_headline():
    pool = list(candidate_pool())
    pool[1] = ClipSelection(
        1, 3, "Sobreposto", "Uma chamada natural para o terceiro", "x", 0.99
    )
    selected = select_top_candidates(transcript(), tuple(pool))
    assert len(selected) == 3
    assert len({item.headline.casefold() for item in selected}) == 3


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


def test_analysis_artifact_preserves_five_candidates_and_top_three():
    result = AnalysisResult((), "gemini", "model", candidates=candidate_pool())
    resolved = make_resolved_analysis(
        youtube_id="video",
        theme="lol",
        theme_prompt="humor",
        transcript=transcript(),
        result=result,
    )
    restored = analysis_from_dict(
        analysis_to_dict(resolved), transcript(), youtube_id="video"
    )
    assert len(restored.result.candidates) == 5
    assert [item.score for item in restored.result.selections] == [0.95, 0.8, 0.79]


class FakeModels:
    def __init__(self):
        self.kwargs = None

    def generate_content(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            parsed={
                "candidates": [
                    {
                        "start_segment": item.start_segment,
                        "end_segment": item.end_segment,
                        "title": item.title,
                        "headline": item.headline,
                        "reason": item.reason,
                        "score": item.score,
                    }
                    for item in candidate_pool()
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
        youtube_id="video",
    )
    assert (
        models.kwargs["config"]["response_json_schema"]["properties"]["candidates"][
            "minItems"
        ]
        == 5
    )
    assert "Priorize humor" in models.kwargs["contents"]
    assert "palavra0" not in models.kwargs["contents"]
    assert result.usage.total_tokens == 1150
    assert len(result.candidates) == 5
    assert result.usage.estimated_cost_usd == estimate_cost_usd(
        "gemini-3.8-flash", 1000, 150
    )


def test_prompt_hash_changes_only_when_effective_prompt_changes():
    provider = GeminiAnalysisProvider(client=SimpleNamespace(models=FakeModels()))
    assert provider.prompt_hash("humor") == provider.prompt_hash("humor")
    assert provider.prompt_hash("humor") != provider.prompt_hash("rage")
