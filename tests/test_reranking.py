from types import SimpleNamespace

import pytest
from test_analysis_service import Provider, prepared_store
from test_transcript_analysis import candidate_pool, transcript

from autoshorts.analysis_service import analyze_transcript
from autoshorts.gemini_reranker import GeminiCandidateReranker
from autoshorts.reranking import (
    RankedSelection,
    RerankResult,
    build_rerank_candidates,
    validate_rerank,
)
from autoshorts.reranking_service import rerank_analysis
from autoshorts.transcript_analysis import AnalysisResult, AnalysisUsage, load_analysis


def ranking():
    return (
        RankedSelection("candidate-4", 1, "melhor hook", 0.94),
        RankedSelection("candidate-2", 2, "mais claro", 0.91),
        RankedSelection("candidate-3", 3, "bom payoff", 0.88),
    )


class Reranker:
    provider_name = "gemini"
    model = "gemini-3.8-flash"

    def __init__(self):
        self.calls = 0

    def prompt_hash(self, theme_prompt):
        return f"rerank:{theme_prompt}"

    def rerank(self, *_args, **_kwargs):
        self.calls += 1
        return RerankResult(
            ranking(),
            self.provider_name,
            self.model,
            "rerank:humor",
            AnalysisUsage(300, 80, 0, 380, 0.5, 0.000525),
        )


class CandidateProvider(Provider):
    def analyze(self, *_args, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        return AnalysisResult(
            (),
            self.provider_name,
            self.model,
            AnalysisUsage(1000, 100, 0, 1100, 1, 0.001125),
            candidate_pool(),
        )


def generated_analysis(tmp_path):
    store, _ = prepared_store(tmp_path)
    path = tmp_path / "lol" / ".work" / "video" / "analysis.json"
    analyze_transcript(
        youtube_id="video",
        transcript=transcript(),
        artifact_path=path,
        provider=CandidateProvider(),
        state_store=store,
        title="title",
        creator="creator",
        theme="lol",
        theme_prompt="humor",
    )
    return store, path


@pytest.mark.parametrize(
    "bad",
    [
        ranking()[:2],
        (ranking()[0], ranking()[0], ranking()[2]),
        (RankedSelection("candidate-99", 1, "x", 0.9), *ranking()[1:]),
        (RankedSelection("candidate-4", 3, "x", 0.9), *ranking()[1:]),
    ],
)
def test_rerank_contract_rejects_invalid_ids_duplicates_and_ranks(tmp_path, bad):
    store, path = generated_analysis(tmp_path)
    analysis = load_analysis(path, transcript(), youtube_id="video")
    candidates = build_rerank_candidates(analysis, transcript())
    with pytest.raises(ValueError):
        validate_rerank(RerankResult(tuple(bad), "p", "m", "h"), candidates)
    store.close()


def test_rerank_is_persisted_reused_and_preserves_candidate_ranges(tmp_path):
    store, path = generated_analysis(tmp_path)
    reranker = Reranker()
    before = load_analysis(path, transcript(), youtube_id="video")
    original = {(x.start_segment, x.end_segment) for x in before.result.candidates}
    rerank_analysis(
        youtube_id="video",
        transcript=transcript(),
        analysis_path=path,
        reranker=reranker,
        state_store=store,
        title="title",
        creator="creator",
        theme="lol",
        theme_prompt="humor",
    )
    rerank_analysis(
        youtube_id="video",
        transcript=transcript(),
        analysis_path=path,
        reranker=reranker,
        state_store=store,
        title="title",
        creator="creator",
        theme="lol",
        theme_prompt="humor",
    )
    after = load_analysis(path, transcript(), youtube_id="video")
    assert reranker.calls == 1
    assert [(x.start_segment, x.end_segment) for x in after.result.selections] == [
        (9, 11),
        (6, 8),
        (3, 5),
    ]
    assert {
        (x.start_segment, x.end_segment) for x in after.result.candidates
    } == original
    operations = [
        x[0]
        for x in store.connection.execute("SELECT operation FROM ai_usage ORDER BY id")
    ]
    assert operations == ["generation", "rerank"]
    store.close()


class Models:
    def generate_content(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            parsed={
                "ranking": [
                    {
                        "candidate_id": x.candidate_id,
                        "rank": x.rank,
                        "reason": x.reason,
                        "editorial_score": x.editorial_score,
                    }
                    for x in ranking()
                ]
            },
            usage_metadata=SimpleNamespace(
                prompt_token_count=300,
                candidates_token_count=80,
                thoughts_token_count=0,
                total_token_count=380,
            ),
        )


def test_gemini_rerank_prompt_uses_theme_and_omits_original_scores(tmp_path):
    store, path = generated_analysis(tmp_path)
    analysis = load_analysis(path, transcript(), youtube_id="video")
    candidates = build_rerank_candidates(analysis, transcript())
    models = Models()
    provider = GeminiCandidateReranker(client=SimpleNamespace(models=models))
    provider.rerank(
        candidates,
        theme="lol",
        theme_prompt="priorize humor",
        title="title",
        creator="creator",
        youtube_id="video",
    )
    prompt = models.kwargs["contents"]
    assert "priorize humor" in prompt
    assert "SCORE" not in prompt.upper()
    assert str(candidates[0].selection.score) not in prompt
    store.close()
