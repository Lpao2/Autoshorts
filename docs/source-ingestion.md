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

## Substituição automática no orquestrador

Vídeos restritos por idade/login, exclusivos para membros ou indisponíveis são
registrados no cooldown existente e ignorados durante a descoberta. Uma falha
equivalente no download permite tentar outro candidato das fontes validadas do
mesmo perfil de tema/mercado. Há no máximo cinco tentativas de download por
produção, sem alterar a qualidade mínima dos cortes.

Somente após esgotar candidatos existentes, o Gemini pode sugerir até três URLs
de canais. O YouTube precisa confirmar o Channel ID e pelo menos dois vídeos
longos acessíveis; ao menos um deve respeitar a duração do perfil. Depois, uma
segunda avaliação Gemini precisa confirmar tema e idioma, com score >= 0.85,
usando títulos e descrições realmente obtidos do YouTube.

Uma fonte validada substitui a associação de prioridade 5, mantendo o registro da
fonte anterior. O histórico da troca, contexto, evidências e uso de tokens ficam
em `editorial_source_fallbacks` (schema 31). Há cooldown persistente de 24 horas
por perfil, inclusive após falhas ou reinício. Fontes existentes/desabilitadas
nunca são reativadas automaticamente. Validação de conteúdo não concede direitos
de uso: novas fontes continuam com `rights_status=UNKNOWN`.

O modelo pode ser configurado em `AUTOSHORTS_GEMINI_SOURCE_MODEL`; o padrão é
`gemini-3.8-flash`. Cada chamada usa no máximo duas tentativas do retry central.
Sem sugestões válidas, o ciclo encerra como falha recuperável. Desafios antibot,
HTTP 429 e falhas de rede interrompem a rotação; não são contornados com novas
fontes. A retomada de uma fonte já baixada continua reutilizando o estado local.
