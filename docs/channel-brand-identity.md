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

## Persistência v12

- `brand_generations`: provider, modelo, operação `brand_generation`, tokens,
  duração, custo teórico, status (`SUCCEEDED`, `VALIDATION_FAILED` ou
  `API_FAILED`) e erro sanitizado quando aplicável.
- `brand_candidates`: propostas e três handles não verificados.
- `channel_brand_kits`: histórico versionado de identidades aprovadas/retiradas.

O histórico de usage não é misturado com análise ou reranking de vídeos.

Handles retornados pelo provider são normalizados localmente antes da validação:
prefixo `@`, caixa, espaços, URLs do YouTube e acentos seguros são corrigidos sem
nova chamada. Isso não verifica disponibilidade; todos continuam `UNKNOWN`. Uma
falha irrecuperável preserva o usage da resposta, mas não publica candidatos.

## Modelos por operação

- análise: `AUTOSHORTS_GEMINI_ANALYSIS_MODEL` (padrão `gemini-3.8-flash`);
- reranking: `AUTOSHORTS_GEMINI_RERANK_MODEL` (padrão `gemini-3.8-flash`);
- branding: `AUTOSHORTS_GEMINI_BRAND_MODEL` (padrão
  `gemini-3.5-flash-lite`, com thinking mínimo).

O modelo gravado e o custo teórico são sempre os da chamada efetivamente feita.
Branding mantém três tentativas apenas para erros transitórios, sem fallback
silencioso para outro modelo.

## Primeiro teste real

As primeiras operações coordenadas para LoL A/B/C receberam `503 UNAVAILABLE`
por alta demanda temporária. Nenhuma resposta parcial, candidato ou aprovação
foi persistida. Uma nova operação deve ser iniciada explicitamente pela ação
**Gerar identidade** na GUI.
