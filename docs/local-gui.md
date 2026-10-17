# Interface local mínima — Linux

## Decisão

A interface deste marco é **Tkinter + ttk**. O produto continua pessoal e local;
CLI e GUI chamam o mesmo pipeline. Não há servidor, login, publicação automática,
framework web, banco de dados ou serviço de nuvem neste marco.

| Alternativa | Vantagem | Custo neste estágio | Decisão |
| --- | --- | --- | --- |
| Tkinter/ttk | Formulários, seletor nativo e tabela sem framework Python extra | Visual simples; pacote Tk do sistema necessário | Usar agora |
| PySide6 | Widgets e ferramentas adequados a uma aplicação desktop maior | Distribuição do Qt e mais dependências | Reavaliar se a revisão visual exigir |
| Flet | Interface moderna e suporte a vários destinos | Runtime e empacotamento adicionais | Não necessário neste marco |
| Web local | Boa opção para biblioteca, previews e histórico maiores | Servidor, frontend e comunicação com tarefas locais | Reavaliar depois |

A recomendação web anterior em `product-vision.md` tratava da biblioteca completa.
Esta decisão reduz a primeira entrega ao fluxo de vídeo + cortes + renderização.
Não impede outra interface usando os serviços do pipeline posteriormente.

Referências oficiais: [Tkinter](https://docs.python.org/3/library/tkinter.html),
[PySide6](https://doc.qt.io/qtforpython-6/gettingstarted.html),
[Flet](https://flet.dev/docs/getting-started/installation/),
[Flask](https://flask.palletsprojects.com/en/stable/server/).

## Instalar e abrir

No Ubuntu/Debian, instale as dependências do sistema uma vez:

```bash
sudo apt install python3-venv python3-tk ffmpeg xdg-utils
```

Na cópia existente, primeiro confira alterações locais. Não descarte mudanças
para conseguir atualizar a branch:

```bash
cd /home/lpao/Documents/ChatGPT/Autoshorts
git status --short
git switch codex/resume-audit
git pull --ff-only origin codex/resume-audit
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/autoshorts-gui
```

Se o ambiente virtual já existe, reutilize-o. O arquivo `.env` é opcional; sem ele,
FFmpeg/FFprobe são encontrados pelo PATH. Caso já tenha `.env`, abra-o no **Kate**
e confira `FFMPEG_PATH` e `FFPROBE_PATH`: caminhos Windows não funcionam no Linux.
Não sobrescreva sua configuração com o exemplo sem revisar.

Também é possível abrir com `.venv/bin/python -m autoshorts.gui`.
Execute numa sessão gráfica, não num terminal SSH sem display.

Para criar o atalho pessoal no menu de aplicativos:

```bash
.venv/bin/autoshorts-install-desktop
```

Depois procure **AutoShorts** no menu. Não é preciso abrir terminal nem ativar o
venv no uso diário. O atalho aponta para o Python do ambiente virtual utilizado;
reinstale o atalho se mover a pasta do projeto ou recriar o venv em outro local.
Para remover, apague `~/.local/share/applications/autoshorts.desktop` (ou o arquivo
correspondente dentro de `$XDG_DATA_HOME/applications`).

## Usar

1. Selecione o vídeo e a pasta de saída.
2. Abra um TXT de cortes ou edite o campo. Exemplo:

   ```text
   [00:00 - 00:05] - Primeiro corte
   [00:06 - 00:10] - Segundo corte
   ```

3. Clique em **Validar**. Confira títulos e durações efetivas na tabela.
4. Clique em **Executar**. Não precisa validar separadamente: Executar também
   verifica as entradas atuais. O editor é a fonte dos cortes; salvar TXT é opcional.
5. Acompanhe o percentual geral, o percentual por clipe e as mudanças de estado.
6. Ao concluir, clique em **Abrir pasta gerada** e revise os vídeos.

A linha abaixo do editor mostra resolução, extensão adicional e limite de duração
carregados da configuração. Por padrão os cortes recebem mais seis segundos,
limitados pela duração do vídeo e pelo máximo configurado.

A política inicial é sempre **Proteger existentes**, com **1 processo**, mesmo
que o `.env` defina outros valores. A GUI permite selecionar até quatro processos.
**Reutilizar válidos** verifica os vídeos existentes com FFprobe ao executar.
**Substituir existentes** exige confirmação na janela e substitui cada arquivo
somente depois de validar sua nova versão.

**Validar** não cria diretórios, MP4 nem manifest. O `--dry-run` da CLI conserva
seu comportamento: grava o manifest do plano. A CLI continua com os mesmos argumentos.
O progresso geral é a média dos percentuais dos clipes, não uma previsão de tempo.
100% por clipe significa vídeo validado e promovido ao nome final.

## Cancelar e fechar

- Cancelar sinaliza as tarefas em segundo plano. Cada FFmpeg ativo recebe término;
  após dois segundos sem saída ele é encerrado à força e aguardado.
- Inspeções FFprobe têm limite de 30 segundos; uma inspeção em andamento pode
  precisar terminar antes de aplicar o cancelamento. A janela continua respondendo.
- `.part.mp4` é removido ao encerrar cada render interrompido ou com falha.
- Vídeos já concluídos permanecem. O manifest registra esses resultados mesmo se
  outro clipe falhar ou o lote for cancelado.
- O manifest do lote fica `completed`, `failed` ou `cancelled`. Clipes sem resultado
  ficam `not_completed` em falhas ou `cancelled` em cancelamentos.
- Fechar durante um trabalho pede confirmação, solicita cancelamento e espera as
  tarefas encerrarem antes de destruir a janela.
- Erros e cancelamentos com manifest também liberam o botão para abrir a saída.
- Para continuar um lote interrompido, execute novamente com **Reutilizar válidos**.
  Não há retomada automática de manifest nem fila persistente nesta etapa.

## Arquitetura e testes

- `render_events.py`: eventos imutáveis e contrato de cancelamento.
- `video_processor.py`: progresso real via `ffmpeg -progress pipe:1`, stderr em
  arquivo temporário, controle de subprocessos e limpeza atômica.
- `pipeline.py`: orquestração e manifest, inclusive resultados parciais.
- `gui_jobs.py`: entradas imutáveis, validação sem efeitos e uma tarefa por vez.
- `gui.py`: widgets atualizados exclusivamente na thread principal por `after()`.
  O worker e os renderizadores apenas colocam mensagens numa `Queue`.
- `desktop.py`: instalação opcional do atalho no menu do usuário.

```bash
.venv/bin/ruff check .
.venv/bin/pytest
```

Os testes da janela são ignorados quando não há display. Para executá-los também
num ambiente sem desktop, instale `xvfb` e `xauth`, e rode:

```bash
xvfb-run -a .venv/bin/pytest
```

Teste manual no desktop do usuário:

- Abra pelo menu; seletores e abertura da pasta precisam funcionar no seu desktop.
- Valide cortes corretos e incorretos; altere o editor após validar e execute.
- Renderize dois cortes com um e dois processos e revise imagem/áudio dos MP4.
- Teste proteger, reutilizar e substituir arquivos numa pasta de teste.
- Cancele um corte longo; confira resposta da janela e ausência de `.part.*`.
- Feche durante um render, recuse uma vez, depois confirme o fechamento seguro.
- Reabra; confira que os arquivos concluídos continuam disponíveis.

A biblioteca de temas, prompts, presets, histórico pesquisável, canais e revisão
antes da publicação continua no plano de evolução; esta GUI é a primeira superfície
para o motor existente, sem antecipar esses módulos.

## Verificação desta entrega — 2026-10-17

Antes das alterações: 20 testes passando, com FFmpeg real, e Ruff limpo na base
`cee5e49`. Após a implementação: **35 testes passando com display virtual**, sem
ignorar testes da janela, e Ruff limpo. A janela foi inspecionada por captura de tela.
Também foram executados os entry points instalados: CLI em dry-run, renderização
e reutilização de vídeo sem áudio, e instalador do atalho em diretório de teste.

Os testes incluem cancelamento de dois renders, término de subprocesso silencioso,
limpeza de temporários, preservação de clipes concluídos em falha/cancelamento,
falha de substituição sem perda do arquivo anterior, GUI responsiva, recuperação
de erro e fechamento aguardando o worker. A integração com o menu/gerenciador de
arquivos do desktop e a revisão humana de imagem/áudio ficam para o roteiro manual.
