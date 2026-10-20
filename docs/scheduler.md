# Scheduler local

O scheduler decide apenas **quando** uma `Publication` privada, já revisada e
aprovada, deve ser enviada. Produção de mídia e upload permanecem separados.
SQLite mantém fila, configurações de pausa, tentativas e histórico.

## Comandos

```bash
autoshorts-scheduler status
autoshorts-scheduler list
autoshorts-scheduler plan
autoshorts-scheduler enqueue PUBLICATION_ID
autoshorts-scheduler pause
autoshorts-scheduler resume
autoshorts-scheduler pause-channel PUBLISHING_CHANNEL_ID
autoshorts-scheduler resume-channel PUBLISHING_CHANNEL_ID
autoshorts-scheduler run-once --confirm-private-uploads
autoshorts-scheduler run --confirm-private-uploads
```

`plan` não cria jobs nem chama APIs remotas. `run` usa polling local, inicialmente
45 segundos, e não instala daemon, systemd ou cron. A confirmação explícita é
obrigatória nos comandos capazes de fazer upload.

## Segurança e recuperação

- Apenas Publications `private` podem entrar na fila piloto.
- O limite inicial é uma publicação por canal/dia, configurável no SQLite.
- Tentativas são limitadas a três.
- `FAILED_RECOVERABLE` pode ser tentado novamente; falhas permanentes não.
- `UPLOAD_UNCERTAIN` nunca recebe retry automático.
- Um job `RUNNING` encontrado após restart volta a `SCHEDULED` somente quando o
  ponto de execução do upload comprovadamente não foi alcançado.
- Se a execução podia ter começado, o job vira `UPLOAD_UNCERTAIN`. Se a
  Publication já contém video ID remoto e está `PUBLISHED`, vira `COMPLETED`.
- Atrasos de até seis horas executam assim que possível e são registrados no
  histórico. Atrasos maiores são recalculados para uma nova janela.

Os horários são persistidos em formato local junto ao timezone IANA. A função
de planejamento retorna um instante timezone-aware e suporta janelas que
atravessam meia-noite, dias preferenciais flexíveis, conflitos, janela
experimental explícita e bypass `FRESH` para `FRESHNESS_FIRST`.
