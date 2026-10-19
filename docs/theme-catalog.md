# Biblioteca de temas

## Objetivo

A biblioteca guarda templates editoriais reutilizáveis sem confundi-los com os
temas que já estão configurados para produção. Um `ThemeTemplate` não coleta,
renderiza nem publica nada. A criação de um `ThemeProfile` é uma ação explícita
por meio de `instantiate_theme`; o perfil nasce inativo e seus canais também.

## Auditoria do legado

A fonte auditada em modo somente leitura foi o repositório privado
`/home/lpao/Documents/ChatGPT/Autoshorts-dev`. Foram encontrados 13 prompts de
seleção de vídeo com conteúdo aproveitável, além de prompts antigos de banner e
avatar: League of Legends, Academia, Atualidades, Anime, Animais, Culinária,
Comédia, Desenhos Animados, Fé e Espiritualidade, Filmes, Futebol,
Influenciadores e Minecraft.

O legado também continha um único registro operacional LoL com login, senha e
estado de navegador, scripts de criação de contas, cookies e arquivos de sessão.
Esses dados são material sensível/operacional e foram deliberadamente excluídos
da migração. Nenhum email, senha, cookie, token ou estado de navegador é salvo no
catálogo.

O arquivo `gerador_temas.py` enumerava ideias adicionais apenas em comentários.
Como não havia prompt editorial real para elas, não foram criados templates
fictícios. O `README.md` legado não continha documentação além do título.

## Modelo

Cada template tem chave estável, nome público, descrição, tags, estratégia de
fonte, padrões de produção, estado e proveniência. Estados permitidos: `DRAFT`,
`READY`, `ACTIVE` e `ARCHIVED`. Proveniências: `LEGACY`, `MANUAL`, `GENERATED` e
`REFINED`.

Os prompts são versionados: uma mudança cria uma nova linha e conserva as
versões anteriores. Cada tema possui três arquétipos (slots 1, 2 e 3) com foco
editorial, estilo de headline, tom, direção visual e paleta. Campos opcionais de
marca incluem nome público, handle, descrição, tagline e caminhos de logo/banner.
Imagens não são armazenadas no banco; a localização planejada é:

`data/branding/<theme>/<channel-id>/`

## Persistência e semeadura

O schema SQLite v9 adiciona `theme_catalog`, `theme_prompt_versions` e
`theme_channel_archetypes`. A migração v8→v9 é aditiva e não altera vídeos,
shorts, análises ou `channel_bindings`.

Na abertura do `StateStore`, `config/theme-catalog.legacy.json` insere somente
chaves ausentes. A rotina é idempotente e nunca sobrescreve uma edição existente.
Por isso, mudanças feitas na GUI sobrevivem a reinícios e a novas execuções da
semente.

## Interface

A aba **Biblioteca de temas** lista tema, estado, origem, quantidade de versões
de prompt e arquétipos. É possível ver detalhes e editar nome, descrição, estado
e prompt ativo. Salvar uma alteração marca a proveniência como `REFINED` e cria
uma nova versão do prompt quando o texto mudou. A aba não ativa produção.

## Compatibilidade LoL

O perfil LoL operacional continua no arquivo de automação existente. A biblioteca
não o recria, não substitui canais e não modifica o vínculo OAuth do LoL A. O
template LoL recebe estado de catálogo `ACTIVE` apenas como indicação de uso
atual; isso não é um comando de execução.
