# Ingestão de vídeos-fonte

O piloto de ingestão conecta o `ThemeMarketProfile` e seus `ContentSources` do
SQLite ao coletor legado baseado em `yt-dlp`. Ele descobre metadados antes de
baixar, aplica duração e deduplicação global por YouTube video ID e escolhe um
único candidato por prioridade da fonte, recência e ID estável.

```bash
autoshorts-ingest run --theme lol --region BR --language pt-BR --dry-run
autoshorts-ingest run --theme lol --region BR --language pt-BR --limit 1
```

O modo `--dry-run` não altera o banco nem baixa mídia. O modo real registra o
vídeo como `DISCOVERED`, promove para `DOWNLOADING` e somente grava
`DOWNLOADED` depois de FFprobe, decode integral e checksum SHA-256. Caminho,
checksum, duração, data de publicação e `ContentSource` ficam no registro
existente de `videos`. Falhas são persistidas como `FAILED` e resíduos `.part`
do download são removidos.

O diretório continua sendo o workspace legado:

`data/automation/<theme>/downloads/<creator>/<youtube_id>.mp4`

Esta etapa não analisa, renderiza, cria Publication ou enfileira upload.
