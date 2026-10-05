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

## Uso semanal (2 comandos)

```bash
uv run peel            # estado: o que falta avaliar, publicação, próxima execução
uv run peel ouvir      # avalia faixas e depois álbuns; envia tudo no fim
uv run peel publicar   # propõe a edição (love antes de like), confirmas, publica
```

Para consultar:

```bash
uv run peel musicas                     # faixas da semana activa, com avaliação e ✓ site
uv run peel musicas --semanas           # semanas disponíveis
uv run peel musicas --semana 2026-W38   # uma semana concreta
uv run peel musicas --abrir 5           # abre a faixa nº 5 na app do Spotify
uv run peel musicas --playlist          # abre a playlist da semana (activa ou última publicada)
uv run peel musicas --site              # abre a semana em peel.sept.pt
uv run peel albums --open 3             # abre o álbum nº 3
```

A ajuda (`peel --help`) mostra só estes comandos; os internos (`run`, `finalize`,
`site`, `triage`, `feedback`, `status`, `affinity`) continuam a funcionar.

No `ouvir`, Enter = like e os números são atalhos: `1 love · 2 like · 3 meh ·
4 skip · 5 ban` (nos álbuns, `6 unavailable`). `o` abre a faixa ou o álbum actual
no Spotify; `q` pára e guarda o que já avaliaste.

O `publicar` mostra só candidatos `love/like` com a posição original na fila.
Enter aceita a proposta ✓; ou escreve as posições pela ordem pública, por
exemplo `5 13 15 16 20 24 28`. Reedições e álbuns sem link directo não entram.
Antes de escrever no Spotify confirma que o site está limpo e actualizado;
depois finaliza com verificação, valida a build, publica o JSON da semana e
envia o estado. `--dry-run` só mostra a proposta.

Para corrigir depois:

```bash
uv run peel ouvir --rever              # lista as notas da semana; escreves f5 ou a11 para mudar
uv run peel ouvir --rever --semana 2026-W38
uv run peel publicar --refazer         # reabre a edição publicada (proposta = a actual)
uv run peel publicar --refazer --semana 2026-W38
```

No `--rever`, Enter mantém a nota e o comentário actuais. No `--refazer`, Enter
mantém a selecção publicada; itens que deixaram de ser love/like saem com aviso
e as correcções de nome de artista mantêm-se. A republicação reescreve a playlist
pública com verificação e actualiza o site.

## Automated Weekly Run

O projeto corre automaticamente à sexta-feira (17:17 UTC, fora do início da hora para reduzir atrasos do GitHub) via [GitHub Actions](/.github/workflows/weekly.yml).

Cada push para `main` e cada PR executam a suite completa, com e sem cores ANSI,
através de `tests.yml`. A execução semanal usa exactamente esse mesmo gate;
só depois dos dois modos passarem pode alterar a triagem e enviar o digest.
O CI de push/PR usa credenciais fictícias e não corre o pipeline musical.
Validação local não basta: confirmar CI verde no commit publicado antes de
declarar a aplicação pronta ou fazer dispatch de recuperação.

Se os testes ou a execução semanal falharem, um job independente envia para o
Telegram: **«Peel: a execução semanal falhou.»**, seguido do link da execução.
O alerta não depende de instalar o Peel, não inclui logs/secrets e não repete
um envio de resultado incerto. Se o próprio Telegram estiver indisponível,
a falha do alerta fica visível no GitHub; não há garantia de entrega nesse caso.

Para dispatch manual (execução real, com Spotify e Telegram):
```bash
# Na página de Actions do repo, clica em "weekly peel run" → "Run workflow"
```

O estado (tracks vistas, histórico de sources) fica guardado em `data/peel.db` e sincronizado ao repo após cada run.

## Project Structure

```
peel/
├── src/peel/
│   ├── main.py            # Run semanal: fontes → Spotify → triagem → Telegram
│   ├── cli.py             # Comandos: ouvir, publicar, albums, report, sync…
│   ├── sources/           # Fontes editoriais (RSS, scrapers, Bandcamp) e registo
│   ├── albums.py          # Fila de álbuns: consenso, afinidade, filtros de formato
│   ├── publication.py     # Selecção pública congelada e proposta do publicar
│   ├── db.py              # Estado SQLite e snapshots
│   ├── state_sync.py      # Sincronização segura da DB com o GitHub
│   ├── report.py          # Relatórios Markdown/HTML
│   ├── site_export.py     # JSON semanal para o site peel-sept
│   ├── spotify_client.py  # Spotify com verificação por releitura
│   ├── telegram.py        # Digest semanal
│   └── affinity.py, scoring.py, matcher.py, musicbrainz.py, …
├── tests/
├── data/                  # peel.db, relatórios, selecções, reconciliações
├── docs/archive/          # Planos e roadmaps antigos (histórico)
└── .github/workflows/
    ├── ci.yml             # Testes em push/PR, sem entregas externas
    ├── tests.yml          # Gate partilhado: suite completa plain/ANSI
    └── weekly.yml         # Cron + manual dispatch + alerta de falha
```

O que está em aberto vive no [TODO](TODO.md).

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

### Fontes

```bash
uv run peel sources               # por fonte: volume/semana, % love/like, estado (12 semanas)
uv run peel sources --semanas 4   # outra janela
uv run peel sources --detalhe     # tabela técnica de scoring
uv run peel sources --json
```

A ordem do registo conta: quando a triagem enche, ficam de fora as últimas.
Por isso as escolhas curadas — Pitchfork Best New Tracks, Stereogum *5 Best
Songs of the Week*, *Pitchfork Selects*, Gorilla vs Bear, KEXP, NPR, Quietus —
vêm antes dos feeds de notícias de grande volume (Stereogum New Music,
Consequence, Pitchfork News). Consenso conta publicações, não feeds: Stereogum
duas vezes continua a ser uma só voz (`src/peel/sources/families.py`).

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
PEEL_MAX_ALBUMS_TO_REVIEW=7
PEEL_MAX_SOURCE_ITEM_AGE_DAYS=30
```

Só sources `kind = "track"` podem entrar na playlist. Sources `album`, `context`, `podcast`, `scrape` ou `manual_spotify` ficam fora da playlist automática. Items publicados há mais de `PEEL_MAX_SOURCE_ITEM_AGE_DAYS` dias são ignorados quando a source expõe data.

A playlist de triagem é a fila real para ouvir: todas as tracks novas da run entram primeiro. Só se faltarem lugares até ao cap entram tracks pendentes, sem feedback, de runs anteriores. Nos pendentes, consenso mantém prioridade e o score da source já inclui feedback; repetições da mesma source sofrem uma penalização linear suave — não há quotas nem caps. Depois da escrita, o Peel **relê todas as URIs e a ordem no Spotify**. Só depois dessa confirmação guarda os snapshots activo/semanal e envia Telegram (`🆕 nova` / `↻ pendente`). Uma resposta HTTP de sucesso não basta: uma discrepância faz a run falhar, sem anunciar uma fila falsa nem repetir a escrita.

As fontes RSS com janela temporal usam 14 dias de sobreposição. Antes da primeira
run bem-sucedida da fonte, `fetch_source` alarga a janela para 30 dias, sem aumentar
o limite de páginas. A DB deduplica observações já conhecidas. Isto não recupera
artigos que já desapareceram do feed: nesses casos a recuperação pelo arquivo é
explícita e documenta a data editorial e a data real de recolha.

### Album queue

A weekly confirma uma fila privada independente de até 7 álbuns por defeito
(`PEEL_MAX_ALBUMS_TO_REVIEW`, limite configurável ímpar e máximo 19) para ouvir
e avaliar. A ordem é: consenso entre publicações (75% de positivos no histórico,
contra 43% com uma só fonte); dentro do mesmo consenso, artistas que já avaliaste
bem primeiro e os que rejeitaste no fim — a afinidade reordena, nunca exclui.
Reedições, compilações *Various Artists*, retrospectivas `(2016-2019)`, sessões
e remisturas não são álbuns novos; discos ao vivo e bandas sonoras continuam
elegíveis. A primeira
observação de cada `(artista, álbum, source)` é imutável; polling repetido só
actualiza a última observação. Menções editoriais novas e consenso entram antes
de pendentes sem feedback; labels Bandcamp são complementares. Como a página
da editora não traz datas nem segue a ordem de lançamento, o Peel lê a página de
cada edição: pré-vendas ficam de fora, a data de lançamento passa pelo filtro
normal de novidade e edições com menos de 4 faixas são singles — seguem para a
triagem de faixas, nunca para a fila de álbuns. Os artigos `First Take` da Clash são encaminhados separadamente
para a triagem de faixas. CLI, Telegram e relatório local mostram a snapshot
completa. A edição pública Sept tem uma selecção explícita separada, com até sete
álbuns aprovados; não trunca automaticamente os primeiros sete da fila privada.

```bash
uv run peel albums                 # fila activa e links de escuta
uv run peel albums --unrated       # apenas pendentes da fila activa
uv run peel albums --week 2026-W32 # lista uma snapshot histórica explícita
uv run peel albums --week 2026-W32 --open 1  # abre um rank histórico
uv run peel albums feedback        # fila activa; love|like|meh|skip|ban|unavailable
uv run peel albums feedback --week 2026-W32  # avalia a snapshot histórica
uv run peel albums refresh --week 2026-W29 --dry-run
uv run peel albums refresh --week 2026-W29  # reconstrói explicitamente a snapshot
uv run peel site export            # reexporta snapshots sem as recalcular
```

`albums refresh` ou uma recuperação dirigida e documentada podem substituir
explicitamente uma snapshot; uma re-exportação normal apenas lê links e ordem
congelados. Os comandos humanos `albums`, `report` e `finalize`, sem `--week`,
seguem a última fila confirmada no Spotify, mesmo que seja uma semana recuperada
anterior à semana mais recente de descobertas.

```bash
uv run peel triage                 # fila confirmada, na ordem Spotify
uv run peel triage --unrated       # só tracks activas sem avaliação (--pending é alias)
uv run peel feedback               # avalia a fila activa, pela ordem Spotify
uv run peel feedback --history     # backlog histórico explícito
uv run peel feedback --history --week 2026-W28
uv run peel triage feedback        # alias compatível de `peel feedback`
uv run peel triage --open          # abre Spotify
uv run peel triage bootstrap       # uma vez: importa a triagem já existente
uv run peel finalize --selection data/selections/2026-W38.json --dry-run
uv run peel finalize --selection data/selections/2026-W38.json
uv run peel site export --week 2026-W38 --weeks 1 --no-resolve-albums
```

Desde W38, `finalize` exige uma selecção aprovada (`--selection`, exemplo em
`data/selections/2026-W38.json`), ou reutiliza a edição já finalizada. O plano fixa
as URIs e identidades dos álbuns **na ordem escolhida**, e os timestamps das duas
filas ouvidas. Só entram avaliações `love`/`like`, com leitura de feedback por
identidade; faixas pendentes de semanas anteriores são elegíveis sem alterar a
sua data de descoberta. Não há preenchimento automático. Correcções de nomes
para publicação são explícitas no plano, sem reescrever as descobertas.

`--dry-run` valida numa cópia temporária sem sync, migração da DB real, Spotify,
Telegram ou export. Na execução real, só depois de Spotify confirmar todas as
URIs e a ordem são gravados atomicamente o snapshot de faixas e a selecção
pública completa (faixas, álbuns e metadados). O export toca **apenas essa semana**.
As filas privadas e o feedback permanecem intactos. Re-exports usam o snapshot
congelado, não novos rankings nem os primeiros sete álbuns privados. Para alterar
uma edição finalizada é obrigatório um novo plano com `--refresh`.

Sem selecção pública confirmada, semanas desde W38 não são exportadas. O fallback
editorial só permanece para o formato legado anterior. A primeira publicação
com o novo schema exige publicar código e estado juntos; o sync recusa merges
que perderiam a selecção completa numa DB remota ainda sem essa tabela.

A ordem e o número de cada faixa vêm sempre da fila confirmada. `triage --unrated`
e `feedback` preservam o número original, sem renumerar depois de ocultar avaliações.
A auditoria do relatório segue a mesma ordem; descobertas fora da playlist ficam
separadas e sem número. O Telegram numera a lista pela mesma snapshot.

Os comandos humanos escondem logs internos por defeito; para diagnóstico local,
usa `uv run peel --verbose triage` (a weekly mantém logs JSON completos para CI).

Fontes `album` activas incluem Guardian, DIY, Clash, reviews Pitchfork (Best New e regulares sem overlap), The Quietus, Feedbacker/Rock, Aquarium Drunkard e labels Bandcamp. Entram em `Albums / Context`, relatório e Telegram, mas não vão para Spotify matching/playlist. Reissues/arquivo explícitos e itens editoriais antigos são excluídos da fila actual. NPR New Music Friday — The Starting 5 e KEXP — In Our Headphones são fontes `track`: entram no matching/playlist como novidades curadas.

Uma recuperação corrente pode actualizar apenas estas sources e pré-visualizar a fila, sem tracks, playlists ou Telegram:

```bash
uv run peel albums refresh --week 2026-W32 --fetch --dry-run
```

A fila final aceita apenas links directos Spotify/Bandcamp. O feedback usa sempre
a fila activa, excepto quando `--week` escolhe explicitamente uma snapshot histórica;
nunca faz fallback silencioso para outra semana. `unavailable` significa que não
foi possível ouvir e não conta como juízo musical sobre a source.

`tracks_found` é calculado a partir dos dados persistidos: matches + unmatched. O comando também mostra telemetria real de `source_runs` (`Runs`, `Fetched/Fresh`, `Proc`, `Stale/Cap/Err`) para distinguir qualidade de fonte, backlog, caps e falhas.

### Relatório local

O Markdown continua a ser o artefacto canónico e versionado. Depois de avançar
para uma semana nova, relatórios Markdown existentes ficam congelados: consultar
ou abrir uma semana histórica não os reescreve. `--refresh` é a única forma de
substituir deliberadamente esse snapshot histórico.

Para uma leitura mais agradável, `--html` cria uma página autónoma em
`data/reports/.html/`, com a paleta visual do Peel; `--open` gera essa preview e
abre-a no browser. A preview é local e pode sempre ser regenerada.

Desde W37, a secção principal de faixas usa `review_queue_snapshots`: a mesma
ordem, URIs, proveniência e contagem de novas/pendentes do Telegram. As descobertas
brutas ficam separadas em auditoria. Sem snapshot semanal, a geração falha em vez
de substituir a triagem por uma listagem de descobertas. Nas semanas anteriores,
a ausência do snapshot é explicitada; não se inventa uma confirmação retroactiva.

Um reset de escuta é um **arquivo, não uma avaliação negativa**. `listening_resets`
regista o intervalo retirado e a semana de recomeço. Descobertas, feedback e filas
históricas permanecem na DB. O corte de backlog aplica-se às faixas, não funciona
como rejeição de álbuns: um disco não avaliado pode ser recuperado por consenso
editorial novo ou por escolha explícita do utilizador. Relatórios arquivados são
movidos sem alteração para `data/reports/archive/` (previews em `archive/.html/`).
Semanas finalizadas não podem ser arquivadas; semanas arquivadas não são exportadas
para o site. Fazer backup antes de um reset e não reconstruir semanas antigas com
feeds actuais. Uma recuperação regista a nota e a proveniência em `queue_recoveries`,
visíveis no relatório; observações feitas hoje não são retrodatadas. O relatório
original arquivado é mantido separado da edição recuperada.

```bash
uv run peel report --week 2026-W32
uv run peel report --week 2026-W32 --html
uv run peel report --week 2026-W32 --open
uv run peel report --week 2026-W32 --refresh  # substituição histórica explícita
```

### Sync

A weekly corre no GitHub, mas os comandos interactivos usam a DB local. Antes de
`feedback`, `triage`, `albums`, `report`, `finalize` e `site export`, o Peel compara
e sincroniza automaticamente **apenas** `data/peel.db`; alterações de código no
checkout não bloqueiam o estado. Se existirem feedback local e estado remoto novo,
o Peel pára sem sobrescrever nenhum dos dois. Um conflito com uma run/reset local
não publicado também pára: o merge de feedback não pode apagar a nova fila.
Reconciliações explícitas são feitas numa cópia, com backups, validação e decisões
registadas em [`data/reconciliations/`](data/reconciliations/README.md); nunca se
resolvem descartando silenciosamente uma das bases de dados.

```bash
uv run peel sync status # mostra Git e estado canónico separadamente
uv run peel sync pull   # actualiza apenas a DB, com backup atómico
uv run peel sync push   # envia feedback/relatórios e marca a DB sincronizada
```

Para manutenção deliberadamente sem rede:

```bash
uv run peel --offline albums
PEEL_OFFLINE=1 uv run peel report --week 2026-W32
```

`--offline` não torna uma DB antiga correcta: relatórios desde W29 exigem a
snapshot canónica e falham em vez de recalcular uma fila divergente.


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
