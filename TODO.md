# Peel — TODO actual

Última actualização: 2026-05-31

## Pronto / feito

- [x] MVP semanal com Spotify, SQLite e GitHub Actions.
- [x] CLI backoffice com Typer/Rich.
- [x] Source attribution e consenso por música.
- [x] Feedback local (`love`, `like`, `meh`, `skip`, `ban`).
- [x] Relatório semanal Markdown em `data/reports/`.
- [x] Sync local/GitHub para `data/peel.db` e reports.
- [x] Source scoring inicial.
- [x] `peel doctor sources` para validar registry de fontes.
- [x] Caps de segurança por source/run e filtro de items antigos.
- [x] Guardian Music como fonte de álbuns/contexto.
- [x] The Quietus tratado como álbum/contexto, não playlist directa.
- [x] The Quietus Tracks of the Month como fonte de tracks.
- [x] `unmatched.source_url` para preservar links externos.
- [x] Telegram digest com tracks, álbuns e escutas externas.
- [x] Playlists temporárias por semana: `peel playlist fill-week`.
- [x] Registry limpa: 9 feeds mortos arquivados em `archived_sources`.
- [x] `peel sources` enriquecido com métricas reais de `source_runs`.
- [x] NPR New Music Friday — The Starting 5 como fonte de álbuns/contexto.

## O que testar agora

### Sanidade local

```bash
uv run ruff check
uv run pytest
uv run peel doctor
uv run peel doctor sources
uv run peel status
```

### Backoffice diário

```bash
uv run peel sync pull
uv run peel tracks --sources
uv run peel feedback
uv run peel report
uv run peel sources --weeks 4
uv run peel sync push
```

### Playlists temporárias

```bash
uv run peel playlist fill-week 2026-W22 --playlist-id <spotify_playlist_id> --dry-run
uv run peel playlist fill-week 2026-W22 --playlist-id <spotify_playlist_id>
uv run peel playlist fill-week 2026-W22 --playlist-id <spotify_playlist_id> --unrated-only
```

## Próximos TODOs pequenos

1. [ ] NPR New Music Friday — avaliar próxima extensão:
   - `The Lightning Round` como álbum/contexto?
   - `Dora's Corner` como álbum/contexto?
   - Long List só com caps/filtros fortes (`Rock/Alt/Indie`, `R&B/Soul`, `Rap/Hip-Hop`).
   - Evitar ingestão cega da Long List completa.
2. [ ] Adicionar Bandcamp Daily como source `context`/external inbox, sem playlist directa.
3. [ ] Adicionar Pitchfork Best New Albums como source de álbuns.
4. [ ] Adicionar First Floor/Substacks como contexto, uma de cada vez.
5. [ ] Em `peel sources`, mostrar “insufficient data” para fontes com poucas tracks.
6. [ ] Só depois considerar matcher Spotify mais agressivo.

## Notas de cautela

- `data/peel.db` é versionado intencionalmente, mas não deve guardar tokens ou dados sensíveis.
- `.env` nunca deve ser commitado.
- Push SSH pode falhar sem chave carregada; workaround: `gh`/HTTPS.
- Para playlists temporárias, criar playlist vazia manualmente no Spotify e usar `peel playlist fill-week`.
- Anthropic/Claude API esteve bloqueada por billing/credits baixos; usar GPT mini para tarefas fechadas.
