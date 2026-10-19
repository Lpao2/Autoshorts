"""Pure adaptive visual direction resolution for generated brand assets."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from enum import StrEnum

from autoshorts.brand_planning import PlannedChannelBrand
from autoshorts.theme_catalog import ChannelArchetype, ThemeTemplate


class ColorMood(StrEnum):
    SOFT = "SOFT"
    BALANCED = "BALANCED"
    BOLD = "BOLD"


class VisualContrast(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class VisualDensity(StrEnum):
    SPARSE = "SPARSE"
    BALANCED = "BALANCED"


class ShapeLanguage(StrEnum):
    ORGANIC = "ORGANIC"
    GEOMETRIC = "GEOMETRIC"
    ANGULAR = "ANGULAR"
    EDITORIAL = "EDITORIAL"


class VisualEnergy(StrEnum):
    CALM = "CALM"
    MODERATE = "MODERATE"
    ENERGETIC = "ENERGETIC"


class ColorTemperature(StrEnum):
    COOL = "COOL"
    NEUTRAL = "NEUTRAL"
    WARM = "WARM"


@dataclass(frozen=True, slots=True)
class ResolvedAssetVisualDirection:
    color_mood: ColorMood
    contrast: VisualContrast
    visual_density: VisualDensity
    shape_language: ShapeLanguage
    energy: VisualEnergy
    temperature: ColorTemperature

    def as_dict(self) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items()}

    @classmethod
    def from_dict(cls, payload: dict[str, str]):
        return cls(
            ColorMood(payload["color_mood"]),
            VisualContrast(payload["contrast"]),
            VisualDensity(payload["visual_density"]),
            ShapeLanguage(payload["shape_language"]),
            VisualEnergy(payload["energy"]),
            ColorTemperature(payload["temperature"]),
        )

    def prompt_block(self) -> str:
        mood = {
            ColorMood.SOFT: "soft, restrained, and approachable",
            ColorMood.BALANCED: "balanced, clear, and assured",
            ColorMood.BOLD: "bold, distinctive, and assertive",
        }[self.color_mood]
        contrast = {
            VisualContrast.LOW: "low, gentle contrast",
            VisualContrast.MEDIUM: "controlled medium contrast",
            VisualContrast.HIGH: "high contrast with a disciplined palette",
        }[self.contrast]
        density = {
            VisualDensity.SPARSE: "sparse composition with generous breathing room",
            VisualDensity.BALANCED: "balanced composition with limited visual elements",
        }[self.visual_density]
        shape = {
            ShapeLanguage.ORGANIC: "organic, fluid shapes",
            ShapeLanguage.GEOMETRIC: "clean geometric shapes",
            ShapeLanguage.ANGULAR: "decisive angular shapes",
            ShapeLanguage.EDITORIAL: "structured editorial shapes",
        }[self.shape_language]
        energy = {
            VisualEnergy.CALM: "calm energy",
            VisualEnergy.MODERATE: "moderate, purposeful energy",
            VisualEnergy.ENERGETIC: "energetic impact without adding more elements",
        }[self.energy]
        temperature = {
            ColorTemperature.COOL: "cool color temperature",
            ColorTemperature.NEUTRAL: "neutral color temperature",
            ColorTemperature.WARM: "warm color temperature",
        }[self.temperature]
        return (
            "Resolved visual mood:\n"
            f"- {mood}\n- {contrast}\n- {density}\n- {shape}\n"
            f"- {energy}\n- {temperature}"
        )


def _normalized(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )


def _contains(text: str, terms: tuple[str, ...]) -> int:
    return sum(1 for term in terms if re.search(rf"\b{re.escape(term)}", text))


def _pick(scores: dict[StrEnum, float], order: tuple[StrEnum, ...]):
    return max(order, key=lambda item: (scores[item], -order.index(item)))


def _palette_temperature(palette: tuple[str, ...]) -> tuple[float, float]:
    warm = cool = 0.0
    for value in palette:
        match = re.fullmatch(r"#([0-9a-fA-F]{6})", value.strip())
        if not match:
            continue
        raw = match.group(1)
        red, green, blue = (int(raw[index : index + 2], 16) for index in (0, 2, 4))
        if red > blue + 28 and red >= green - 12:
            warm += 1
        elif blue > red + 28 or (green > red + 35 and blue >= red):
            cool += 1
    return warm, cool


def resolve_asset_visual_direction(
    theme: ThemeTemplate,
    archetype: ChannelArchetype,
    brand: PlannedChannelBrand,
) -> ResolvedAssetVisualDirection:
    """Resolve visual personality from semantic and visual inputs, not theme name."""
    semantic = _normalized(
        " ".join(
            (
                theme.description,
                " ".join(theme.tags),
                theme.source_strategy.description,
                archetype.tone,
                archetype.editorial_prompt,
                archetype.visual_direction,
                archetype.headline_style,
                archetype.audience,
                " ".join(archetype.editorial_focus),
                " ".join(archetype.content_preferences),
                brand.description,
                " ".join(brand.brand_keywords),
                brand.visual_direction,
                brand.logo_brief,
                brand.banner_brief,
            )
        )
    )
    tone = _normalized(archetype.tone)

    soft_terms = (
        "acolhedor",
        "empatico",
        "respeitoso",
        "ponderado",
        "suave",
        "leve",
        "pastel",
        "emocao",
        "conexao",
        "nostalgico",
    )
    bold_terms = (
        "irreverente",
        "intenso",
        "competitivo",
        "dramatico",
        "apaixonado",
        "entusiasmado",
        "motivador",
        "rage",
        "potencia",
        "rivalidade",
        "alto contraste",
    )
    balanced_terms = (
        "didatico",
        "analitico",
        "tecnico",
        "sobrio",
        "critico",
        "investigativo",
        "objetivo",
        "pratico",
        "curioso",
        "narrativo",
    )
    mood_scores = {
        ColorMood.SOFT: _contains(semantic, soft_terms)
        + 5 * _contains(tone, soft_terms),
        ColorMood.BALANCED: 2
        + _contains(semantic, balanced_terms)
        + 4 * _contains(tone, balanced_terms),
        ColorMood.BOLD: _contains(semantic, bold_terms)
        + 4 * _contains(tone, bold_terms),
    }
    mood = _pick(mood_scores, (ColorMood.BALANCED, ColorMood.SOFT, ColorMood.BOLD))

    gentle_terms = (*soft_terms, "difusa", "sutil", "delicado", "fluido")
    forceful_terms = (
        "intenso",
        "competitivo",
        "dramatico",
        "irreverente",
        "vibrante",
        "neon",
        "impacto",
        "potencia",
        "colisao",
    )
    gentle = _contains(semantic, gentle_terms) + 4 * _contains(tone, gentle_terms)
    forceful = _contains(semantic, forceful_terms) + 4 * _contains(tone, forceful_terms)
    if mood == ColorMood.SOFT and gentle >= forceful:
        contrast = VisualContrast.LOW
    elif mood == ColorMood.BOLD:
        contrast = VisualContrast.HIGH
    else:
        contrast = VisualContrast.MEDIUM

    sparse_terms = (
        "minimalista",
        "limpo",
        "sutil",
        "sobrio",
        "leve",
        "monograma",
        "icone",
        "uma linha",
        "um simbolo",
    )
    layered_terms = (
        "padrao",
        "textura",
        "elementos",
        "linhas",
        "conectado",
        "conectados",
        "mapa",
        "dados",
        "historias",
        "narrativa",
        "narrativo",
    )
    sparse_score = _contains(semantic, sparse_terms)
    layered_score = _contains(semantic, layered_terms)
    density = (
        VisualDensity.SPARSE
        if sparse_score + (1 if mood == ColorMood.SOFT else 0) >= layered_score
        else VisualDensity.BALANCED
    )

    shape_scores = {
        ShapeLanguage.ORGANIC: 4
        * _contains(
            tone,
            (
                "acolhedor",
                "empatico",
                "respeitoso",
                "nostalgico",
                "narrativo",
                "ludico",
            ),
        )
        + _contains(
            semantic,
            (
                "acolhedor",
                "empatico",
                "organico",
                "fluido",
                "onda",
                "folha",
                "pata",
                "animal",
                "humano",
                "coracao",
                "alimento",
                "cozinha",
                "jornada",
                "trilha",
                "receita",
                "ingrediente",
                "prato",
                "gourmet",
                "gastronomia",
            ),
        ),
        ShapeLanguage.GEOMETRIC: 4 * _contains(tone, ("tecnico", "objetivo", "pratico"))
        + _contains(
            semantic,
            (
                "geometrico",
                "geometrica",
                "tecnico",
                "tecnologia",
                "engenharia",
                "mecanica",
                "chip",
                "hardware",
                "bloco",
                "pixel",
                "estrutura",
                "diagrama",
                "estrategia",
                "laboratorio",
                "dados",
                "grafico",
                "engrenagem",
                "roda",
            ),
        ),
        ShapeLanguage.ANGULAR: 4
        * _contains(
            tone,
            (
                "irreverente",
                "intenso",
                "competitivo",
                "dramatico",
                "apaixonado",
                "entusiasmado",
                "motivador",
                "direto",
            ),
        )
        + _contains(
            semantic,
            (
                "irreverente",
                "intenso",
                "competitivo",
                "dramatico",
                "duelo",
                "colisao",
                "luta",
                "diagonal",
                "espada",
                "rage",
                "potencia",
            ),
        ),
        ShapeLanguage.EDITORIAL: 1
        + 4
        * _contains(
            tone,
            (
                "analitico",
                "didatico",
                "critico",
                "investigativo",
                "sobrio",
                "ponderado",
                "contador",
                "curioso",
            ),
        )
        + _contains(
            semantic,
            (
                "analitico",
                "didatico",
                "critico",
                "investigativo",
                "sobrio",
                "jornalistico",
                "debate",
                "fato",
                "cronologia",
                "ciencia",
                "opiniao",
            ),
        ),
    }
    shape = _pick(
        shape_scores,
        (
            ShapeLanguage.EDITORIAL,
            ShapeLanguage.ORGANIC,
            ShapeLanguage.GEOMETRIC,
            ShapeLanguage.ANGULAR,
        ),
    )

    calm_terms = (*soft_terms, "sobrio", "nostalgico", "narrativo")
    energetic_terms = (
        "irreverente",
        "intenso",
        "competitivo",
        "dramatico",
        "entusiasmado",
        "apaixonado",
        "motivador",
        "divertido",
        "ludico",
        "rage",
        "clutch",
        "potencia",
    )
    calm = _contains(semantic, calm_terms) + 4 * _contains(tone, calm_terms)
    energetic = _contains(semantic, energetic_terms) + 4 * _contains(
        tone, energetic_terms
    )
    if calm >= energetic + 2:
        energy = VisualEnergy.CALM
    elif energetic >= calm + 3:
        energy = VisualEnergy.ENERGETIC
    else:
        energy = VisualEnergy.MODERATE

    warm_palette, cool_palette = _palette_temperature(brand.palette)
    warm = warm_palette + _contains(
        semantic,
        (
            "acolhedor",
            "quente",
            "vermelho",
            "laranja",
            "amarelo",
            "dourado",
            "ambar",
            "terroso",
            "comida",
            "cozinha",
            "humor",
            "paixao",
        ),
    )
    cool = cool_palette + _contains(
        semantic,
        (
            "azul",
            "ciano",
            "turquesa",
            "tecnico",
            "tecnologia",
            "analitico",
            "investigativo",
            "precisao",
            "dados",
        ),
    )
    if warm >= cool + 2:
        temperature = ColorTemperature.WARM
    elif cool >= warm + 2:
        temperature = ColorTemperature.COOL
    else:
        temperature = ColorTemperature.NEUTRAL

    return ResolvedAssetVisualDirection(
        mood, contrast, density, shape, energy, temperature
    )
