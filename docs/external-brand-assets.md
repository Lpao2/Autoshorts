# Importação de assets externos de marca

O AutoShorts aceita logos e banners produzidos fora do aplicativo sem usar um
provider de geração. A importação não cria um segundo catálogo: o arquivo passa
pelo mesmo `BrandAssetService`, versionamento, publicação atômica, preview,
checksum, persistência e estados dos assets gerados.

## Staging por Brand Plan

A estrutura convencional é:

```text
data/branding-import/<theme_key>/<brand_plan_id>/
  logo.png
  banner.png
```

O diretório é resolvido por `brand_plan_id`, não pelo nome público. O
`theme_key` da pasta precisa corresponder ao plano. Arquivos desconhecidos são
ignorados; um `brand_plan_id` inexistente ou colocado sob o tema errado bloqueia
a leitura do lote com mensagem clara.

## Pacote final e preparação canônica

Para pacotes externos já organizados, `BrandAssetPackageService` executa um
dry-run antes de qualquer escrita. A pasta do canal pode usar o
`brand_plan_id` exato (preferencial) ou um nome público com correspondência
normalizada exata e não ambígua. Não há fuzzy matching. ZIPs passam por
extração limitada, sem caminhos absolutos, `..`, links simbólicos ou entradas
criptografadas.

A preparação é explícita no relatório: logos quadrados podem receber resize
Lanczos; banners já próximos de 16:9 podem receber resize proporcional e crop
central mínimo. Logos não quadrados e banners distantes de 16:9 ficam em
`NEEDS_ATTENTION`. Os originais são copiados como somente leitura para
`data/branding-import-sources/`; os canônicos ficam separados em
`data/branding-import-prepared/`. O importador estrito continua sem fazer
transformações silenciosas.

O schema v17 registra pacote, nome relativo, cópia preservada, SHA-256 e
dimensões da fonte, além da transformação canônica. O manifest consolidado
reúne essa provenance com versão, status e caminhos de cada logo/banner.

## Manifest JSON

Caminhos relativos são resolvidos a partir do diretório do manifest. Cada item
pode conter logo, banner ou ambos:

```json
{
  "assets": [
    {
      "brand_plan_id": "00000000-0000-0000-0000-000000000000",
      "logo_path": "assets/canal-a-logo.png",
      "banner_path": "assets/canal-a-banner.png"
    }
  ]
}
```

## Contrato de validação

- Logo: PNG decodificável, exatamente 1024×1024.
- Banner: PNG decodificável, exatamente 2560×1440.
- O importador nunca redimensiona, recorta ou deforma silenciosamente.
- O banner deve chegar com nome e motivo importante dentro da safe area central;
  isso permanece uma verificação visual humana, não uma classificação artística
  automática.
- O master é preservado. Se exceder 6 MB, o pipeline cria um `youtube.jpg` de
  entrega sem substituir o master.
- Todo asset válido recebe preview, SHA-256, manifest local, direção visual
  resolvida e estado `VALIDATED`.
- A origem persistida é `external-import` e o modelo é `external`.
- Se o SHA-256 for igual ao da versão atual, a operação retorna `duplicate` e não
  cria uma nova versão.
- Um conteúdo diferente cria a próxima versão disponível sem sobrescrever as
  anteriores.

## GUI

Em **Biblioteca de temas → Assets de marca** estão disponíveis **Importar
logo**, **Importar banner**, **Importar ambos**, **Importar pasta/lote** e
**Importar manifest JSON**. A revisão visual por tríades também oferece as três
ações individuais.

Antes de escrever qualquer asset, a GUI abre uma tabela de correspondência com
tema, canal, tipo e arquivo externo. Somente **Confirmar importação** inicia o
lote. Falhas individuais são apresentadas no resumo e não impedem os demais
itens. Nenhum asset é aprovado automaticamente.
