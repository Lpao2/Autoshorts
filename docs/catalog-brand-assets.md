# Assets visuais de marca

`BrandImageProvider` desacopla geração de imagem do restante do AutoShorts. O
contrato recebe um `PlannedChannelBrand`, caminho temporário e prompt composto a
partir de regras globais, `ThemeTemplate`, `ChannelArchetype` e Brand Plan.

`BrandAssetService` aceita somente planos `APPROVED`. Cada logo ou banner é
gerado numa pasta temporária, decodificado e validado, recebe checksum SHA-256 e
preview, e só então a pasta é publicada por rename atômico. O SQLite guarda
metadata; os pixels permanecem em:

```text
data/branding/<theme_key>/<brand_plan_id>/<logo|banner>/vN/
  master.png
  preview.png
  manifest.json
  youtube.jpg  # somente quando o PNG do banner excede 6 MB
```

Logos são 1024×1024. Banners são 2560×1440, com prompt orientado à zona segura
central de 1235×338. Masters nunca são sobrescritos. Uma regeneração cria a
próxima versão e apenas uma versão por Brand Plan/tipo pode ficar `APPROVED`;
aprovar outra versão transforma a anterior em `RETIRED`.

O prompt global de banners usa linguagem de design de marca minimalista: fundo
forte, um motivo dominante, nome e espaço negativo generoso. Ele limita elementos
decorativos e rejeita explicitamente densidade típica de concept art, wallpaper,
pseudo UI/HUD e ilustração excessivamente ornamentada. Tagline, handle e URL não
entram nos banners.

O manifest no filesystem permite reconciliar uma publicação concluída antes de
seu INSERT no SQLite. Pastas `.tmp-*` são descartadas e nunca contam como asset.
A fila ignora versões `VALIDATED` ou `APPROVED`, portanto retoma apenas itens
pendentes. A concorrência é deliberadamente limitada a um worker enquanto a
execução compartilha uma conexão SQLite.

O schema v15 estende `brand_asset_metadata` com versão, tamanho, preview e path
de entrega. Geração termina em `VALIDATED`; aprovação e rejeição são ações
humanas separadas na Biblioteca de temas.

`FakeBrandImageProvider` cobre testes sem rede. `PreparedFileBrandImageProvider`
integra imagens reais produzidas por um provider fora do processo, normalizando
dimensões pelo mesmo pipeline. O runtime da GUI não presume credenciais nem
provider configurado.
