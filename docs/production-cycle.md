# Ciclo de produção local

O modo automático de desenvolvimento fecha o ciclo local sem IA, legenda ou
publicação:

1. descobre e baixa/reutiliza um vídeo autorizado;
2. o provider `TEST / DEVELOPMENT ONLY` gera exatamente três cortes;
3. FFmpeg renderiza e valida `01.mp4`, `02.mp4` e `03.mp4`;
4. SQLite registra os masters e move o vídeo para `RENDERED` em uma transação;
5. cada corte vai para a conta que possui o mesmo slot persistente;
6. SQLite registra as filas e move o vídeo para `QUEUED` em uma transação;
7. somente então o arquivo-fonte baixado é removido em best effort.

`COMPLETED` fica reservado para quando os três Shorts forem publicados e o
cleanup de publicação estiver concluído.

## Layout

```text
data/<tema>/
  creators/<criador>/<youtube-id>/
    01.mp4
    02.mp4
    03.mp4
  accounts/slot-1-<conta>/queue/<youtube-id>_01.mp4
  accounts/slot-2-<conta>/queue/<youtube-id>_02.mp4
  accounts/slot-3-<conta>/queue/<youtube-id>_03.mp4
```

As filas usam hard links quando o filesystem permite e cópia atômica como
fallback. `RENDERED`, `QUEUED` e `COMPLETED` bloqueiam nova produção para o mesmo
`youtube_id`. Um `FAILED` não é tratado como produzido.

## Execução pela GUI

Execute `.venv/bin/autoshorts-gui`, abra a aba **Automático** e configure:

- tema, criador e contas habilitadas nos slots 1, 2 e 3;
- autenticação YouTube `Brave`;
- um vídeo por criador;
- `TEST / DEVELOPMENT ONLY` ativo;
- `Somente download` desativado.

Ao concluir, a interface exibe `Render 1/3` a `Render 3/3` e o estado final
`QUEUED`. Nenhum upload é realizado.
