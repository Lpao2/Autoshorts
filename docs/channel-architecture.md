# Arquitetura de canais do AutoShorts

O modelo separa produção, destino e administração de autenticação. Nenhum dos
objetos abaixo contém senha, cookie, token OAuth ou client secret.

## Domínio

`ThemeProfile` define **o que produzir**: possui `theme_id` estável, nome,
criadores, prompt, pasta de saída e política de produção. O nome pode mudar sem
alterar a identidade interna.

`PublishingChannel` define **para onde enviar** um Short. `channel_id` é a
identidade interna estável; `youtube_channel_id` será a identidade confirmada da
plataforma quando OAuth for implementado. O `slot` é persistente e mantém o
mapeamento Short 1→slot 1, Short 2→slot 2 e Short 3→slot 3.

`GoogleIdentity` é cadastro administrativo de uma identidade Google. Uma mesma
identity pode ser referenciada por qualquer quantidade de canais. A relação é
armazenada somente em `PublishingChannel.google_identity_id`, evitando duas
fontes de verdade.

`EditorialProfile` adiciona prompt, estilo de headline e tom por canal. A função
`resolve_editorial_context()` compõe, na ordem, prompt base do AutoShorts,
prompt do tema e orientação do canal. Ela não chama IA.

`VisualProfile` guarda paleta, crop, caption e branding presets. Esses valores
ainda não alteram o FFmpeg.

`PostingPolicy` pertence ao canal e começa inativa. Limite diário, timezone e
janelas são apenas configuração; não existe scheduler nesta etapa.

`ResolvedPublishingProfile` entrega tema, canal, contexto editorial resolvido,
visual e posting policy num único objeto para os módulos futuros.

## IDs e compatibilidade

Novos temas e canais recebem UUID uma vez no CRUD. Configurações antigas sem IDs
ganham UUIDv5 determinístico, portanto carregamentos repetidos produzem os mesmos
IDs. O leitor ainda aceita `accounts`; cada entrada é migrada em memória para um
canal e uma Google identity administrativa. Ao salvar, somente o formato novo
`google_identities` + `themes[].channels` é escrito.

`AccountProfile` permanece apenas como adaptador depreciado para código legado.
Pipeline e distribuição usam `PublishingChannel` e persistem `channel_id`.

## SQLite

O schema v7 adiciona `shorts.publishing_channel_id`. Na migração v6→v7, linhas
antigas copiam `account_identifier` para o novo campo para preservar a referência
existente. Produções novas gravam o `channel_id`; `account_identifier` fica como
coluna legada compatível.

## Escala

As quantidades não são globais nem hardcoded. A regra atual limita somente cada
tema a três canais habilitados, por causa dos três Shorts produzidos por vídeo.
Assim, 20 temas × 3 canais podem referenciar, por exemplo, quatro identities com
15 canais cada, sem mudança no domínio ou no schema. Adicionar temas, identities
e canais é apenas configuração.

## Limites desta etapa

Não foram implementados OAuth, credenciais, upload, publicação, scheduler,
daemon, novos renders, crop, legendas visuais ou chamadas adicionais de IA.
