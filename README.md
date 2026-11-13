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

No `ouvir`, a escala é igual para faixas e álbuns:

| Tecla | Rating | Significado | Valor |
|---|---|---|---:|
| 1 | Love | Adorei; quero voltar a ouvir | +2 |
| 2 | Like | Gostei | +1 |
| 3 | Meh | Não me disse grande coisa; neutro | 0 |
| 4 | Dislike | Não gostei | −1 |
| 5 | Hate | Detestei | −2 |

`Enter`, `s` ou `skip` passa ao seguinte **sem gravar uma avaliação**; o item
continua pendente. `u` marca indisponível (sem juízo musical). `b`/`ban` bloqueia
só o item, pede confirmação e não conta como opinião sobre o artista ou a fonte;
ao rever, substitui a nota desse item. **Hate não bloqueia.** `o` abre a faixa
ou o álbum; `q` pára e guarda o que já avaliaste. O Spotify aberto toca cada item
automaticamente (`--sem-tocar` desliga).

Podes retomar uma semana antiga por ordem:
`uv run peel ouvir --semana 2026-W43`. Salta o que já tem nota, sem alterar a
playlist activa.

As notas antigas não são convertidas: `skip` histórico continua a significar −1
e os antigos `ban` continuam a bloquear. Os bloqueios e `unavailable` ficam fora
das médias musicais; `meh` deixa de penalizar a afinidade. As comparações entre
semanas devem ter em conta que o Dias afinou a distinção Love/Like durante o uso.

O `publicar` mostra só candidatos `love/like` com a posição original na fila.
`--album-extras` permite acrescentar explicitamente álbuns avaliados fora dessa
fila; aparecem identificados como `Outside queue`, sem alterar a fila privada.
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

No `--rever`, Enter não grava nada: mantém a nota, o comentário e a data actuais.
Para mudar apenas o comentário, escolhe explicitamente a mesma nota. No `--refazer`, Enter
mantém a selecção publicada; itens que deixaram de ser love/like saem com aviso
e as correcções de nome de artista mantêm-se. A republicação reescreve a playlist
pública com verificação e actualiza o site.

### Publication extras

Para incluir álbuns ouvidos no radar ou fora da fila semanal, fornece um JSON
com, no máximo, sete candidatos explícitos. Não há pesquisa nem inclusão
automática de todos os álbuns avaliados. Exemplo de ficheiro:

```json
[
  {
    "artist": "PISS",
    "album": "You Made Me / Skin on Linen",
    "listen_url": "https://open.spotify.com/album/4L3aQdOW11hKFGRbpkw3wN",
    "listen_kind": "spotify"
  }
]
```

```bash
uv run peel publicar --album-extras /tmp/album-extras.json --dry-run
uv run peel publicar --album-extras /tmp/album-extras.json
```

Só aparecem candidatos com Love/Like atual, sem títulos de arquivo/compilação e
com link direto Spotify/Bandcamp. Os números extra são códigos de escolha da
interface, **não posições inventadas na fila**. A seleção combinada mantém o
máximo de sete álbuns, sem enchimento, pela ordem confirmada pelo Dias. Um álbum
já na fila não pode voltar a entrar como extra, nem se aceitam aliases ou links
duplicados. O ficheiro não pode declarar proveniência interna: essa informação
é congelada a partir das menções locais; sem menções, o crédito é `Personal pick`,
com zero famílias, não um voto adicional ADM.

Antes de escrever no Spotify, os **novos extras** passam pela validação de
lançamento/formato Spotify/Bandcamp (35 dias, sem pré-venda/arquivo identificado),
com confirmação de identidade para Spotify. A pré-visualização não faz esses
pedidos: avisa que a verificação final está pendente e abre SQLite só de leitura,
sem auto-sync, schema/migrações ou serviços. Os links externos são explícitos,
não resultados de pesquisa aproximada.

A seleção pública guarda identidades, ordem, links, proveniência e a origem
`outside_queue`, sem inserir álbuns nas filas históricas. Reexportar uma edição
continua a usar essa seleção congelada, mesmo se entretanto houver novas opiniões
ou fontes. `publicar --refazer` recupera também os extras publicados sem exigir o
ficheiro original; volta a excluir o que já não for Love/Like. Um extra publicado
e mantido com a mesma identidade/link não é requalificado como novidade meses
depois; extras novos ou com novo link voltam a ser verificados. Continuam a ser
necessárias a escolha explícita e a confirmação de publicação.

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
e avaliar. A ordem é: consenso entre publicações; dentro do mesmo consenso,
artistas que já avaliaste bem primeiro e os que rejeitaste no fim — a afinidade
reordena, nunca exclui. O consenso é um sinal editorial, não uma garantia de gosto.
A diversidade só desempata qualidades próximas: o ajuste por repetição de uma
publicação é limitado a menos de 0,05 pontos, sem quotas nem limites por fonte.

Variações de iniciais como `Fontaines D.C.` / `Fontaines DC` partilham consenso e
avaliação, preservando os nomes e as linhas guardadas. Uma revisão explícita usa
a avaliação existente; se houver avaliações antigas em ambas as variantes,
prevalece a mais recente sem apagar a outra. As filas históricas não são refeitas.

Reedições, compilações *Various Artists*, retrospectivas `(2016-2019)`, sessões
e remisturas não são álbuns novos; discos ao vivo e bandas sonoras continuam
elegíveis. A primeira
observação de cada `(artista, álbum, source)` é imutável; polling repetido só
actualiza a última observação. Menções editoriais novas e consenso entram antes
de pendentes sem feedback; labels Bandcamp são complementares. O leitor combina
os cartões visíveis da editora com a continuação `data-client-items`, sem duplicar
lançamentos. Inspeciona no máximo 20 candidatos por editora para devolver até cinco
edições elegíveis: arquivos, falhas, datas antigas e pré-vendas não preenchem esse
limite de cinco. Edições com menos de quatro faixas são singles/EPs curtos: seguem
para a triagem de faixas, nunca para a fila de álbuns. Um refresh só de álbuns não
regista esses singles como discos. Os artigos `First Take` da Clash são encaminhados separadamente
para a triagem de faixas. CLI, Telegram e relatório local mostram a snapshot
completa. A edição pública Sept tem uma selecção explícita separada, com até sete
álbuns aprovados; não trunca automaticamente os primeiros sete da fila privada.

### ADM radar (read-only)

```bash
uv run peel radar                 # datas recentes + presença/feedback local
uv run peel radar --all           # inclui antigos, futuros, arquivo e dados incertos
uv run peel radar --json          # inclui publicações/links originais indexados
uv run peel radar --no-local      # consulta ADM sem ler SQLite
uv run peel radar --limit 20 --snapshot /tmp/peel-adm-$(date -u +%Y%m%dT%H%M%SZ).json
```

Consulta **Just In e Unlisted**, sem mínimo de cinco críticas. As notas ADM são
contexto editorial, nunca previsão de gosto, ranking pessoal ou um voto/família
adicional. `Known` significa que o álbum já consta do Peel, não que o Dias conhece
o artista. A comparação usa as opiniões atuais retidas, sem as alterar ou enviar
ao site. Não há auto-sync, inicialização/migração de SQLite, Spotify, Telegram,
exportação do site, integração semanal nem agendamento. Por defeito não grava
ficheiros; `--snapshot` cria apenas o JSON explicitamente pedido, datado por
`observed_at`, sem substituir existentes nem escrever no estado canónico.

A janela de **35 dias** é descritiva sobre as datas UK/US indexadas pelo ADM:
prevalece a mais antiga conhecida; `Added` não substitui lançamento. Formato,
primeira edição/gravação e disponibilidade para ouvir **não ficam verificados**.
Os artigos originais não são consultados; datas de críticas ausentes permanecem
desconhecidas. O índice atual não é reconstituído como se existisse numa sexta
anterior. `--json` e a snapshot incluem também os excluídos e os erros.

Lê apenas as duas listagens atuais, não um catálogo exaustivo. Consulta até 30
fichas por defeito (`--limit`: 1–50), intercalando as listagens e deduplicando
URLs/identidades; não são quotas de seleção musical. Cada URL admite no máximo
três GETs, incluindo redirecionamentos restritos ao ADM. Para após HTTP 429 ou
três falhas consecutivas nas fichas e assinala resultados parciais. Estado local
inacessível fica desconhecido, não passa a «álbum novo». A opção global
`--offline` refere-se ao sync do estado, não impede esta consulta web explícita.

Primeira observação e limites: [análise de 10/11/2026](docs/analysis/2026-11-10-adm-radar.md).

### Release admission

Novas admissões exigem uma edição já lançada há, no máximo, **35 dias**. A data da
notícia, recolha ou adição a uma playlist não substitui a data de lançamento.
O filtro editorial de 30 dias continua separado e pode ser mais restritivo no
Bandcamp. Os metadados da pesquisa Spotify validam faixas sem pedidos adicionais;
uma edição antiga com o mesmo ISRC, quando devolvida na pesquisa, impede que uma
compilação recente faça a mesma gravação passar por novidade. Uma faixa bónus
nova não é excluída só por estar num álbum deluxe.

Álbuns são verificados no link de escuta Spotify/Bandcamp: sem pré-vendas,
compilação identificada ou edição de arquivo. Um single/EP Spotify de 1–3 faixas
não ocupa uma vaga de álbum; um lançamento explicitamente classificado como
álbum pelo Spotify não é excluído apenas por ter poucas faixas longas. No
Bandcamp, que não distingue esse formato no URL, mantém-se o mínimo de quatro
faixas para a fila de álbuns. Proveniência
explícita de uma coleção de material/gravações inéditas «from their vault/archive»
na abertura da descrição Bandcamp também identifica arquivo. Referências a
samples de arquivo ou a discos antigos na biografia não bastam. Datas
imprecisas só são aceites se todo o intervalo possível couber na janela. Sem
data/formato confirmado, a nova admissão fica de fora — não é um Dislike nem Ban.
Faixas futuras ou com data por confirmar ficam no mecanismo de retry existente,
sem ocupar vagas ou gerar votos; não se perdem só porque a notícia entretanto
desapareceu do feed. São guardadas em `unmatched`, cujo total continua a participar
no sinal de disponibilidade do scoring existente — não é uma rejeição musical.
Falhas/ambiguidade na pesquisa do artista continuam conservadoras e não excluem
um lançamento de outra forma elegível. Datas de edição não detetam todas as
reedições silenciosas: estes controlos não garantem identificar a primeira
publicação de cada gravação.

Não se apagam pendentes, opiniões ou filas históricas. Antes de preencher uma
nova fila, os pendentes também passam pela validação de lançamento; antigos ou
não confirmados ficam guardados, mas não servem para encher vagas. Essas consultas
são limitadas a uma pool de `max(14, 2 × vagas livres)` e não se fazem quando as
novidades já preenchem a fila. A triagem mantém a separação entre novidades e
pendentes; abrir/exportar uma snapshot não a refaz.
`albums refresh --dry-run` continua a ser uma **pré-visualização parcial**, sem
validar os metadados de todos os links Spotify/Bandcamp. `--fetch` pode confirmar
alguns dados na recolha das fontes, mas não equivale à validação final de todos
os candidatos; o comando avisa explicitamente.

```bash
uv run peel albums                 # fila activa e links de escuta
uv run peel albums --unrated       # apenas pendentes da fila activa
uv run peel albums --week 2026-W32 # lista uma snapshot histórica explícita
uv run peel albums --week 2026-W32 --open 1  # abre um rank histórico
uv run peel albums feedback        # fila activa; love|like|meh|dislike|hate; controlos s/u/b
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
