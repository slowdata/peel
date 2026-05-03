# Peel v2 Roadmap — CLI-first backoffice, consenso e feedback

> Estado em 2026-05-02: v2 CLI-first funcional e sincronizada.
> Sprints 0–6 e 8 concluídos; fixes de segurança de playlist, freshness, Guardian albums,
> Quietus como álbum/contexto e links externos para unmatched também concluídos.
>
> Regra-mãe continua válida: implementar em fatias pequenas. Cada sprint deve deixar o projecto
> funcional, testado e fácil de rever.

---

## Estado operacional — 2026-05-02

### Feito

- CLI Typer/Rich como backoffice principal:
  - `peel run`
  - `peel status`
  - `peel tracks`
  - `peel tracks --sources`
  - `peel feedback`
  - `peel report`
  - `peel sources`
  - `peel doctor`
  - `peel doctor sources`
  - `peel sync status|pull|push`
- Source attribution e consenso preservados: a mesma URI pode ter várias fontes.
- Feedback local explícito (`love|like|meh|skip|ban`) guardado em SQLite.
- Relatórios semanais Markdown em `data/reports/YYYY-Www.md`.
- Sync local/GitHub para DB e relatórios.
- Source scoring inicial com `peel sources --weeks N --min-tracks N --json`.
- Validação de registry com `peel doctor sources`.
- Caps de segurança:
  - `PEEL_MAX_TRACKS_PER_SOURCE`
  - `PEEL_MAX_TRACKS_PER_RUN`
  - `PEEL_MAX_SOURCE_ITEM_AGE_DAYS`
- `source_runs` criado para histórico real por fonte/run.
- Guardian Music entra como fonte de álbuns/contexto, não playlist directa.
- The Quietus entra como `album`, reduzindo unmatched falso de reviews.
- `unmatched.source_url` preserva links externos para ouvir fora do Spotify.
- Telegram digest mostra tracks, álbuns e “escutas externas”.

### Última validação conhecida

```bash
uv run pytest      # 212 passed
uv run ruff check  # OK
```

### Estado Git conhecido

- `main` sincronizado com `origin/main`.
- Últimos commits relevantes:
  - `7257a9f feat(external): preserve unmatched source links`
  - `389824f chore: update peel local feedback/state`
- Nota: push SSH pode falhar se a chave não estiver carregada; workaround usado com `gh`/HTTPS.

---

## 0. Protocolo para o agent

### Antes de mexer

1. Ler este ficheiro completo.
2. Correr:
   ```bash
   git status --short
   git diff --stat
   uv run pytest
   uv run ruff check
   ```
3. Não apagar nem reformatar trabalho existente sem explicar.
4. Não fazer commit sem autorização explícita do Dias/Opus.
5. Implementar **apenas o sprint pedido**.

### Regras de código

- Python 3.11+.
- Type hints em tudo.
- Código simples, explícito e pedagógico.
- SQLite stdlib, sem ORM.
- Testes com `pytest`.
- Formatação/lint:
  ```bash
  uv run ruff format
  uv run ruff check
  uv run pytest
  ```
- Fixtures reais em `tests/fixtures/` quando a tarefa envolve fontes externas.
- Nunca commitar secrets, `.env`, tokens ou IDs sensíveis.

### Estado actual importante

Antes de começar trabalho novo, o agent deve mostrar sempre:

```bash
git status --short
git diff --stat
```

O estado versionável intencional inclui:

- `data/peel.db`
- `data/reports/`

Não adicionar secrets/tokens/user IDs sensíveis à DB. Se `data/peel.db` estiver modificado,
confirmar se é estado local esperado antes de o commitar.

---

## 1. Visão v2

Peel deixa de ser apenas um cron que adiciona músicas ao Spotify e passa a ser um
**backoffice pessoal de descoberta musical**, com três superfícies:

1. **Playlist Spotify** — output final de escuta.
2. **SQLite DB** — memória, histórico, consenso e feedback.
3. **CLI bonita** — backoffice local para consultar, avaliar e sincronizar.

A web app fica adiada. A CLI é o backoffice v2.

---

## 2. Decisões já tomadas

### Cron

Usar uma run semanal ao sábado de manhã:

```yaml
- cron: "0 10 * * 6" # sábado 10:00 UTC
```

Motivo:

- Sexta é dia global de lançamentos.
- Críticos/blogs publicam ao longo da sexta, muitas vezes em horário EUA.
- Sábado de manhã apanha melhor o material sem esperar até domingo.
- O sistema é idempotente; se mais tarde precisarmos, adicionamos segunda run domingo.

### Source attribution / consenso

Registar sempre a fonte, mesmo quando a URI já existe globalmente.

Padrão desejado:

```python
already = db.already_added(uri)

inserted = db.record_track(
    uri,
    source.id,
    track.artist,
    track.title,
    track.source_url,
)

if already:
    # não adiciona outra vez à playlist, mas a source ficou registada
    continue

# só faixas globalmente novas entram na playlist
```

Isto permite:

- playlist sem duplicados;
- DB com múltiplas fontes por faixa;
- contagem de consenso;
- scoring futuro das fontes.

### Feedback

Não depender de play count Spotify. É ruidoso e limitado.

Usar feedback explícito do Dias:

- `love` = 2
- `like` = 1
- `meh` = 0
- `skip` = -1
- `ban` = -2 opcional/futuro

### Backoffice

Começar com CLI:

```bash
uv run peel run
uv run peel status
uv run peel tracks
uv run peel tracks --sources
uv run peel feedback
uv run peel report
uv run peel sources
uv run peel doctor
uv run peel sync pull
uv run peel sync push
```

---

## 3. Sprint 0 — estabilizar baseline

### Objectivo

Não começar v2 em cima de estado ambíguo.

### Tarefas

- [ ] Mostrar `git status --short`.
- [ ] Mostrar `git diff --stat`.
- [ ] Correr `uv run pytest`.
- [ ] Correr `uv run ruff check`.
- [ ] Identificar alterações já existentes que pertencem a trabalho anterior:
  - rotação de playlist;
  - retry de unmatched;
  - albums;
  - Telegram;
  - alterações no Spotify client;
  - `music_sources/`.
- [ ] Propor separação em commits ou stash.

### Critério de aprovação

Opus/Dias confirma que o baseline está limpo ou conscientemente aceite.

---

## 4. Sprint 1 — CLI base com Typer + Rich

### Objectivo

Criar uma CLI bonita, extensível e útil, sem ainda fazer scoring avançado.

### Dependências

Adicionar a `pyproject.toml`:

```toml
typer = ">=0.12"
rich = ">=13.7"
```

### Ficheiros esperados

- `src/peel/cli.py`
- `tests/test_cli.py`
- ajuste em `pyproject.toml`:
  ```toml
  [project.scripts]
  peel = "peel.cli:app"
  ```

### Comandos mínimos

```bash
uv run peel --help
uv run peel run
uv run peel status
uv run peel tracks
uv run peel tracks --sources
uv run peel doctor
```

### Comportamento

#### `peel run`

Chama `peel.main.run()`.

#### `peel status`

Mostra com Rich:

- caminho da DB;
- se DB existe;
- total de tracks;
- total de sources;
- total unmatched;
- última run por source (`sources_state`).

#### `peel tracks`

Tabela com músicas recentes:

- artista;
- título;
- semana;
- nº fontes;
- rating se existir;
- primeira/última data.

#### `peel tracks --sources`

Além da tabela, mostra sources por música.

Exemplo desejado:

```text
Floating Points — Falling To Earth  🔥 2 fontes
  - Stereogum — https://...
  - Guardian — https://...
```

#### `peel doctor`

Valida:

- `.env` encontrado;
- secrets presentes sem imprimir valores;
- DB abre;
- tabelas existem;
- entrada `peel` está correcta.

### Testes obrigatórios

- `peel --help` não falha.
- `peel status` com DB temporária não falha.
- `peel tracks` com DB temporária mostra dados inseridos.
- `peel run` pode ser mockado para verificar que chama `main.run()`.

### Critério de aprovação

- CLI funciona localmente.
- Rich output legível.
- Testes passam.
- Não há regressões no cron: workflow deve passar a usar `uv run peel run`.

---

## 5. Sprint 2 — source attribution e consenso

### Objectivo

Corrigir a perda de informação quando a mesma música aparece em múltiplas fontes.

### DB/API desejada

Actualizar ou adicionar métodos em `DB`:

```python
def record_track(...) -> bool:
    """Retorna True se inseriu nova linha (uri, source_id), False se já existia."""


def track_sources(self, spotify_uri: str) -> list[tuple[str, str | None]]:
    """Lista (source_id, source_url) de uma URI."""


def recent_tracks_with_sources(self, limit: int = 50) -> list[...]:
    """Dados agregados para CLI/report."""
```

A tabela `tracks` já tem a PK correcta:

```sql
PRIMARY KEY (spotify_uri, source_id)
```

### Alteração no pipeline

Em `main.py`, substituir:

```python
if db.already_added(uri):
    continue

db.record_track(...)
```

por:

```python
already = db.already_added(uri)
db.record_track(...)

if already:
    continue
```

### Testes obrigatórios

- Mesma URI de duas fontes cria 2 rows em `tracks`.
- `already_added(uri)` continua global.
- Playlist recebe só 1 URI para a mesma música.
- `track_sources(uri)` devolve as duas fontes.
- `peel tracks --sources` mostra contagem 2.

### Critério de aprovação

Conseguimos ver consenso por música na CLI e no DB.

---

## 6. Sprint 3 — feedback local

### Objectivo

Permitir ao Dias avaliar músicas para calibrar fontes.

### Schema

Adicionar tabela:

```sql
CREATE TABLE IF NOT EXISTS feedback (
    spotify_uri TEXT PRIMARY KEY,
    rating      INTEGER NOT NULL,
    label       TEXT NOT NULL,
    comment     TEXT,
    rated_at    TEXT NOT NULL
)
```

Mapeamento:

| Label | Rating |
|---|---:|
| love | 2 |
| like | 1 |
| meh | 0 |
| skip | -1 |
| ban | -2 |

### DB/API desejada

```python
def upsert_feedback(self, spotify_uri: str, label: str, comment: str | None = None) -> None: ...
def feedback_for_track(self, spotify_uri: str) -> tuple[int, str, str | None] | None: ...
def unrated_tracks(self, week: str | None = None, limit: int = 50) -> list[...]: ...
```

### CLI

```bash
uv run peel feedback
uv run peel feedback --week 2026-W18
uv run peel feedback --uri spotify:track:... --rating love --comment "grande baixo"
```

Modo interactivo com Rich Prompt:

```text
1/12 Floating Points — Falling To Earth
Sources: Stereogum, Guardian
Rating [love/like/meh/skip/q]? like
Comment optional? bom groove
```

### Testes obrigatórios

- Upsert altera rating existente.
- Labels inválidas falham com erro claro.
- CLI non-interactive grava feedback.
- `tracks` mostra rating quando existe.

### Critério de aprovação

O Dias consegue avaliar músicas sem mexer directamente na DB.

---

## 7. Sprint 4 — relatório semanal Markdown

### Objectivo

Criar output legível e versionável da semana.

### Ficheiros

- `src/peel/report.py`
- `tests/test_report.py`
- outputs em:
  ```text
  data/reports/YYYY-Www.md
  ```

### Conteúdo mínimo

```md
# Peel 2026-W18

## Tracks

- Artist — Title — 2 fontes — rating: like
  - Spotify: spotify:track:...
  - Sources:
    - Pitchfork — https://...
    - Guardian — https://...

## Albums / Context

- Artist — Album
  - Source: Pitchfork BNA — https://...

## Unmatched

- Artist — Title — source

## Source summary

| Source | Tracks | New | Consensus | Unmatched | Avg rating |
```

### CLI

```bash
uv run peel report
uv run peel report --week 2026-W18
uv run peel report --open
```

### Workflow

Actualizar GitHub Actions para commitar também:

```bash
git add data/peel.db data/reports/
```

### Testes obrigatórios

- Report gera Markdown estável.
- Sources são agrupadas por URI.
- Ratings aparecem se existirem.

### Critério de aprovação

O relatório semanal substitui a necessidade imediata de web app.

---

## 8. Sprint 5 — sync local/GitHub

### Objectivo

Resolver sincronização entre cron no GitHub e feedback local do Dias.

### CLI

```bash
uv run peel sync status
uv run peel sync pull
uv run peel sync push
```

### Comportamento

#### `sync status`

Mostra:

- branch actual;
- working tree clean/dirty;
- commits ahead/behind;
- se `data/peel.db` mudou.

#### `sync pull`

Executa:

```bash
git pull --ff-only
```

Se working tree dirty, aborta com mensagem clara.

#### `sync push`

Executa:

```bash
git add data/peel.db data/reports/
git diff --cached --quiet || git commit -m "chore: update peel local feedback/state"
git push
```

Nunca deve fazer force push.

### Testes

Usar mocks de `subprocess.run`; não executar git real nos testes.

### Critério de aprovação

Fluxo diário possível:

```bash
uv run peel sync pull
uv run peel feedback
uv run peel sync push
```

---

## 9. Sprint 6 — source scoring

### Objectivo

Descobrir quais fontes realmente funcionam para o gosto do Dias.

### Métricas desejadas

Por fonte e por janela temporal:

- `tracks_found`
- `tracks_matched`
- `new_unique_tracks`
- `duplicate_mentions`
- `consensus_hits`
- `unmatched_count`
- `liked_count`
- `skipped_count`
- `avg_rating`
- `score`

### Nota importante

Algumas métricas não existem hoje na DB. Pode ser necessário criar:

```sql
source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    run_at TEXT NOT NULL,
    fetched_count INTEGER NOT NULL,
    matched_count INTEGER NOT NULL,
    new_unique_count INTEGER NOT NULL,
    duplicate_count INTEGER NOT NULL,
    unmatched_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT
)
```

### CLI

```bash
uv run peel sources
uv run peel sources --weeks 4
```

Tabela Rich:

```text
Source       Found  Matched  New  Consensus  Avg rating  Score
Pitchfork       20       18    8          3        1.2   86
Guardian        10        7    5          4        1.5   91
```

### Fórmula inicial simples

```text
score =
  10 * avg_rating
  + 3 * consensus_hits
  + 2 * new_unique_tracks
  - 2 * skipped_count
  - 1 * unmatched_count
```

A fórmula é provisória. O objectivo inicial é observar, não optimizar.

---

## 10. Sprint 7 — novas fontes

### Regra obrigatória

Nenhuma fonte entra sem validação e fixture real.

Para cada fonte nova:

- [ ] validar URL com `peel doctor sources` ou script equivalente;
- [ ] guardar fixture real em `tests/fixtures/`;
- [ ] parser dedicado ou genérico bem testado;
- [ ] source_id estável;
- [ ] decidir `kind`: `track`, `album`, `context`, `podcast`, `video`;
- [ ] teste com pelo menos 2 exemplos reais.

### Fontes recomendadas por ordem

#### 1. Guardian Music

- URL: `https://www.theguardian.com/music/rss`
- Status testado: OK.
- Valor: coluna “Add to playlist” + crítica mainstream UK boa.
- Tipo: `track` e `album`, dependendo do parser.
- Prioridade: alta.

#### 2. Radar Lisboa

- URL correcto RSS: `https://radarlisboa.fm/category/semana/feed/`
- Status testado: OK.
- Valor: match muito bom com gosto do Dias e contexto PT.
- Tipo: sobretudo `context`/`album`; talvez track extraction parcial.
- Prioridade: alta.

#### 3. Pitchfork Best New Albums

- URL correcto: `https://pitchfork.com/feed/reviews/best/albums/rss`
- Status testado: OK.
- Tipo: `album`/`context`, não playlist directa.
- Prioridade: alta.

#### 4. Bandcamp Daily

- URL: `https://daily.bandcamp.com/feed`
- Status testado: OK.
- Tipo: `context`/`album`.
- Prioridade: média-alta.

#### 5. First Floor / Joe Muggs / Simon Reynolds

- First Floor: `https://firstfloor.substack.com/feed` — OK.
- Joe Muggs: `https://joemuggs.substack.com/feed` — OK.
- Simon Reynolds: `https://blissout.blogspot.com/feeds/posts/default` — OK.
- Tipo: `context`.
- Prioridade: média.

#### 6. Fantano / Indiecast / podcasts críticos

- Fantano YouTube RSS: OK.
- Indiecast podcast RSS: OK.
- All Songs Considered: OK.
- Pitchfork Review Podcast: OK.
- Tipo: `context`/consenso, não playlist directa.
- Prioridade: média.

#### 7. RA Podcast

- URL: `https://ra.co/xml/podcast.xml`
- Status testado: OK.
- Tipo: `context` electrónico/DJ.
- Prioridade: média.

### Fontes a não adicionar ainda

| Fonte | Motivo |
|---|---|
| RA reviews XML | 404 |
| AOTY RSS | HTML, não feed real |
| Complex RSS | 403/bloqueio |
| Pigeons & Planes | morto/404 |
| HipHopDX RSS | 410 |
| Passion of the Weiss | redirect estranho para domínio errado |
| KEXP SOTD antigo | 404 |
| Dead End Hip Hop channel_id | errado/404 |
| Christgau Substack actual | 404 |

---

## 11. Sprint 8 — doctor sources

### Objectivo

Ferramenta para validar fontes antes de as meter em produção.

### CLI

```bash
uv run peel doctor sources
uv run peel doctor sources --json
```

### Output

Tabela:

```text
Source               Type          HTTP  Entries  OK  Note
Guardian Music       rss           200       26   ✅
RA Reviews           rss           404        0   ❌  dead feed
AOTY                 rss           200        0   ❌  HTML, not feed
```

### Implementação

Pode ler uma registry Python primeiro. Mais tarde pode ler JSON.

Validação mínima:

- GET com timeout 10s;
- User-Agent explícito;
- follow redirects;
- parse com feedparser se RSS/Atom;
- para API JSON, validar JSON parseável;
- não crashar se uma fonte falhar.

### Testes

Mock de httpx/feedparser ou fixture local. Não depender de internet nos testes.

---

## 12. Ordem recomendada de execução

### Concluído

- [x] **Sprint 0** — estabilizar baseline.
- [x] **Sprint 1** — CLI base.
- [x] **Sprint 2** — attribution/consenso.
- [x] **Sprint 3** — feedback.
- [x] **Sprint 4** — report semanal.
- [x] **Sprint 5** — sync.
- [x] **Sprint 6** — source scoring.
- [x] **Sprint 8** — doctor sources.
- [x] Caps/freshness: limites por fonte/run e filtro temporal.
- [x] Guardian Music como álbum/contexto.
- [x] The Quietus como álbum/contexto.
- [x] Unmatched com `source_url` e escutas externas no Telegram/report.

### Próximo trabalho recomendado

1. **Sprint 7 incremental** — adicionar novas fontes uma a uma, começando por Bandcamp Daily como
   `context`/external inbox, não playlist directa.
2. Rever `source_runs` após algumas semanas de histórico e decidir se `peel sources` deve usar
   `fetched_count`, `fresh_count`, `processed_count`, `skipped_stale` e `skipped_cap`.
3. Melhorar scoring com estado “insufficient data” para fontes com poucas tracks.
4. Só depois considerar matcher mais agressivo para casos Spotify difíceis.

---

## 13. Próximo pedido recomendado para agente pequeno

Usar tarefas fechadas, sem expandir scope:

```text
Lê ROADMAP_V2.md e TODO.md.

Implementa apenas Bandcamp Daily como fonte context/external, sem adicionar tracks à playlist.

Requisitos:
1. Validar URL/feed.
2. Criar fixture real em tests/fixtures/.
3. Criar parser com kind="context" ou kind="album" conforme os dados reais.
4. Garantir que nada desta fonte entra em Spotify matching/playlist.
5. Garantir que aparece em report/Telegram como contexto/escuta externa quando houver URL útil.
6. Adicionar testes.
7. Correr uv run ruff format, uv run ruff check e uv run pytest.
8. Não fazer commit sem mostrar diff.

Depois pára.
```

Prompt alternativo de manutenção:

```text
Lê ROADMAP_V2.md e TODO.md.

Não implementes features. Apenas revê source_runs e propõe como integrar histórico real no
peel sources/scoring. Mostra plano e queries SQLite necessárias. Não alteres ficheiros.
```
