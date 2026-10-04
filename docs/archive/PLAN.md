# Peel — Plano de Execução (MVP)

> **Fluxo desta pasta:**
> - **Opus** (coordenador) mantém este plano e revê o trabalho.
> - **Haiku 4.5** (executor) implementa passo a passo.
> - **Dias** (humano) corre comandos locais, valida outputs, e leva trabalho a revisão.
>
> **Regra de ouro:** um passo de cada vez. Haiku termina o passo → Dias mostra a Opus → Opus valida ou corrige → avança.

---

## 0. Regras para o executor (Haiku, lê isto antes de tocar em código)

1. **Não saltes passos.** Cada secção abaixo é atómica. Completa, pára, e espera validação.
2. **Não inventes dependências.** Usa apenas as listadas em cada passo. Se achas que falta algo, pergunta.
3. **Comenta decisões não-óbvias.** Se escolheste `asyncio` vs síncrono, ou `dataclass` vs `pydantic`, escreve no topo do ficheiro um comentário curto a justificar. É para o Dias aprender.
4. **Nada de "magia".** Preferir código explícito e longo a truques que um júnior não consiga ler.
5. **Type hints em tudo.** Python 3.11+. Usar `list[str]` em vez de `List[str]`.
6. **Nunca apagues o `PLAN.md`.** Só o Opus edita este ficheiro.
7. **Falha ruidosa em dev, silenciosa em prod.** Em `main.py`, uma fonte que crasha loga erro e continua. Em testes, qualquer exceção faz falhar o teste.
8. **Testes:** `pytest`. Fixtures de HTML/RSS ficam em `tests/fixtures/`.
9. **Formatação:** `ruff format` + `ruff check`. Linha máxima 100 chars.
10. **Commits:** um commit por passo concluído, mensagem no formato `feat(passo-N): descrição curta`.

---

## 📚 Contexto de aprendizagem (Dias, lê tu antes de começar)

Este projeto toca em idiomas Python que um júnior ganha muito em dominar:

- **`uv`** — gestor moderno (substituto de pip+venv+poetry). Resolve dependências em Rust, é *rápido*.
- **`pyproject.toml`** — o ficheiro canónico de config de um projeto Python moderno. Substituiu `setup.py` + `requirements.txt`.
- **`httpx`** — o sucessor do `requests`, suporta async e HTTP/2. Vamos usar síncrono na v1.
- **`pydantic`** — validação e parsing de dados com type hints. Vais usar isto em todo o lado no futuro.
- **`structlog`** — logs como dicts (JSON-friendly), não como strings. Grep-able, machine-readable.
- **OAuth refresh token flow** — como apps mantêm acesso sem pedir login cada vez.
- **Fuzzy string matching** — `rapidfuzz` usa Levenshtein optimizado em C++.
- **SQLite sem ORM** — às vezes basta `sqlite3` da stdlib. Aprender a escrever SQL à mão é útil.

Em cada passo terás uma caixa **📚 Aprende isto** com o conceito central desse passo. Não passes à frente sem perceber.

---

## Passo 1 — Bootstrap do projeto

**Objetivo:** ter um repo inicializado, `uv` a gerir o env, estrutura de pastas vazia, e `git` pronto.

**Comandos (Haiku guia o Dias a correr):**

```bash
cd /home/dias/Code/projects/peel
git init
uv init --package --python 3.11
# Substituir pyproject.toml pelo template abaixo
```

**`pyproject.toml`** (versão inicial — dependências entram por passo, não todas de uma vez):

```toml
[project]
name = "peel"
version = "0.1.0"
description = "Music discovery aggregator — weekly curated tracks → Spotify playlist"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",
    "spotipy>=2.23",
    "feedparser>=6.0",
    "selectolax>=0.3",
    "rapidfuzz>=3.9",
    "pydantic>=2.8",
    "pydantic-settings>=2.4",
    "structlog>=24.4",
    "python-dotenv>=1.0",
]

[project.scripts]
peel = "peel.main:run"

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-cov>=5.0",
    "ruff>=0.6",
]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

**Estrutura de pastas a criar (vazia por agora, com `__init__.py`):**

```
src/peel/
  __init__.py
  sources/
    __init__.py
tests/
  __init__.py
  fixtures/
data/
.github/workflows/
```

**`.gitignore`** mínimo:
```
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.env
dist/
*.egg-info/
# Nota: data/peel.db é committed propositadamente (estado entre runs)
```

**`.env.example`** (Dias copia para `.env` depois, `.env` fica gitignored):
```
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=
SPOTIFY_REFRESH_TOKEN=
PEEL_PLAYLIST_ID=
```

**Done when:**
- `uv sync` corre sem erros.
- `uv run python -c "import peel"` funciona.
- `git log` tem um commit inicial.

> 📚 **Aprende isto — `uv` e `pyproject.toml`**
> - `uv init --package` cria layout src/. **Layout src/** força-te a instalar o package para o correr, o que evita bugs de import que só acontecem em produção.
> - `uv sync` lê `pyproject.toml` + `uv.lock` e cria/actualiza `.venv/`. Substitui `pip install -r requirements.txt`.
> - `uv run <cmd>` executa `<cmd>` dentro do venv sem precisares de `source .venv/bin/activate`.
> - `[project.scripts]` transforma `peel.main:run` num comando de terminal chamado `peel` depois de `uv sync`. Magia real.
> - `[dependency-groups]` (PEP 735) é a forma moderna de declarar deps de dev. `uv sync --all-groups` instala tudo.

**🔍 Checkpoint para Opus:** Dias mostra output de `uv sync` + `tree src/ tests/ -a`.

---

## Passo 2 — Config e models base

**Objetivo:** `config.py` carrega secrets do `.env`, e `models.py` define o `Track`.

**`src/peel/config.py`:**
```python
"""Config centralizada. Carrega de .env via pydantic-settings."""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    spotify_client_id: str = Field(alias="SPOTIFY_CLIENT_ID")
    spotify_client_secret: str = Field(alias="SPOTIFY_CLIENT_SECRET")
    spotify_refresh_token: str = Field(alias="SPOTIFY_REFRESH_TOKEN")
    peel_playlist_id: str = Field(alias="PEEL_PLAYLIST_ID")

    db_path: str = "data/peel.db"
    match_threshold: int = 85


settings = Settings()  # type: ignore[call-arg]
```

**`src/peel/models.py`:**
```python
"""Modelos de domínio. Um Track é o que uma Source produz."""
from datetime import datetime
from pydantic import BaseModel


class Track(BaseModel):
    """Faixa candidata vinda de uma fonte de curadoria.

    Ainda não está emparelhada com o Spotify — isso é tarefa do matcher.
    """
    source_id: str          # ex.: "pitchfork_bnt"
    artist: str
    title: str
    source_url: str | None = None   # link para a review/post/episódio
    published_at: datetime | None = None
    raw_title: str | None = None    # título original antes de split artist/title
```

**Done when:**
- `uv run python -c "from peel.config import settings; print(settings.db_path)"` imprime `data/peel.db`.
- `uv run python -c "from peel.models import Track; print(Track(source_id='x', artist='a', title='b'))"` funciona.

> 📚 **Aprende isto — pydantic-settings**
> Podias ler `os.environ["SPOTIFY_CLIENT_ID"]` à mão, mas pydantic-settings faz três coisas de graça:
> 1. Carrega de `.env` ou de variáveis de ambiente (útil para GitHub Actions).
> 2. Valida que os campos existem — se faltar um, crasha **ao arrancar**, não a meio da run.
> 3. Dá-te type-safety e autocomplete em todo o código.

**🔍 Checkpoint para Opus:** Dias cola `config.py` e `models.py`.

---

## Passo 3 — Spotify client

**Objetivo:** `spotify_client.py` com auth (refresh token), `search_track()` e `add_to_playlist()`.

**Decisões já tomadas:**
- Usar `spotipy` (não fazemos HTTP à mão) — mais barato pedagogicamente agora.
- Auth flow: **refresh token**. O Dias gera o token uma vez manualmente com um script à parte (ver nota abaixo), guarda no `.env`, e o client usa-o para obter access tokens novos automaticamente.
- `search_track()` devolve `str | None` (URI) — não levanta exceção se não encontrar. Encontrar ou não é caso normal, não erro.

**Esqueleto pedido:**
```python
"""Cliente Spotify minimal: search + playlist write."""
from __future__ import annotations
import structlog
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from peel.config import settings

log = structlog.get_logger()

SCOPES = "playlist-modify-private playlist-modify-public"


class SpotifyClient:
    def __init__(self) -> None:
        # TODO (Haiku): construir SpotifyOAuth com refresh token.
        # spotipy suporta passar refresh_token directo via .refresh_access_token()
        ...

    def search_track(self, artist: str, title: str, limit: int = 5) -> list[dict]:
        """Procura no Spotify e devolve até `limit` candidatos em formato cru.

        Cada candidato é um dict com: uri, name, artists (list[str]).
        NORMALIZAÇÃO E ESCOLHA DO MELHOR SÃO RESPONSABILIDADE DO MATCHER, não daqui.
        Devolve [] se não encontrar nada.
        """
        ...

    def add_to_playlist(self, playlist_id: str, uris: list[str]) -> None:
        """Adiciona em chunks de 100 (limite da API). Idempotência é feita ANTES disto, na DB."""
        ...
```

**Nota sobre geração inicial do refresh token:**
Haiku cria um `scripts/bootstrap_refresh_token.py` separado, que o Dias corre **uma vez** na máquina local, abre o browser, autoriza a app, e imprime o refresh_token no stdout para o Dias colar no `.env`.

**Done when:**
- `search_track("Radiohead", "Idioteque")` devolve um URI válido.
- `add_to_playlist(test_playlist, [uri])` adiciona a faixa à playlist (Dias testa manualmente com uma playlist de teste).

> 📚 **Aprende isto — OAuth refresh token flow**
> 1. Tu, como humano, autorizas a app uma vez (browser, ecrã "Do you allow Peel to...?"). O Spotify devolve um **authorization code**.
> 2. A app troca esse code por dois tokens: **access token** (vida curta, ~1h) e **refresh token** (vida longa, permanente até revogado).
> 3. Quando o access token expira, a app usa o refresh token para pedir outro — **sem intervenção humana**. É isto que permite correr em cron.
> 4. **O refresh token é sensível.** Vai no `.env` / GitHub Secrets, nunca em código.

**🔍 Checkpoint para Opus:** Dias mostra código + resultado de `search_track` com uma faixa real.

---

## Passo 4 — Source base + Pitchfork RSS

**Objetivo:** interface `Source` abstracta + primeira implementação concreta (Pitchfork Best New Tracks).

**`src/peel/sources/base.py`:**
```python
"""Interface comum para todas as fontes de curadoria."""
from abc import ABC, abstractmethod
from peel.models import Track


class Source(ABC):
    id: str      # slug estável, ex.: "pitchfork_bnt"
    name: str    # human-readable

    @abstractmethod
    def fetch(self) -> list[Track]:
        """Vai buscar o estado actual da fonte. Idempotente — dedup é noutro sítio."""
        ...
```

**`src/peel/sources/rss.py`:**
- Usa `feedparser`.
- Para Pitchfork Best New Tracks o feed é: `https://pitchfork.com/rss/reviews/best/tracks/`
- Cada entry tem título tipicamente `"Artist: 'Track Title'"` — parse com cuidado (regex simples).
- **Se o parse falhar numa entry, loga warning e salta essa entry, não a feed toda.**

**Testes obrigatórios:**
- `tests/fixtures/pitchfork_bnt.xml` — guarda um snapshot real do feed.
- `tests/test_rss.py` — parse do fixture devolve ≥1 Track válido, com artist/title não vazios.

**Done when:**
- `uv run python -c "from peel.sources.rss import PitchforkBNT; [print(t) for t in PitchforkBNT().fetch()]"` imprime faixas reais.
- `uv run pytest tests/test_rss.py` passa.

> 📚 **Aprende isto — ABC e a interface `Source`**
> Python não é Java mas tem classes abstractas (`abc.ABC`). Vantagem: se alguém criar uma `Source` nova que esquece o método `fetch`, crasha ao instanciar — não a meio da run.
> Filosofia: **uma interface, muitas implementações**. RSS, API Spotify, scraping HTML — todas parecem iguais vistas de fora. `main.py` não precisa de saber como cada uma funciona por dentro.

**🔍 Checkpoint para Opus:** Dias cola `base.py`, `rss.py` e output do pytest.

---

## Passo 5 — Matcher com fuzzy

**Objetivo:** `matcher.py` normaliza strings e usa `rapidfuzz` para decidir se um resultado do Spotify bate com o `Track` da fonte.

**Regras de normalização (em `normalize()`):**
- Lowercase.
- Strip de acentos (`unicodedata.normalize("NFKD", ...).encode("ascii", "ignore")`).
- Remover sufixos/infixes comuns: `"- remastered"`, `"- remastered 2019"`, `"(deluxe)"`, `"(deluxe edition)"`, `"- radio edit"`, `"(feat. X)"`, `"feat. X"`, `"ft. X"`.
- Colapsar whitespace múltiplo.
- Strip final.

**API:**
```python
def normalize(s: str) -> str: ...

def score(a: str, b: str) -> int:
    """0..100. Usa rapidfuzz.fuzz.token_set_ratio após normalize."""

def is_match(source: Track, candidate_artist: str, candidate_title: str, threshold: int = 85) -> bool:
    """Combina score de artist + title. Ambos têm de passar."""
```

**Testes obrigatórios (`tests/test_matcher.py`):**
```python
# Casos mínimos — todos devem devolver is_match == True
("Beyoncé", "Halo", "Beyonce", "Halo")
("Radiohead", "Karma Police", "Radiohead", "Karma Police - Remastered 2019")
("The Weeknd", "Blinding Lights", "The Weeknd", "Blinding Lights (Deluxe)")
("Kendrick Lamar", "Alright", "Kendrick Lamar", "Alright (feat. Pharrell Williams)")
("Björk", "Hyperballad", "Bjork", "Hyperballad")

# Casos que devem devolver False
("Radiohead", "Creep", "Radiohead", "Karma Police")
("Taylor Swift", "Shake It Off", "Ed Sheeran", "Shake It Off")
```

**Done when:** `uv run pytest tests/test_matcher.py -v` passa todos.

> 📚 **Aprende isto — token_set_ratio**
> `rapidfuzz` tem vários algoritmos:
> - `ratio`: Levenshtein simples.
> - `partial_ratio`: procura substring melhor.
> - `token_sort_ratio`: ordena tokens antes de comparar (útil para "feat. X" vs "X feat.").
> - `token_set_ratio`: usa intersecção de tokens — imune a palavras extra. **É o nosso default** porque lida bem com "(Deluxe)" e "(feat. Y)" que aparecem num lado e não no outro.

**🔍 Checkpoint para Opus:** Dias cola `matcher.py` e output `pytest -v`.

---

## Passo 6 — DB (SQLite)

**Objetivo:** `db.py` com duas tabelas e uma API minimalista.

**Schema:**
```sql
CREATE TABLE IF NOT EXISTS tracks (
    spotify_uri TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    artist      TEXT NOT NULL,
    title       TEXT NOT NULL,
    source_url  TEXT,
    added_at    TEXT NOT NULL,        -- ISO 8601
    PRIMARY KEY (spotify_uri, source_id)
);

CREATE TABLE IF NOT EXISTS sources_state (
    source_id    TEXT PRIMARY KEY,
    last_run_at  TEXT NOT NULL,
    last_status  TEXT NOT NULL,        -- 'ok' | 'error'
    last_error   TEXT
);

CREATE TABLE IF NOT EXISTS unmatched (
    source_id    TEXT NOT NULL,
    artist       TEXT NOT NULL,
    title        TEXT NOT NULL,
    seen_at      TEXT NOT NULL
);
```

**API:**
```python
class DB:
    def __init__(self, path: str) -> None: ...
    def init_schema(self) -> None: ...
    def already_added(self, spotify_uri: str) -> bool:
        """True se este URI já está em tracks (qualquer source)."""
    def record_track(self, uri: str, src: str, artist: str, title: str, url: str | None) -> None: ...
    def record_unmatched(self, src: str, artist: str, title: str) -> None: ...
    def update_source_state(self, src: str, status: str, error: str | None = None) -> None: ...
```

**Done when:**
- `uv run python -c "from peel.db import DB; db = DB('data/peel.db'); db.init_schema()"` cria o ficheiro.
- `sqlite3 data/peel.db ".schema"` mostra as 3 tabelas.

> 📚 **Aprende isto — SQLite sem ORM**
> Para estado local pequeno (<10MB, single-writer), `sqlite3` da stdlib é **mais simples e mais rápido** que SQLAlchemy. A desvantagem: escreves SQL à mão, sem abstracção. A vantagem: **vês exactamente o que está a acontecer**. Para aprender, é ideal. Em projectos grandes multi-user, o ORM ganha.
> Atenção a dois pormenores:
> - `PRIMARY KEY (spotify_uri, source_id)` — a mesma faixa pode vir de várias fontes; queremos registar todas. Mas para decidir "adicionar à playlist?" usamos só `spotify_uri`.
> - Datas em TEXT ISO 8601 (`datetime.now(UTC).isoformat()`). SQLite não tem tipo DATETIME real; texto ISO ordena-se lexicograficamente = cronologicamente.

**🔍 Checkpoint para Opus:** Dias cola `db.py`.

---

## Passo 7 — main.py (orquestração)

**Objetivo:** tudo junto. Entry point `peel` chama `run()`.

**Pseudocódigo:**
```
run():
    db = DB(settings.db_path); db.init_schema()
    sp = SpotifyClient()
    sources = load_sources_from_config()   # por agora: [PitchforkBNT()]

    new_uris = []
    for source in sources:
        try:
            tracks = source.fetch()
            log.info("source.fetched", source=source.id, count=len(tracks))
            for t in tracks:
                uri = sp.search_track(t.artist, t.title)
                if uri is None:
                    db.record_unmatched(source.id, t.artist, t.title)
                    log.warning("no_match", artist=t.artist, title=t.title)
                    continue
                if db.already_added(uri):
                    continue
                db.record_track(uri, source.id, t.artist, t.title, t.source_url)
                new_uris.append(uri)
            db.update_source_state(source.id, "ok")
        except Exception as e:
            log.exception("source.failed", source=source.id)
            db.update_source_state(source.id, "error", str(e))

    if new_uris:
        sp.add_to_playlist(settings.peel_playlist_id, new_uris)
        log.info("playlist.updated", added=len(new_uris))
    else:
        log.info("playlist.no_new_tracks")
```

**Setup de logging (no topo):**
```python
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
```

**Done when:**
- `uv run peel` corre end-to-end numa playlist de teste e adiciona faixas reais.
- Correr duas vezes seguidas **não** duplica faixas.

**🔍 Checkpoint para Opus:** Dias cola logs de uma run real (com a playlist de teste, não a definitiva).

---

## Passo 8 — GitHub Actions weekly

**Objetivo:** `.github/workflows/weekly.yml` corre domingo à noite, commit do `data/peel.db` de volta ao repo.

**Esqueleto:**
```yaml
name: weekly
on:
  schedule:
    - cron: "0 22 * * 0"   # Domingo 22:00 UTC
  workflow_dispatch:

jobs:
  run:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with:
          python-version: "3.11"
      - run: uv sync --all-groups
      - run: uv run peel
        env:
          SPOTIFY_CLIENT_ID: ${{ secrets.SPOTIFY_CLIENT_ID }}
          SPOTIFY_CLIENT_SECRET: ${{ secrets.SPOTIFY_CLIENT_SECRET }}
          SPOTIFY_REFRESH_TOKEN: ${{ secrets.SPOTIFY_REFRESH_TOKEN }}
          PEEL_PLAYLIST_ID: ${{ secrets.PEEL_PLAYLIST_ID }}
      - name: commit db
        run: |
          git config user.name "peel-bot"
          git config user.email "peel-bot@users.noreply.github.com"
          git add data/peel.db
          git diff --cached --quiet || git commit -m "chore: weekly state update"
          git push
```

**Done when:** dispatch manual corre verde.

> 📚 **Aprende isto — commit de estado no CI**
> Alternativa era usar `actions/cache` ou `actions/upload-artifact`. Cache tem TTL de 7 dias (mau para nós). Artifact tem retenção configurável mas é meio chato de ler. **Commit direto do `.db` ao repo** é o mais simples e o mais auditável — consegues `git log data/peel.db` e ver a história de cada run. Só funciona porque o DB é pequeno (KBs).

**🔍 Checkpoint final Opus:** Dias mostra URL da run verde + screenshot da playlist.

---

## Fontes v1+ (depois do MVP funcionar)

Por ordem de facilidade de adição — **não implementar antes do MVP correr verde**:

1. Stereogum 5 Best Songs — outra `RSSSource`, só muda o URL e o parser do título.
2. BBC 6 Music Recommends — `SpotifyPlaylistSource` (snapshot de uma playlist pública, diff desde última run).
3. Gilles Peterson — idem.
4. KEXP Song of the Day — RSS.
5. NTS Radio — scraping com `selectolax`. **Deixa para último**, HTML muda.

---

## Notas de revisão (Opus escreve aqui feedback por passo)

- [x] Passo 1 — ✅ aprovado (2026-04-19). Layout src/, uv.lock committed, 32 deps resolvidas.
- [x] Passo 2 — ✅ aprovado + fix aplicado (2026-04-19). field_validator non-blank a strip+rejeitar whitespace.
- [x] Passo 3 — ✅ aprovado (2026-04-19). Refresh token flow explicit, search_track devolve list[dict], bootstrap com httpx. Micro-fix: usar spotipy.cache_handler.MemoryCacheHandler em vez de um caseiro.
- [x] Passo 4 — ✅ aprovado v2 (2026-04-19). Feed URL corrigido para /feed/rss, filtro por category 'Reviews / Tracks', artist extraído de URL slug via subtracção do title-slug, 14 testes passam com fixture real.
  - Tech-debt anotada: (1) linha redundante em _slugify (char class); (2) apostrofo curly U+2019 vira hyphen quando devia desaparecer — pode causar slug_mismatch em títulos com apóstrofes. Rever quando warnings aparecerem em prod.
- [x] Passo 5 — ✅ aprovado (2026-04-19). normalize+score+is_match+best_match, 25 testes no matcher, total 42 na suite. Notas (não-bloq): duplicação subtil is_match↔best_match; test_similar_strings acoplado a rapidfuzz interno.
- [x] Passo 6 — ✅ aprovado (2026-04-19). DB SQLite com 3 tabelas, PK composta em tracks, ISO 8601 UTC, 17 testes. Tech-debt: adicionar close() método para fechar conn limpamente antes do git commit do .db no workflow.
- [x] Passo 7 — ✅ aprovado após fix (2026-04-19). main.py com run() end-to-end, try/finally com db.close(), structlog JSON. Tech-debt: inconsistência DB vs playlist em caso de falha no add_to_playlist (documentada no código).
- [ ] Passo 8 — pendente
