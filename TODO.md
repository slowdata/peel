# TODO

Curto e actual. O histórico está em [`docs/archive/`](docs/archive/README.md).

## Uso semanal

1. `uv run peel ouvir` — avaliar faixas e álbuns (envia no fim).
2. `uv run peel publicar` — escolher e publicar a edição.

## Em aberto

- Medir o efeito da fila de 7 álbuns e da afinidade ao fim de 4–6 semanas de
  avaliações (taxa de love/like por semana, consenso vs fonte única).
- Fontes candidatas, em teste paralelo antes de entrar: musicOMH (críticas de
  álbuns), Bandcamp Daily Album of the Day.
- Pitchfork News: alargar o parser só com títulos reais que falhem.
- Reedições sem marca no título (ex.: Alan Vega — *Collision Drive*) ainda
  passam; o `publicar` permite excluí-las à mão.
- Partir `main.run` em etapas, sem mudar resultados.
