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
  --tag shorts --privacy private
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

## Escopo atual

- Upload: vídeo, título, descrição, tags e privacy status.
- Transporte: `videos.insert` resumable pela biblioteca oficial Google.
- Thumbnail customizada: ainda não suportada.
- Scheduler: fora deste smoke test; nenhum agendamento automático foi ativado.
