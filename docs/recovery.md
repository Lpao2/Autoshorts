# Recuperação de jobs

O core calcula um `RecoveryPlan` antes de limpar ou alterar qualquer artefato.
O plano registra `youtube_id`, estado atual, próxima ação, motivo, artefatos
reutilizáveis e limpezas propostas.

## Política por estado

| Estado | Ação de recovery |
|---|---|
| `DISCOVERED` | iniciar download |
| `DOWNLOADING` | remover somente `.part`/`.ytdl` do `youtube_id` e baixar novamente |
| `DOWNLOADED` | reutilizar source validado por FFprobe; baixar novamente se ausente/inválido |
| `TRANSCRIBING` | estágio futuro, explicitamente não suportado |
| `TRANSCRIBED` | estágio futuro, explicitamente não suportado |
| `ANALYZING` | estágio futuro, explicitamente não suportado |
| `ANALYZED` | estágio futuro, explicitamente não suportado |
| `RENDERING` | preservar masters válidos, limpar temporários/inválidos e rerenderizar o restante |
| `RENDERED` | validar os três masters e continuar apenas na distribuição |
| `QUEUED` | nenhuma ação; produção encerrada |
| `COMPLETED` | nenhuma ação; publicação futura encerrada |
| `FAILED` | aguardar retry explícito; nunca fingir que foi produzido |

Para `RENDERED`, filas parciais daquele `youtube_id` são reconciliadas antes da
redistribuição. Os três destinos são recriados deterministicamente e o banco só
avança para `QUEUED` na transação que associa todos os Shorts. Uma segunda
execução encontra `QUEUED` e vira `NOOP`.

`StateStore.list_incomplete_videos()` lista trabalhos recuperáveis sem incluir
`QUEUED`, `COMPLETED` ou, por padrão, `FAILED`. O schema permanece na versão 2;
recovery não exigiu mudança estrutural no banco.
