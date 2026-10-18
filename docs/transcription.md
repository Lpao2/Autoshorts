# Transcrição local

O AutoShorts oferece transcrição opcional com `faster-whisper`, sem acoplar o
core ao provider. Instale com:

```bash
.venv/bin/python -m pip install -e '.[dev,automation,transcription]'
```

Os defaults são adequados ao hardware de desenvolvimento sem CUDA:

- modelo `small`;
- device `cpu`;
- compute type `int8`;
- detecção automática de idioma;
- VAD ativo;
- timestamps por palavra ativos.

As variáveis `AUTOSHORTS_WHISPER_MODEL`, `AUTOSHORTS_WHISPER_DEVICE` e
`AUTOSHORTS_WHISPER_COMPUTE_TYPE` permitem mudar esses defaults. O modelo usa o
cache normal do Hugging Face e não é salvo no repositório.

## Lifecycle

```text
DOWNLOADED
  -> TRANSCRIBING
  -> data/<tema>/.work/<youtube-id>/transcript.json
  -> registro na tabela transcripts
  -> TRANSCRIBED
```

O JSON usa schema 1 e é escrito atomicamente. Contém idioma, probabilidade,
duração, texto, segmentos e palavras temporizadas. O SQLite v3 armazena somente
metadados e o caminho do arquivo, não o texto completo.

Se o processo morrer depois do JSON e antes do commit, recovery valida o artefato,
reconcilia o registro e promove para `TRANSCRIBED`. Arquivo ausente ou corrompido
gera `RETRANSCRIBE` quando o source ainda é válido. Um transcript válido em
`TRANSCRIBED` gera `REUSE_TRANSCRIPT` e aguarda a futura análise inteligente.

Na GUI, marque **Transcrição local (Whisper)** na aba Automático. A análise fake
continua independente e não consome o transcript nesta etapa.
