# Provisionamento administrativo de canais YouTube

O provisionamento inicial de canais é separado do orchestrator de conteúdo.
A GUI **Provisionamento YouTube** e a CLI usam o mesmo
`YouTubeProvisioningService`; Playwright fica isolado no adapter visível.

## Sessões e credenciais

Cada `GoogleIdentity.identity_id` possui um perfil Chromium persistente em:

`data/playwright/youtube-profiles/<google-identity-id>/`

Esses perfis guardam a sessão web do navegador e estão ignorados pelo Git. Eles
não são tokens OAuth da YouTube Data API, não ficam no SQLite e nunca armazenam
senha ou segredo de 2FA por código do AutoShorts. OAuth/API e sessão web são
credenciais independentes.

Na primeira utilização, use **Abrir sessão** e faça login manualmente. Depois,
**Verificar sessão** deve mostrar `LOGGED_IN`. Redirecionamento para login gera
`AUTH_REQUIRED`/`NEEDS_LOGIN`, não falha permanente.

## Reconciliação e segurança

Antes de criar, execute **Atualizar existentes**. O serviço lê os cards
estruturados do channel switcher e compara Channel ID, handle e nome completo.
Correspondência ambígua vira `POSSIBLE_DUPLICATE`; nunca cria automaticamente.
`Rota de Renda` permanece para revisão manual e nenhum duplicado é apagado.

O estado operacional autoritativo fica no SQLite (schema 30). `CREATED` e
`EXISTS` são preservados entre reinícios; **Retomar** começa no primeiro estado
incompleto. Timeouts de navegação usam até três tentativas, timeout de 45 s e
viram `FAILED_RECOVERABLE` quando a página continua inutilizável.

## CLI

```bash
.venv/bin/python tools/provision_youtube_channels.py --account google-c --inspect
.venv/bin/python tools/provision_youtube_channels.py --account google-c --dry-run
.venv/bin/python tools/provision_youtube_channels.py --account google-c --brand-plan-id ID
```

Use sempre o ID persistido da GoogleIdentity (`google-a`, `google-b`, etc.), não
e-mail ou alias humano. O browser permanece visível.

## Smoke controlado de 2026-10-21

- identidade: `google-c` (AutoTech);
- sessão persistente: reutilizada, `LOGGED_IN`;
- reconciliação: 9 canais remotos lidos;
- `PromptLab`, `Hardware Check` e `Tela Crítica`: `EXISTS`;
- `Rota de Renda`: `POSSIBLE_DUPLICATE`;
- único canal criado: `Olho no Olho`;
- handle: `@OlhoNoOlhoOficial`;
- Channel ID: `UC7YrH40Ty-POs_ld6_yPNNA`;
- logo: `data/branding/combat-sports/Olho no Olho/logo/v1/master.png`.

Nenhum outro canal foi criado durante esse smoke.
