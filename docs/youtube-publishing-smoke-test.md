# Primeiro upload privado controlado

O fluxo de publicação é deliberadamente separado da renderização:

```text
PlannedChannelBrand APPROVED
  → ChannelBrandKit APPROVED (vínculo explícito por source_plan_id)
  → PublishingChannel
  → GoogleIdentity
  → ChannelCredentialBinding CONNECTED
  → Publication READY_FOR_REVIEW
  → APPROVED_FOR_PUBLISH
  → dry-run/preflight
  → upload resumable oficial
  → PUBLISHED + remote_video_id
```

Renderizar ou enfileirar um Short nunca o publica automaticamente.

## Preparação única

1. No Google Cloud, crie ou selecione um projeto, habilite a **YouTube Data API
   v3**, configure a tela de consentimento e adicione a conta como usuário de
   teste quando o app estiver em modo de teste.
2. Crie um OAuth Client do tipo **Desktop app** e guarde o JSON fora do
   repositório.
3. Instale o suporte oficial:

   ```bash
   .venv/bin/pip install -e '.[youtube-oauth]'
   ```

4. Antes de iniciar a GUI ou CLI, configure somente o caminho do client secret:

   ```bash
   export AUTOSHORTS_GOOGLE_CLIENT_SECRET=/caminho/seguro/client_secret.json
   ```

Tokens autorizados permanecem em
`~/.config/autoshorts/youtube-credentials/`, com diretório `0700` e arquivos
`0600`. Senhas Google não são solicitadas nem persistidas.

## Fluxo pela GUI

1. Em **Automático**, cadastre ou confira a Google Identity e associe-a a um
   PublishingChannel.
2. Use **Conectar YouTube**, conclua OAuth no navegador e confirme visualmente o
   título e o YouTube Channel ID retornados.
3. Em **Publicação YouTube**, selecione o canal e use **Vincular Brand Plan**.
   A ação só funciona quando o plano e os assets correntes estão aprovados.
4. Selecione um vídeo final, preencha título, descrição e tags, mantenha
   `private` e escolha **Criar para revisão**.
5. Confira a linha persistida e clique **Aprovar selecionada**.
6. Execute **Dry-run**. Ele valida arquivo/checksum, metadata, Brand Plan,
   identidade, binding, canal remoto e refresh da credencial, sem upload.
7. Somente depois, escolha **Upload real...** e confirme a ação externa.
8. Confira `PUBLISHED` e o `remote_video_id` no histórico.

## Fluxo equivalente pela CLI

Todos os comandos usam as mesmas camadas da GUI:

```bash
autoshorts-publish audit
autoshorts-publish list-identities
autoshorts-publish list-channels
autoshorts-publish link-brand --channel <publishing_channel_id>
autoshorts-publish create \
  --channel <publishing_channel_id> \
  --video /caminho/short-final.mp4 \
  --title "Título revisado" \
  --description "Descrição revisada" \
  --tag shorts --privacy private \
  --no-made-for-kids --no-contains-synthetic-media \
  --no-paid-product-placement --default-language pt-BR \
  --license youtube --embeddable --public-stats
autoshorts-publish approve <publication_id>
autoshorts-publish dry-run <publication_id>
autoshorts-publish upload <publication_id> --confirm-upload
```

Um upload `public` exige também `--allow-public`. Testes automatizados nunca
executam o backend real.

## Idempotência e falhas

A chave lógica combina PublishingChannel e SHA-256 do vídeo. O mesmo arquivo
não cria duas publicações concorrentes para o mesmo destino. O início do upload
é adquirido atomicamente no SQLite e cada tentativa recebe histórico próprio.

Se a API confirmar o envio, o ID remoto é persistido e uma segunda execução
reutiliza o registro `PUBLISHED`. Se a conexão cair depois do início do upload e
o resultado remoto não puder ser provado, o estado vira `UPLOAD_UNCERTAIN` e o
sistema bloqueia retry automático. Primeiro confira o YouTube Studio; isso evita
duplicar um vídeo que talvez já tenha sido aceito.

Erros não carregam access token, refresh token ou payload de credencial.

## Metadata oficial e limites do Studio

Cada nova Publication persiste e revisa audiência infantil, declaração de
conteúdo sintético/alterado, promoção paga, categoria por tema, idioma, licença,
incorporação, estatísticas públicas e os campos opcionais de agendamento/data de
gravação. Audiência, conteúdo sintético e promoção paga nunca podem ficar
indefinidos na aprovação. O preset do tema LoL usa `20 / Gaming`; outros temas
devem configurar seu próprio `youtube_category_id` e `youtube_category_name`.

| Configuração do Studio | Suportada pela API? | Campo API | AutoShorts cobre? |
|---|---|---|---|
| Criado para crianças | Sim | `status.selfDeclaredMadeForKids` | Sim, explícito e obrigatório |
| Conteúdo alterado/sintético | Sim | `status.containsSyntheticMedia` | Sim, explícito e obrigatório |
| Promoção paga | Sim no upload | `paidProductPlacementDetails.hasPaidProductPlacement` | Sim, explícito e obrigatório |
| Categoria | Sim | `snippet.categoryId` | Sim, preset por tema e editável |
| Idioma da metadata | Sim | `snippet.defaultLanguage` | Sim |
| Licença | Sim | `status.license` | Sim |
| Incorporação | Sim | `status.embeddable` | Sim |
| Estatísticas públicas | Sim | `status.publicStatsViewable` | Sim |
| Agendamento | Sim, somente vídeo privado nunca publicado | `status.publishAt` | Sim, opcional |
| Data de gravação | Sim | `recordingDetails.recordingDate` | Sim, opcional |
| Restrição de idade | Não gravável por `videos.insert/update` | `contentDetails.contentRating.ytRating` é informativo | Studio |
| Comentários | Apenas defaults do canal/Studio | Sem campo gravável em `video` | Não |
| Exibir contagem de likes/ratings | Não exposta para escrita | Sem campo gravável equivalente | Não |
| Remix/amostragem de Shorts | Não exposta para escrita | Sem campo na Data API v3 | Studio |
| Capítulos automáticos | Apenas default do canal/Studio | Sem campo na Data API v3 | Não |
| Lugares automáticos | Não exposta para escrita | Sem campo na Data API v3 | Studio |

Fontes oficiais: recurso [`videos`](https://developers.google.com/youtube/data/reference/rest/v3/videos),
[`videos.insert`](https://developers.google.com/youtube/data/reference/rest/v3/videos/insert),
[`videos.update`](https://developers.google.com/youtube/data/reference/rest/v3/videos/update)
e [`videoCategories.list`](https://developers.google.com/youtube/data/reference/rest/v3/videoCategories/list).

## Escopo atual

- Upload: vídeo, metadata editorial, audiência, declarações, categoria, idioma,
  licença, incorporação, estatísticas e datas suportadas oficialmente.
- Transporte: `videos.insert` resumable pela biblioteca oficial Google.
- Thumbnail customizada: ainda não suportada.
- Scheduler: fora deste smoke test; nenhum agendamento automático foi ativado.
