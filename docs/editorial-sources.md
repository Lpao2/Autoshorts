# Perfis editoriais e fontes

O SQLite é a fonte operacional autoritativa para categoria e fontes. Cada
`theme_market_profiles` identifica uma combinação de tema, plataforma, região e
idioma. Isso permite, por exemplo, manter perfis independentes para
`lol/youtube/BR/pt-BR` e `lol/youtube/US/en-US`.

`content_sources` identifica canais externos por `(platform,
external_channel_id)`. O status de validação editorial é independente de
`rights_status`: cadastrar uma fonte não representa autorização de reutilização.
`theme_content_sources` associa cinco fontes ao perfil, com prioridade e faixa
alvo de duração.

O arquivo `config/editorial-sources.br-pt-BR.seed.json` é um artefato revisado de
importação e auditoria; o pipeline não o consulta em runtime. Para reaplicar o
seed de forma idempotente:

```bash
autoshorts-editorial import-seed config/editorial-sources.br-pt-BR.seed.json
autoshorts-editorial summary
```

As amostras armazenam a data do upload mais recente, a quantidade de vídeos
analisados e as contagens long-form e dentro da faixa de 900–2400 segundos. A
validação inicial usa os 20 uploads mais recentes de cada canal e mantém
`rights_status=UNKNOWN`.
