# Identidade de marca dos canais

O fluxo mantém `PublishingChannel.channel_id` como chave interna imutável. Nome
público, handle e identidade editorial são dados versionados e podem mudar sem
alterar OAuth, bindings ou roteamento de publicação.

## Fluxo

1. `GeminiBrandIdentityProvider` recebe um `ThemeTemplate`, os três
   `ChannelArchetype` e os três `PublishingChannel`.
2. Uma chamada coordenada solicita cinco propostas por canal.
3. Os 15 `BrandCandidate` são persistidos com handles em estado `UNKNOWN`.
4. A GUI permite selecionar, revisar os campos e aprovar explicitamente.
5. Uma aprovação cria nova versão de `ChannelBrandKit`; a versão anteriormente
   aprovada passa para `RETIRED`. Nunca há duas versões `APPROVED` por canal.

Não há verificação de disponibilidade, criação de canal ou geração de imagem.
`BrandImageProvider` é apenas uma interface reservada. Assets futuros ficam em
`data/branding/<theme_key>/<publishing_channel_id>/`, sem BLOBs no SQLite.

## Persistência v11

- `brand_generations`: provider, modelo, operação `brand_generation`, tokens,
  duração e custo teórico.
- `brand_candidates`: propostas e três handles não verificados.
- `channel_brand_kits`: histórico versionado de identidades aprovadas/retiradas.

O histórico de usage não é misturado com análise ou reranking de vídeos.

## Primeiro teste real

A primeira chamada coordenada para LoL A/B/C recebeu `503 UNAVAILABLE` por alta
demanda temporária do modelo. Não houve segunda chamada, resposta parcial,
persistência de candidatos ou aprovação. Uma nova tentativa deve ser iniciada
explicitamente pela ação **Gerar identidade** na GUI.
