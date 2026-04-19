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

4. **Cria a playlist alvo:**
   - No Spotify, cria uma playlist privada chamada "Peel"
   - Copia o ID da playlist (vê na URL: `spotify.com/playlist/{ID}`) para .env como PEEL_PLAYLIST_ID

5. **Testa localmente:**
   ```bash
   uv run pytest          # Valida todo o código
   uv run peel            # Executa uma run completa
   ```

## Automated Weekly Run

O projeto corre automaticamente toda a segunda-feira (domingo 22:00 UTC) via [GitHub Actions](/.github/workflows/weekly.yml).

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
