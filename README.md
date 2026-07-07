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

### Playlist safety caps

```bash
PEEL_MAX_TRACKS_PER_SOURCE=8
PEEL_MAX_TRACKS_PER_RUN=40
PEEL_MAX_SOURCE_ITEM_AGE_DAYS=30
```

Só sources `kind = "track"` podem entrar na playlist. Sources `album`, `context`, `podcast`, `scrape` ou `manual_spotify` ficam fora da playlist automática. Items publicados há mais de `PEEL_MAX_SOURCE_ITEM_AGE_DAYS` dias são ignorados quando a source expõe data.

Fontes `album` activas incluem Guardian album reviews e The Quietus album reviews. Entram em `Albums / Context`, relatório e Telegram, mas não vão para Spotify matching/playlist. NPR New Music Friday — The Starting 5 é fonte `track`: entra no matching/playlist como cinco novidades semanais.

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

- [ ] Mais fontes: BBC 6 Music Recommends, KEXP, NTS Radio scraping
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
