# AutoShorts

Pipeline local-first para transformar vídeos longos em Shorts verticais, desde a
descoberta da fonte até o agendamento e a publicação no YouTube. O projeto combina
Python, FFmpeg, Faster Whisper, Gemini, SQLite, YouTube Data API e Playwright em um
fluxo auditável, recuperável e orientado a múltiplos canais.

> Este é o repositório público do projeto. Credenciais, tokens OAuth, perfis de
> navegador, banco de produção, mídia e configurações locais nunca são versionados.

## O que o AutoShorts faz

1. Descobre e baixa fontes autorizadas com `yt-dlp`.
2. Valida mídia e duração com `ffprobe`/FFmpeg, usando amostragem para evitar
   leituras integrais desnecessárias.
3. Transcreve localmente com Faster Whisper e reaproveita artefatos válidos.
4. Solicita ao Gemini candidatos estruturados de cortes e metadados editoriais.
5. Valida timestamps, duração, contexto, transcript e score; um único candidato
   qualificado já é suficiente e o melhor vence pelo ranking.
6. Renderiza em 9:16, aplica crop por tema e grava legendas sincronizadas.
7. Mantém identidades, logos, banners e histórico de assets por Brand Plan.
8. Submete o resultado à revisão manual ou ao fluxo `AUTO_APPROVE` configurável.
9. Cria Publications explícitas e auditáveis, agenda e publica pela YouTube Data
   API, com proteção contra duplicidade e estado `UPLOAD_UNCERTAIN`.
10. Repõe o inventário por canal continuamente e remove a mídia local apenas após
    confirmação inequívoca da publicação.

## Principais capacidades

- GUI desktop em Tkinter e CLIs independentes por etapa.
- Catálogo de 20 temas, com até três canais por tema e políticas por canal.
- Distribuição justa de produções e reposição baseada em inventário.
- Coleta autenticada e não autenticada, cancelamento cooperativo e progresso.
- Transcrição local, cache de Whisper e retomada sem repetir trabalho caro.
- Análise estruturada, reranking editorial e metadados revisáveis.
- Crop, composição e legendas configuráveis por perfil visual de tema.
- Branding versionado, provenance, checksums e diretórios legíveis por canal.
- Sessões web persistentes e isoladas por Google Identity.
- Provisionamento e reconciliação idempotentes de canais do YouTube.
- OAuth multicanal com validação estrita do Channel ID esperado.
- Metadados explícitos de público infantil, mídia sintética, promoção paga,
  categoria, idioma, licença, incorporação, estatísticas e privacidade.
- Scheduler persistente, posting profiles e janelas editoriais por mercado.
- Upload idempotente, tentativa única em caso ambíguo e recuperação controlada.
- Serviços `systemd`, health check, backup, retenção e execução 24/7.
- SQLite com migrações incrementais; schema atual: **30**.

## Arquitetura

```text
Fonte autorizada
      │
      ▼
Coleta e validação ──► Transcrição ──► Gemini / ranking
                                              │
                                              ▼
                                  Render + crop + legendas
                                              │
                                              ▼
                           READY ──► revisão/AUTO_APPROVE
                                              │
                                              ▼
                           Publication ──► Scheduler ──► YouTube
                                              │
                                              ▼
                                  confirmação + retenção
```

O SQLite é a fonte autoritativa para jobs, produções, assets, Publications,
tentativas e bindings. Operações externas só avançam depois de validações locais;
reruns reaproveitam o estado já persistido.

## Requisitos

- Python 3.11+
- FFmpeg e `ffprobe`
- Chromium/Brave apenas para fluxos web guiados
- Dependências opcionais conforme o uso:
  - `automation`: `yt-dlp` e Playwright
  - `transcription`: Faster Whisper
  - `analysis`: Google Gen AI
  - `youtube-oauth`: clientes oficiais Google/YouTube

## Instalação local

```bash
git clone https://github.com/Lpao2/Autoshorts.git
cd Autoshorts
python -m venv .venv
.venv/bin/pip install -e '.[dev,automation,transcription,analysis,youtube-oauth]'
cp .env.example .env
```

Instale os navegadores do Playwright somente se usar os fluxos guiados:

```bash
.venv/bin/playwright install chromium
```

Nunca envie `.env`, arquivos OAuth, cookies, profiles, banco SQLite ou mídia ao
Git. As regras correspondentes já estão em `.gitignore`.

## Uso rápido

Abrir a interface:

```bash
.venv/bin/autoshorts-gui
```

Executar componentes separadamente:

```bash
.venv/bin/autoshorts-ingest --help
.venv/bin/autoshorts-produce --help
.venv/bin/autoshorts-orchestrator --help
.venv/bin/autoshorts-scheduler --help
.venv/bin/autoshorts-publish --help
```

Antes de qualquer upload, use o dry-run da publicação e confira canal, Channel
ID, arquivo, checksum, título, descrição, tags, categoria, idioma, audiência,
privacidade e demais declarações.

## Estados e segurança operacional

O pipeline distingue falhas de conteúdo, configuração, autenticação, navegação e
respostas externas ambíguas. Pontos importantes:

- `MANUAL_REVIEW` pode permanecer ativo por canal.
- `AUTO_APPROVE` é uma política explícita, não um efeito colateral.
- uma resposta ambígua do YouTube gera `UPLOAD_UNCERTAIN` e nunca retry cego;
- Channel ID e Google Identity são validados antes de trocar credenciais;
- canais e Publications existentes são reconciliados em vez de duplicados;
- a mídia publicada só é removida quando há estado `PUBLISHED` e video ID remoto;
- histórico, manifests, metadados e trilha de tentativas permanecem preservados.

## Operação contínua

Os exemplos em [`deploy/systemd`](deploy/systemd) cobrem scheduler, orquestrador,
backup e retenção. Ajuste primeiro os caminhos de
[`deploy/runtime.env.example`](deploy/runtime.env.example) e mantenha segredos em
um arquivo de ambiente separado, com permissões restritas.

Documentação detalhada:

- [Ciclo de produção](docs/production-cycle.md)
- [GUI local](docs/local-gui.md)
- [Transcrição](docs/transcription.md)
- [Análise Gemini](docs/gemini-analysis.md)
- [Metadados e revisão](docs/metadata-review.md)
- [Scheduler](docs/scheduler.md)
- [OAuth do YouTube](docs/youtube-oauth.md)
- [Provisionamento de canais](docs/youtube-channel-provisioning.md)
- [Smoke test de publicação](docs/youtube-publishing-smoke-test.md)
- [Recuperação](docs/recovery.md)

## Qualidade

```bash
.venv/bin/ruff check .
.venv/bin/pytest
.venv/bin/python -m compileall src
git diff --check
```

A suíte atual possui **493 testes** cobrindo domínio, persistência, GUI, coleta,
transcrição, análise, render, branding, OAuth, provisionamento, scheduler,
publicação, orquestração, recuperação e retenção.

## Privacidade e custos

O processamento pesado de mídia e transcrição pode ser local. Custos externos
dependem do provedor/modelo de IA, tráfego e infraestrutura escolhidos. A aplicação
não armazena senhas Google nem 2FA; OAuth da API e sessão web do Playwright são
credenciais distintas e ficam fora do repositório.

## Licença e autoria

Projeto desenvolvido por [Lucas Patrocínio](https://github.com/Lpao2). Consulte o
autor antes de reutilizar o projeto em produção comercial; não há licença aberta
declarada neste repositório.
