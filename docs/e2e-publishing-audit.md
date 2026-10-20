# Auditoria E2E de publicação

## Estado encontrado antes da implementação

- OAuth desktop oficial, scopes `youtube.upload` e `youtube.readonly`, refresh e
  confirmação do canal já existiam.
- Tokens já ficavam fora do repositório em arquivos restritos; SQLite guardava
  apenas a referência e metadados do binding.
- `PublishingChannel`, `GoogleIdentity`, `ChannelCredentialBinding`, fila local
  e metadata editorial dos Shorts já existiam.
- `ChannelBrandKit.source_plan_id` já modelava a associação operacional com o
  Brand Plan, mas nenhum kit operacional estava materializado no banco real.
- O pipeline terminava em `QUEUED`. `mock_uploader.py` estava vazio e não havia
  pacote revisável, aprovação pré-upload, tentativa, remote video ID ou uploader.

## Lacunas que bloqueavam o E2E

1. Ausência de uploader YouTube oficial.
2. Ausência de estados explícitos de revisão/aprovação de publicação.
3. Ausência de idempotência por vídeo/destino e histórico de tentativas.
4. Ausência de dry-run remoto que confirmasse credencial e canal.
5. Ausência de GUI/CLI para o smoke test.
6. Binding OAuth inválido/revogado não era persistido como tal.
7. YouTube Channel ID não possuía unicidade persistente entre bindings.

## Estado após a implementação

- Schema v18 adiciona `publications`, `publication_attempts` e unicidade de
  YouTube Channel ID.
- O vínculo Brand Plan → canal reutiliza `ChannelBrandKit.source_plan_id` e
  exige uma ação explícita.
- O preflight confere arquivo e checksum, metadata, plano, assets, identity,
  binding, canal remoto e OAuth renovável.
- O uploader usa `videos.insert`, `MediaFileUpload` resumable e persiste o ID
  remoto somente após resposta consistente.
- Falhas distinguem retryable, permanent e remote outcome uncertain; nenhuma
  delas marca o vídeo como publicado.
- GUI e CLI chamam o mesmo `YouTubePublicationService`.

## Estado operacional auditado

- 20 temas e 60 Brand Plans aprovados.
- 120 assets correntes, sem órfãos ou referências cruzadas inválidas.
- LoL: seis assets correntes aprovados.
- Demais temas: 114 assets correntes ainda `VALIDATED`, sem promoção silenciosa.
- Planejamento Google: 15/15/15/15.
- Configuração operacional atual: uma Google Identity e três PublishingChannels
  LoL; somente LoL A possui binding `CONNECTED`.
- Nenhum upload real foi executado por esta implementação/auditoria.
