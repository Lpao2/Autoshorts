"""Human-reviewed catalog brand decisions, applied without provider calls."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from autoshorts.brand_planning import PlannedBrandStatus, PlannedChannelBrand
from autoshorts.state_store import StateStore, default_state_db_path

_RENAMES = {
    ("animais", 2): (
        "InstintoQ",
        "Radar Animal",
        ("@radaranimal", "@radaranimalbr", "@canalradaranimal"),
    ),
    ("animais", 3): (
        "Vínculos",
        "Entre Patas",
        ("@entrepatas", "@entrepatasbr", "@canalentrepatas"),
    ),
    ("anime", 2): (
        "CodexAnime",
        "Atlas Otaku",
        ("@atlasotaku", "@atlasotakubr", "@canalatlasotaku"),
    ),
    ("atual", 2): (
        "Contraponto",
        "Pulso do Debate",
        ("@pulsododebate", "@debateempulso", "@canalpulsodebate"),
    ),
    ("filmes", 2): (
        "Veredicto",
        "Tela Crítica",
        ("@telacritica", "@telacriticabr", "@canaltelacritica"),
    ),
    ("filmes", 3): (
        "Trama Oculta",
        "Por Trás da Trama",
        ("@portrasdatrama", "@trasdatrama", "@canaltrasdatrama"),
    ),
    ("finance-business", 3): (
        "Salto Profissional",
        "Rota de Renda",
        ("@rotaderenda", "@rotarendabr", "@canalrotaderenda"),
    ),
    ("influ", 1): (
        "GargalhaClip",
        "Fora do Script",
        ("@foradoscript", "@foradoscriptbr", "@canalforadoscript"),
    ),
    ("combat-sports", 3): (
        "Veredito Final",
        "Pós-Gongo",
        ("@posgongo", "@posgongobr", "@canalposgongo"),
    ),
    ("mysteries-true-crime", 2): (
        "Fase Factual",
        "Linha dos Fatos",
        ("@linhadosfatos", "@fatosnalinha", "@canallinhadosfatos"),
    ),
    ("relationships-behavior", 1): (
        "Sintonia",
        "Papo a Dois",
        ("@papoadois", "@papoadoisbr", "@canalpapoadois"),
    ),
    ("relationships-behavior", 2): (
        "Decisão",
        "Dilema Real",
        ("@dilemareal", "@dilemarealbr", "@canaldilemareal"),
    ),
    ("relationships-behavior", 3): (
        "Dinâmica",
        "Código Social",
        ("@codigosocial", "@codigosocialbr", "@canalcodigosocial"),
    ),
}

_HANDLE_CORRECTIONS = {
    ("cars-motorcycles", 2): (
        "Engenharia Pura",
        ("@engenhariapura", "@mecanicasemsegredo", "@tecnologiasobrerodas"),
    ),
    ("comida", 1): (
        "Pronto Pra Mesa",
        ("@prontopramesa", "@prontpramesa", "@canalprontopramesa"),
    ),
    ("futebol", 1): (
        "Tática Pura",
        ("@taticapura", "@canal_tatica", "@taticafutebol"),
    ),
}


def reviewed_brand_plans(
    current: tuple[PlannedChannelBrand, ...],
) -> tuple[PlannedChannelBrand, ...]:
    """Return the frozen human decisions, rejecting an unexpected input catalog."""
    if len(current) != 60:
        raise ValueError(f"Expected 60 brand plans, found {len(current)}.")
    found_renames: set[tuple[str, int]] = set()
    found_handles: set[tuple[str, int]] = set()
    reviewed = []
    for plan in current:
        key = (plan.theme_key, plan.slot)
        changes = {"status": PlannedBrandStatus.APPROVED}
        if key in _RENAMES:
            old_name, new_name, handles = _RENAMES[key]
            if plan.public_name != old_name:
                raise ValueError(
                    f"Expected {old_name!r} at {key}, found {plan.public_name!r}."
                )
            changes.update(
                public_name=new_name,
                preferred_handle=handles[0],
                backup_handles=handles[1:],
                description=plan.description.replace(old_name, new_name),
                tagline=plan.tagline.replace(old_name, new_name),
            )
            found_renames.add(key)
        elif key in _HANDLE_CORRECTIONS:
            expected_name, handles = _HANDLE_CORRECTIONS[key]
            if plan.public_name != expected_name:
                raise ValueError(
                    f"Expected {expected_name!r} at {key}, found {plan.public_name!r}."
                )
            changes.update(preferred_handle=handles[0], backup_handles=handles[1:])
            found_handles.add(key)
        reviewed.append(replace(plan, **changes))
    if found_renames != set(_RENAMES) or found_handles != set(_HANDLE_CORRECTIONS):
        raise ValueError("The catalog does not contain every requested review target.")
    return tuple(reviewed)


def freeze_human_brand_review(database: str | Path | None = None) -> None:
    """Apply the approved review as one local SQLite transaction."""
    path = Path(database) if database is not None else default_state_db_path()
    with StateStore(path) as store:
        plans = reviewed_brand_plans(store.planned_channel_brands())
        store.approve_planned_channel_brands(
            plans,
            provenance="HUMAN_REVIEW",
            review_note="Frozen 60-plan catalog review before brand asset generation.",
        )


if __name__ == "__main__":
    freeze_human_brand_review()
