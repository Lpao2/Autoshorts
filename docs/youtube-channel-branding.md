# Provisionamento de branding do canal YouTube

O AutoShorts deriva o plano remoto de `ChannelBrandKit` aprovado, assets
aprovados e `PublishingChannel`. Sempre execute o dry-run antes da aplicação:

```bash
.venv/bin/autoshorts-publish branding-dry-run --channel CHANNEL_ID
.venv/bin/autoshorts-publish branding-apply --channel CHANNEL_ID --confirm-branding
```

O segundo comando faz `channels.list`, valida ID e título do binding, envia o
banner com `channelBanners.insert`, constrói um payload conservador preservando
todos os campos graváveis existentes, aplica `channels.update` e consulta o
canal novamente. Ele não envia vídeos.

## Matriz oficial de capacidades

| Propriedade do Studio | YouTube Data API v3 | Campo | AutoShorts |
|---|---|---|---|
| Nome do canal | somente leitura para este fluxo | `snippet.title`; alteração via `brandingSettings.channel.title` é proibida | manual |
| Handle | consulta por handle, sem campo de escrita | `snippet.customUrl` / `forHandle` | manual |
| Descrição | gravável | `brandingSettings.channel.description` | automático |
| Palavras-chave | gravável | `brandingSettings.channel.keywords` | automático |
| Idioma padrão | gravável | `brandingSettings.channel.defaultLanguage` | automático (`pt-BR`) |
| País | gravável | `brandingSettings.channel.country` | somente quando configurado explicitamente |
| Banner | gravável em duas etapas | `channelBanners.insert` + `brandingSettings.image.bannerExternalUrl` | automático |
| Foto de perfil | não exposta para escrita | — | manual |
| Público infantil do canal | gravável | `status.selfDeclaredMadeForKids` | não aplicado sem política explícita |
| Trailer para não inscritos | gravável | `brandingSettings.channel.unsubscribedTrailer` | preservado, não gerenciado |
| Conta de Analytics | gravável | `brandingSettings.channel.trackingAnalyticsAccountId` | preservada, não gerenciada |
| Localizações | graváveis | `localizations.*` | fora do escopo atual |

Referências oficiais: [channels.update](https://developers.google.com/youtube/data/reference/rest/v3/channels/update),
[recurso channels](https://developers.google.com/youtube/data/reference/rest/v3/channels)
e [channelBanners.insert](https://developers.google.com/youtube/data/reference/rest/v3/channelBanners/insert).

## OAuth

O onboarding solicita `https://www.googleapis.com/auth/youtube`. Esse escopo é
aceito tanto por `channels.update` quanto por `channelBanners.insert` e pelos
uploads de vídeo. Bindings antigos que só possuam `youtube.upload` e
`youtube.readonly` aparecem como `NEEDS_REAUTH`; a aplicação é bloqueada antes
de qualquer chamada remota de escrita.

O plano conserva `pt-BR` como intenção editorial. O YouTube pode normalizar
esse valor para `pt` na leitura posterior; a validação aceita essa normalização
de região, mas continua rejeitando outro idioma.

## Smoke test seguro

1. Confirme `schema`, binding, Brand Plan e assets com `audit`, `list-channels`
   e o dry-run de branding.
2. Confira caminhos, dimensões, checksums, Channel ID e `OAuth scopes: OK`.
3. Só então execute `branding-apply` com `--confirm-branding`.
4. Confirme `BANNER_REMOTE=PASS` e `REMOTE_VALIDATION=PASS`.
5. Aplique manualmente o logo aprovado e o handle no YouTube Studio.

Se o processo falhar depois de `channelBanners.insert`, não repita às cegas:
consulte o canal e o Studio antes de uma nova aplicação.
