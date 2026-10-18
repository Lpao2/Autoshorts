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
| `TRANSCRIBING` | reconciliar JSON válido ou retranscrever a partir do source |
| `TRANSCRIBED` | reutilizar transcript válido; retranscrever se ausente/corrompido |
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
`QUEUED`, `COMPLETED` ou, por padrão, `FAILED`. O schema v3 adiciona a tabela
1:1 `transcripts`; as migrations v1→v2→v3 preservam os dados anteriores.
