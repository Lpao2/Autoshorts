from __future__ import annotations

from dataclasses import replace

import pytest

from autoshorts.asset_visual_direction import (
    ColorMood,
    ColorTemperature,
    ShapeLanguage,
    VisualContrast,
    VisualDensity,
    VisualEnergy,
    resolve_asset_visual_direction,
)
from autoshorts.brand_identity import HandleAvailability
from autoshorts.brand_planning import PlannedBrandStatus, PlannedChannelBrand
from autoshorts.state_store import StateStore


def _direction_for_profile(tmp_path, profile: str, cues: str):
    with StateStore(tmp_path / f"{profile}.db") as store:
        original = store.list_theme_templates()[0]
    source = replace(
        original.source_strategy, description="Fontes editoriais relevantes"
    )
    theme = replace(
        original,
        description="Conteúdo de marca com linguagem própria",
        tags=(),
        source_strategy=source,
    )
    archetype = replace(
        theme.channel_archetypes[0],
        tone=profile,
        editorial_prompt=f"Abordagem {profile}",
        visual_direction=cues,
        headline_style=profile,
        audience="público geral",
        editorial_focus=(profile,),
        content_preferences=(profile,),
    )
    brand = PlannedChannelBrand(
        brand_plan_id=f"profile-{profile}",
        theme_key=theme.theme_key,
        slot=archetype.slot,
        public_name="Marca Perfil",
        preferred_handle="@marcaperfil",
        backup_handles=("@perfilmarca", "@canalperfil"),
        description=f"Uma marca de perfil {profile}. {cues}",
        tagline="Clareza com personalidade",
        brand_keywords=(profile, cues),
        visual_direction=cues,
        palette=("#667788", "#D0D3D6", "#8899AA", "#F4F4F2"),
        logo_brief=f"Símbolo simples, {profile}. {cues}",
        banner_brief=f"Um motivo dominante, {profile}. {cues}",
        status=PlannedBrandStatus.APPROVED,
        handle_availability=HandleAvailability.UNKNOWN,
        provider="human",
        model="manual",
    )
    return resolve_asset_visual_direction(theme, archetype, brand)


@pytest.mark.parametrize(
    ("profile", "cues", "mood", "contrast", "shape", "energy"),
    (
        (
            "acolhedor",
            "orgânico, suave e empático",
            ColorMood.SOFT,
            VisualContrast.LOW,
            ShapeLanguage.ORGANIC,
            VisualEnergy.CALM,
        ),
        (
            "analítico",
            "sóbrio, preciso e estruturado",
            ColorMood.BALANCED,
            VisualContrast.MEDIUM,
            ShapeLanguage.EDITORIAL,
            VisualEnergy.MODERATE,
        ),
        (
            "didático",
            "claro, estruturado e explicativo",
            ColorMood.BALANCED,
            VisualContrast.MEDIUM,
            ShapeLanguage.EDITORIAL,
            VisualEnergy.MODERATE,
        ),
        (
            "crítico",
            "editorial, ponderado e objetivo",
            ColorMood.BALANCED,
            VisualContrast.MEDIUM,
            ShapeLanguage.EDITORIAL,
            VisualEnergy.MODERATE,
        ),
        (
            "irreverente",
            "angular, intenso e vibrante",
            ColorMood.BOLD,
            VisualContrast.HIGH,
            ShapeLanguage.ANGULAR,
            VisualEnergy.ENERGETIC,
        ),
        (
            "competitivo",
            "angular, intenso e de alto impacto",
            ColorMood.BOLD,
            VisualContrast.HIGH,
            ShapeLanguage.ANGULAR,
            VisualEnergy.ENERGETIC,
        ),
        (
            "investigativo",
            "sóbrio, factual e editorial",
            ColorMood.BALANCED,
            VisualContrast.MEDIUM,
            ShapeLanguage.EDITORIAL,
            VisualEnergy.MODERATE,
        ),
        (
            "técnico",
            "geométrico, preciso e objetivo",
            ColorMood.BALANCED,
            VisualContrast.MEDIUM,
            ShapeLanguage.GEOMETRIC,
            VisualEnergy.MODERATE,
        ),
    ),
)
def test_resolves_expected_profile_personality(
    tmp_path, profile, cues, mood, contrast, shape, energy
):
    direction = _direction_for_profile(tmp_path, profile, cues)
    assert direction.color_mood == mood
    assert direction.contrast == contrast
    assert direction.shape_language == shape
    assert direction.energy == energy
    assert direction.visual_density in VisualDensity
    assert direction.temperature in ColorTemperature


def test_energetic_direction_can_stay_sparse(tmp_path):
    direction = _direction_for_profile(
        tmp_path,
        "competitivo",
        "angular, intenso, minimalista, limpo, um símbolo",
    )
    assert direction.energy == VisualEnergy.ENERGETIC
    assert direction.visual_density == VisualDensity.SPARSE
    assert "without adding more elements" in direction.prompt_block()
