# Preparar canal

A GUI apresenta um fluxo único de preparação sem fundir os modelos internos.
`GoogleIdentity`, `PublishingChannel`, `ChannelBrandKit`, binding OAuth e
`PostingPolicy` continuam independentes.

`ResolvedChannelSetup` reúne esses dados apenas para leitura. O estado é derivado
por `resolve_channel_setup_status`:

- `UNCONFIGURED`: identidade administrativa ausente ou inativa;
- `BRANDING_PENDING`: Google configurado, identidade pública ainda não aprovada;
- `YOUTUBE_PENDING`: branding aprovado, canal YouTube ainda não conectado;
- `READY_FOR_SCHEDULING`: branding e YouTube prontos, scheduler desligado;
- `ACTIVE`: todas as partes prontas e PostingPolicy ativa;
- `ERROR`: binding conhecido em estado inválido.

Branding e YouTube são independentes. Um binding existente não é removido quando
o branding muda, e uma desconexão OAuth não apaga o BrandKit.

## Wizard

O botão **Preparar canal** mostra:

1. configuração básica e escolha da Google Identity;
2. blueprint e situação do branding, com geração/revisão de propostas;
3. instruções para criação manual do canal, utilitários de cópia e conexão OAuth;
4. resumo de branding, YouTube, scheduler e estado geral.

IDs técnicos não são editáveis. Tokens e referências de credencial não aparecem.
Erros 503 do Gemini informam indisponibilidade temporária, preservam a tela e não
criam candidatos falsos.

Google Identities aparecem pelo nome e email; o `identity_id` permanece interno.
Quando o YouTube já está conectado, o wizard oferece **Verificar conexão**,
**Reconectar** e **Desconectar** em vez de destacar uma nova conexão.

Branding também pode ser preenchido manualmente. Nome, handle, descrição,
tagline, paleta e briefs são revisáveis; paleta e briefs começam no blueprint do
arquétipo. A aprovação usa a mesma validação e o mesmo histórico versionado dos
candidatos gerados.

Uma geração é uma operação lógica coordenada para os três canais. Falhas
transitórias 429, 500, 502, 503, 504, timeout e conexão recebem no máximo três
tentativas, com esperas de aproximadamente 2 e 6 segundos. Erros determinísticos,
incluindo 400, 401, 403 e validação de schema, não são repetidos. Nada é persistido
até a resposta completa de 15 candidatos ser validada.

## Schema v11

O código atual aceita bancos v11 e continua rejeitando versões futuras. Uma GUI
que já estava aberta antes da atualização podia manter em memória o antigo limite
v10 e exibir `Unsupported state schema version: 11`; reiniciar a aplicação carrega
o módulo atualizado. Falhas de leitura agora mantêm configuração e linhas já
visíveis em vez de limpar a interface.
