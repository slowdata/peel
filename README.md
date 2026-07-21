# Peel — Music Discovery Aggregator

**Peel** é um agregador automatizado de descoberta musical que corre semanalmente (via cron) para:

1. Recolher recomendações de curadores humanos (Pitchfork, BBC 6 Music, NTS, etc.)
2. Procurar as faixas no Spotify
3. Adicionar automaticamente a uma playlist pessoal

Sem algoritmos, sem bolhas — apenas bom gosto humano, entregue.

## Quick Start

### Prerequisites

- Python 3.11+
- `uv` (universal Python package manager)
- Uma conta Spotify com acesso à Spotify Web API

### Local Setup

1. **Clone e instala dependências:**
   ```bash
   git clone <repo-url>
   cd peel
   uv sync
   ```

2. **Regista a app no Spotify:**
   - Vai a https://developer.spotify.com/dashboard
   - Cria uma nova app
   - Regista o Redirect URI como `http://127.0.0.1:8888/callback`
   - Copia o Client ID e Client Secret

3. **Gera o refresh token:**
   ```bash
   cp .env.example .env
   # Preenche SPOTIFY_CLIENT_ID e SPOTIFY_CLIENT_SECRET no .env
   uv run python scripts/bootstrap_refresh_token.py
   # O script abre o browser, tu autorizas, ele imprime o refresh_token
   # Copia-o para o .env como SPOTIFY_REFRESH_TOKEN
   ```

   Os refresh tokens Spotify expiram após 6 meses. Quando renovares:
   ```bash
   uv run python scripts/bootstrap_refresh_token.py
   gh secret set SPOTIFY_REFRESH_TOKEN
   ```
   Se uma run falhar com `invalid_grant`, substitui o token em `.env` e no
   GitHub Secrets; não faças retry com o token antigo.

4. **Cria a playlist alvo:**
   - No Spotify, cria uma playlist privada chamada "Peel"
   - Copia o ID da playlist (vê na URL: `spotify.com/playlist/{ID}`) para .env como PEEL_PLAYLIST_ID

5. **Testa localmente:**
   ```bash
   uv run pytest          # Valida todo o código
   uv run peel run        # Executa uma run completa
   ```

## Automated Weekly Run

O projeto corre automaticamente ao sábado (10:00 UTC) via [GitHub Actions](/.github/workflows/weekly.yml).

Para dispatch manual (testes):
```bash
# Na página de Actions do repo, clica em "weekly peel run" → "Run workflow"
```

O estado (tracks vistas, histórico de sources) fica guardado em `data/peel.db` e sincronizado ao repo após cada run.

## Project Structure

```
peel/
├── src/peel/
│   ├── config.py           # Carregamento de secrets do .env
│   ├── models.py           # Track (datamodel)
│   ├── spotify_client.py   # Auth + search + playlist write
│   ├── matcher.py          # Fuzzy matching de faixas
│   ├── db.py               # SQLite state management
│   ├── main.py             # Orquestração principal
│   └── sources/
│       ├── base.py         # Interface Source (ABC)
│       └── rss.py          # RSSSource + PitchforkBNT
├── tests/                  # Suite de testes (62 testes)
├── scripts/
│   └── bootstrap_refresh_token.py  # Geração inicial do refresh token
├── data/
│   └── peel.db            # SQLite state (tracks vistas, histórico)
└── .github/workflows/
    └── weekly.yml         # GitHub Actions: cron + manual dispatch
```

## Development

### Running Tests

```bash
uv run pytest -v
```

### Code Quality

```bash
uv run ruff format src/ tests/
uv run ruff check src/ tests/
```

### Doctor

```bash
uv run peel doctor
uv run peel doctor sources
uv run peel doctor sources --json
```

### Source scoring

```bash
uv run peel sources
uv run peel sources --weeks 4
uv run peel sources --json
```

### Spotify Release Radar snapshots

Release Radar é sinal pessoal/algorítmico, não source editorial. Fica fora da
weekly automática; usa-se para auditoria de cobertura e afinidade futura:

```bash
uv run peel radar snapshot --week 2026-W28
uv run peel radar snapshot --week 2026-W28 --no-write
uv run peel radar liked --week 2026-W28
uv run peel radar compare --week 2026-W28
```

### Affinity genre cache

Affinity v1 uses local feedback first. Optional genre tags are cached locally and
never fetched during the weekly run. Backfill explicitly, with dry-run first:

```bash
uv run peel affinity backfill-genres --dry-run --limit 50
uv run peel affinity backfill-genres --source musicbrainz --limit 20 --sleep 1.5 --min-tag-count 2
```

### Playlist safety caps

```bash
PEEL_MAX_TRACKS_PER_SOURCE=8
PEEL_MAX_TRACKS_PER_RUN=28
PEEL_MAX_SOURCE_ITEM_AGE_DAYS=30
```

Só sources `kind = "track"` podem entrar na playlist. Sources `album`, `context`, `podcast`, `scrape` ou `manual_spotify` ficam fora da playlist automática. Items publicados há mais de `PEEL_MAX_SOURCE_ITEM_AGE_DAYS` dias são ignorados quando a source expõe data.

A playlist de triagem é a fila real para ouvir: todas as tracks novas da run entram primeiro. Só se faltarem lugares até ao cap entram tracks pendentes, sem feedback, de runs anteriores. Nos pendentes, consenso mantém prioridade e o score da source já inclui feedback; repetições da mesma source sofrem uma penalização linear suave — não há quotas nem caps. O Telegram só é enviado depois de Spotify actualizar a triagem e identifica cada faixa como `🆕 nova` ou `↻ pendente`.

### Album queue

A weekly também confirma uma fila independente de até sete álbuns. A primeira
observação de cada `(artista, álbum, source)` é imutável; polling repetido só
actualiza a última observação. Menções editoriais novas e consenso entram antes
de pendentes sem feedback; labels Bandcamp são complementares e singles nunca
são elegíveis. A mesma snapshot é consumida pela CLI, Telegram e export do site.

```bash
uv run peel albums                 # fila activa e links de escuta
uv run peel albums --unrated       # apenas pendentes
uv run peel albums --open 1        # abre o link do rank 1
uv run peel albums feedback        # love|like|meh|skip|ban (q não grava)
uv run peel albums refresh --week 2026-W29 --dry-run
uv run peel albums refresh --week 2026-W29  # reconstrói explicitamente a snapshot
uv run peel site export            # reexporta snapshots sem as recalcular
```

`albums refresh` é o único caminho para substituir deliberadamente uma semana
já existente; uma re-exportação normal apenas lê links e ordem congelados.

```bash
uv run peel triage                 # fila confirmada, na ordem Spotify
uv run peel triage --unrated       # só tracks activas sem avaliação (--pending é alias)
uv run peel feedback               # avalia a fila activa, pela ordem Spotify
uv run peel feedback --history     # backlog histórico explícito
uv run peel feedback --history --week 2026-W28
uv run peel triage feedback        # alias compatível de `peel feedback`
uv run peel triage --open          # abre Spotify
uv run peel triage bootstrap       # uma vez: importa a triagem já existente
uv run peel finalize --week 2026-W29 # após feedback: confirma o Top 7 em Spotify e no site
```

`finalize` grava o Top 7 e a ordem que Spotify confirmou. Re-exports posteriores
usam esse snapshot canónico; semanas ainda não finalizadas mantêm o ranking editorial.

Os comandos humanos escondem logs internos por defeito; para diagnóstico local,
usa `uv run peel --verbose triage` (a weekly mantém logs JSON completos para CI).

Fontes `album` activas incluem Guardian album reviews e The Quietus album reviews. Entram em `Albums / Context`, relatório e Telegram, mas não vão para Spotify matching/playlist. NPR New Music Friday — The Starting 5 e KEXP — In Our Headphones são fontes `track`: entram no matching/playlist como novidades curadas.

`tracks_found` é calculado a partir dos dados persistidos: matches + unmatched. O comando também mostra telemetria real de `source_runs` (`Runs`, `Fetched/Fresh`, `Proc`, `Stale/Cap/Err`) para distinguir qualidade de fonte, backlog, caps e falhas.

### Playlists temporárias por semana

Para recriar uma semana numa playlist Spotify existente:

```bash
uv run peel playlist fill-week 2026-W22 --playlist-id <spotify_playlist_id>
uv run peel playlist fill-week 2026-W22 --playlist-id <spotify_playlist_id> --unrated-only
uv run peel playlist fill-week 2026-W22 --playlist-id <spotify_playlist_id> --dry-run
```

Uso típico: criar manualmente uma playlist privada vazia no Spotify, copiar o ID e preencher com uma semana antiga para ouvir/avaliar.

### Sync

```bash
uv run peel sync status
uv run peel sync pull
uv run peel sync push
```

### Roadmap (v2+)

- [ ] Mais fontes: BBC 6 Music Recommends, NTS Radio scraping
- [ ] Configuração de fontes dinâmica (via `config.yml`, não hardcoded)
- [ ] Web UI para gérir playlists / fontes
- [ ] Notificações (email / Discord) com resumo semanal
- [ ] Recomendações personalizadas baseadas em escuta histórica

## Architecture Notes

- **Sem ORM:** SQLite com `sqlite3` da stdlib para aprender SQL manualmente
- **Fuzzy matching:** `rapidfuzz.fuzz.token_set_ratio` para robustez contra sufixos (Deluxe, Remastered, feat., etc.)
- **Structured logging:** `structlog` com JSON output para GitHub Actions parsing
- **OAuth refresh token flow:** Access tokens expiram em ~1h, refresh automático a cada run (aceitável para semanal)
- **Resiliência:** Falha de uma source não para a run; falha de matching não para a run

## License

MIT — vê [LICENSE](./LICENSE) para detalhes.

---

**Made with ♪ by Dias**
