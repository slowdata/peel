# Peel — Estado actual / TODO

Última actualização: 2026-06-15

## Estado operacional

Peel é agora um motor Python semanal de descoberta musical + exportador para o site estático
`peel-sept`.

Superfícies activas:

1. `peel` — ingestão, matching Spotify, feedback, scoring, reports, playlist e export JSON.
2. `peel-sept` — site Astro público em Cloudflare Pages (`https://peel.sept.pt`).
3. Spotify — playlist pública canónica `Peel — Weekly Discoveries`.

Playlist canónica actual:

```text
PEEL_PLAYLIST_ID=3iHETIGrWBdoY3a8jNrMox
https://open.spotify.com/playlist/3iHETIGrWBdoY3a8jNrMox
```

## Validação conhecida

Última validação local completa:

```bash
uv run pytest                  # 288 passed
uv run ruff check src/ tests/  # All checks passed
```

Site:

```bash
npm run build                  # 6 pages + OG/IG PNGs gerados
```

Rotas públicas verificadas:

```text
https://peel.sept.pt/
https://peel.sept.pt/2026-W24/
https://peel.sept.pt/2026-W23/
https://peel.sept.pt/pt/
https://peel.sept.pt/pt/2026-W24/
https://peel.sept.pt/og/2026-W24.png
https://peel.sept.pt/ig/2026-W24.png
```

## Feito

- [x] MVP semanal com Spotify, SQLite e GitHub Actions.
- [x] CLI backoffice com Typer/Rich.
- [x] Source attribution e consenso por música.
- [x] Feedback local (`love`, `like`, `meh`, `skip`, `ban`).
- [x] `ban` filtra URI + `artist/title` normalizados sem banir artista inteiro.
- [x] Relatório semanal Markdown em `data/reports/`.
- [x] Sync local/GitHub para `data/peel.db` e reports.
- [x] Source scoring inicial + `insufficient data`.
- [x] Registry declarativa de sources.
- [x] Guardian/The Quietus como fontes de álbuns/contexto.
- [x] The Quietus Tracks of the Month como fonte de tracks.
- [x] NPR New Music Friday — The Starting 5 como fonte de tracks.
- [x] KEXP — In Our Headphones como fonte de tracks.
- [x] Triagem dá prioridade a todas as tracks novas; pendentes só preenchem vagas livres.
- [x] Aquarium Drunkard “On The Turntable” como fonte de álbuns.
- [x] Bandcamp labels como fontes de álbuns.
- [x] Feature “7 Álbuns a Ouvir” com `album_mentions`.
- [x] Telegram digest com tracks, álbuns e escutas externas.
- [x] Exportador `peel site export` para `peel-sept/src/data/weeks/*.json`.
- [x] Export JSON com datas ISO `start_date`/`end_date`.
- [x] Export JSON com Spotify album links resolvidos para On Rotation.
- [x] Export JSON com `sources` como `{name, url}` para creditar curadores.
- [x] Playlist pública canónica consolidada e usada pela fonte de verdade.
- [x] Snapshot/compare do Spotify Release Radar como sinal pessoal fora da weekly.

## Estado do site `peel-sept`

- [x] Astro estático em Cloudflare Pages.
- [x] EN por defeito; PT em `/pt/`.
- [x] Dados musicais únicos; só UI/chrome traduzida.
- [x] Datas formatadas por locale a partir de `start_date`/`end_date`.
- [x] Social cards OG (`1200x630`) e Instagram (`1080x1080`) gerados no build.
- [x] Botões de partilha X/Web Share/download Instagram.
- [x] Spotify privacy-first: iframe só é criado após clique.
- [x] Player Spotify aparece só na semana mais recente; histórico mostra nota e tracks individuais.
- [x] Source chips linkam para homepages dos curadores.

## Atenções / riscos actuais

1. **`peel site export --weeks 2` usa a semana ISO actual.**
   - Em 2026-06-15 gerou `2026-W25.json` vazio ao correr sem override.
   - Se isto for publicado antes de haver dados reais W25, a homepage pode saltar para uma semana vazia.
   - Próximo fix recomendado: o exportador deve exportar a última semana com dados, ou aceitar `--week/current-week`, ou o cron deve correr só depois da run real criar dados.

2. **`peel playlist fill-week` não representa o Top 7 do site.**
   - O comando repõe todas as tracks da DB da semana (ex.: 11 em W24), incluindo items que o site filtra/rankeia fora do Top 7.
   - Para a playlist canónica semanal, usar a mesma lógica do export/site ranking.
   - Próximo fix recomendado: adicionar `peel playlist fill-site-week` ou alterar `fill-week` com opção `--site-top`.

3. **GitHub Actions secret deve manter o ID canónico.**
   - Valor esperado: `3iHETIGrWBdoY3a8jNrMox`.
   - Já foi actualizado manualmente pelo Dias em 2026-06-15.

4. **`web/` no repo `peel` continua untracked.**
   - É mock/reference visual; não faz parte do build actual.

## Próximos TODOs pequenos

1. [ ] Corrigir comportamento do `peel site export` para não criar semana vazia por defeito.
2. [ ] Alinhar comando de playlist com o Top 7 do site/export.
3. [ ] Documentar workflow semanal completo:
   - run Peel;
   - feedback;
   - export JSON;
   - validar playlist;
   - commit/push `peel-sept`.
4. [ ] Limpar docs antigas (`README.md`, `ROADMAP_V2.md`, `PLAN.md`) ou marcá-las como históricas.
5. [ ] Reavaliar source scoring após ~8 semanas de dados reais.
6. [ ] Continuar limpeza de feeds mortos / sources instáveis.

## Comandos úteis

```bash
# Motor
uv run pytest
uv run ruff check src/ tests/
uv run peel run
uv run peel report
uv run peel sources
uv run peel doctor sources

# Export site — cuidado com semana vazia se usado em segunda-feira sem dados
uv run peel site export --weeks 2

# Site
cd ../peel-sept
npm run build
```
