# Análise inteligente com Gemini

O modo real usa somente o transcript local. Cada segmento é enviado com um ID e o
Gemini escolhe três intervalos contínuos. O AutoShorts resolve os segundos a partir
dos segmentos, valida duração (20–60 s), sobreposição, ranking, headline e score,
e só então permite o render.

## Instalação e configuração

```bash
cd /home/lpao/Documents/ChatGPT/Autoshorts
.venv/bin/python -m pip install -e '.[dev,automation,transcription,analysis]'
export GEMINI_API_KEY='sua-chave'
export AUTOSHORTS_GEMINI_MODEL='gemini-3.8-flash'       # opcional
export AUTOSHORTS_GEMINI_MONTHLY_BUDGET_USD='8.00'     # opcional
.venv/bin/autoshorts-gui
```

Na aba **Automação**, desmarque **Somente download**, marque **Gemini — análise
real** e **Transcrição local (Whisper)**, selecione tema/criadores e execute. Não
marque simultaneamente a análise fake.

A chave existe apenas no ambiente do processo. Ela não é salva em JSON, SQLite,
logs ou na interface. Não coloque a chave no repositório.

## Artefatos, custo e retomada

Cada vídeo produz `<tema>/.work/<youtube-id>/analysis.json` no workspace, com
schema versionado, três seleções, clips resolvidos, headlines, hash do prompt e
usage retornado pela API. O SQLite v4 guarda metadados e uma linha de custo por
chamada faturável conhecida.

O preço local para `gemini-3.8-flash` é US$ 0,75 por milhão de tokens de entrada e
US$ 3,75 por milhão de tokens de saída (thinking incluído), vigente até
31/12/2026. É uma estimativa; o faturamento Google é a fonte final. Modelos sem
preço local configurado são bloqueados antes da chamada.

Ao atingir o orçamento mensal local (US$ 8 por padrão), novas análises Gemini são
bloqueadas. Jobs locais e retomadas sem nova chamada continuam funcionando. Um
`analysis.json` válido é reutilizado depois de reiniciar.

## Primeiro teste real

Os testes automatizados usam cliente simulado e não fazem chamadas externas. O
teste qualitativo real com `Yg-bipv4fO0` requer `GEMINI_API_KEY` no ambiente.
