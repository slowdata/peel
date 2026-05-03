# Peel — TODO actual

Última actualização: 2026-05-02

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
- [x] `unmatched.source_url` para preservar links externos.
- [x] Telegram digest com tracks, álbuns e escutas externas.

## O que testar agora

### Sanidade local

```bash
uv run ruff check
uv run pytest
uv run peel doctor
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

### Próxima run semanal

Depois do próximo cron de sábado, confirmar:

- [ ] Playlist recebeu apenas tracks novas e deduplicadas.
- [ ] Quietus aparece como álbum/contexto, não como unmatched track.
- [ ] Telegram mostra secção de álbuns quando houver álbuns.
- [ ] Telegram mostra “Escutas externas” quando houver unmatched com link.
- [ ] Report semanal inclui links em `Unmatched`.
- [ ] `peel sources` continua legível depois de mais uma semana de histórico.

## Próximos TODOs pequenos

1. [ ] Adicionar Bandcamp Daily como source `context`/external inbox, sem playlist directa.
2. [ ] Adicionar Pitchfork Best New Albums como source de álbuns.
3. [ ] Adicionar First Floor/Substacks como contexto, uma de cada vez.
4. [ ] Rever `source_runs` no fim de Maio e decidir se scoring passa a usar histórico real.
5. [ ] Em `peel sources`, mostrar “insufficient data” para fontes com poucas tracks.
6. [ ] Só depois considerar matcher Spotify mais agressivo.

## Notas de cautela

- `data/peel.db` é versionado intencionalmente, mas não deve guardar tokens ou dados sensíveis.
- `.env` nunca deve ser commitado.
- Push SSH pode falhar sem chave carregada; workaround: `gh`/HTTPS.
- Anthropic/Claude API esteve bloqueada por billing/credits baixos; usar GPT mini para tarefas fechadas.
