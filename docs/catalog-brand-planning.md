# Planejamento de marca do catálogo

`PlannedChannelBrand` pertence a um `ThemeTemplate` e slot, não a um canal
operacional. Seus IDs são estáveis e seus handles permanecem `UNKNOWN`. O estado
`PROPOSED_FOR_REVIEW` significa que o vencedor automático ainda requer decisão
humana; não equivale a um `ChannelBrandKit` aprovado. Após a revisão completa,
`APPROVED` libera o plano para a futura geração de assets sem materializar um
canal operacional. O schema v14 arquiva a versão anterior em
`planned_channel_brand_history`, com provenance e nota da revisão.

Cada tema possui uma geração coordenada de 15 candidatos e um reranking conjunto
de três vencedores. Geração, candidatos, usage e reranking ficam persistidos por
tema, permitindo retomada sem repetir chamadas concluídas. LoL reutiliza a geração
real existente.

A distribuição futura usa `google-plan-1` a `google-plan-4` em rotação. Esses IDs
são planejamento e não criam `GoogleIdentity`, credenciais ou bindings reais.

`materialize_brand_plan` cria futuramente um `ChannelBrandKit` DRAFT para um
`PublishingChannel` real de mesmo slot e preserva `source_plan_id`. Assets usarão
`data/branding/<theme_key>/<brand_plan_id>/`; seus metadados ficam no SQLite e os
arquivos nunca são BLOBs.

A geração, validação, versionamento, recuperação e revisão desses arquivos são
descritos em `docs/catalog-brand-assets.md`.

Antes de escala, o pipeline deverá usar `ChannelArchetype.editorial_prompt` na
seleção do short destinado a cada canal. Assim, por exemplo, um canal de humor não
receberá sistematicamente cortes puramente analíticos. Essa integração ainda não
foi implementada.
