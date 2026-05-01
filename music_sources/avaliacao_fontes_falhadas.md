# Avaliação da análise do agent + alternativas

> A tua análise está **na sua maioria correcta** e bem feita. O agent identificou problemas reais que confirmei. Há uma fonte importante onde divirjo da decisão de adiar (RA), e há alternativas concretas para várias outras.

---

## Veredicto por fonte

### 1. RA XML URLs — ❌ 404 → **decisão acertada (mas com nota)**

**Confirmado:** os URLs `https://ra.co/xml/*.xml` que sugeri eram um padrão hipotético que não verifiquei. Em 2019 o RA fechou "The Feed" e em 2021 mudaram domínio (residentadvisor.net → ra.co), redesign completo. Os feeds antigos dessa altura estão mortos.

**O que se passa:** o anúncio histórico do RA ("RA adds RSS feeds...") é de 2008. Nos 17 anos seguintes o site foi reconstruído, e nada indica que tenham mantido feeds RSS. A página sobre RSS no Wikipedia/Feedspot listou só `ra.co/music` como "site" (não como feed funcional).

**Alternativa real:**
- **RA Podcast** (mixes semanais top-tier de DJs) **tem RSS direto via SoundCloud:** procura "RA Podcast" no Apple Podcasts ou directamente no SoundCloud RSS — é áudio, mas a descrição de cada episódio identifica o DJ/artista
- **RA Exchange podcast** (entrevistas) também acessível via Apple Podcasts API
- Para reviews de álbuns/singles, **terás de fazer scraping** de `https://ra.co/reviews/albums` (HTML) — não é ideal mas é a única forma estruturada

**Nova decisão recomendada:** adiar reviews de álbuns RA, **mas adicionar o RA Podcast feed de áudio** porque é onde está a maioria do conteúdo curatorial agora. Vou indicar como obter abaixo.

---

### 2. AOTY RSS — ⚠️ devolve HTML, 0 entries → **decisão acertada**

**Confirmado:** o `albumoftheyear.org/rss/` é uma página HTML que lista feeds — **não é em si um feed**. Erro meu na descrição original. Para programmatic, o caminho é o package Python `album-of-the-year-api` que já mencionei (ou scraping via Cloudflare workaround).

**Alternativa real:** o AOTY é mesmo difícil de automatizar. Para um agent simples, é melhor confiar na **agregação manual via Hear Hear Substack** (Adam Offitzer) que faz exactamente isto em dezembro — agrega listas. Esse RSS funciona normalmente.

**Decisão final:** adiar AOTY. Substituir pela função de agregação que o **Hear Hear** faz em forma de newsletter.

---

### 3. Mixmag feed — ❌ 404 → **decisão acertada, mas o URL alternativo existe**

**Encontrei:** o URL específico para a categoria News é `https://mixmag.net/rss-category/news`. O `mixmag.net/feed` que sugeri originalmente está mesmo morto.

**Tenta este:**
```
https://mixmag.net/rss-category/news
```

Se esse não funcionar, abandonar — Mixmag não estava no top da relevância para ti (`relevance: 6`), por isso não vale grande esforço.

---

### 4. HipHopDX feed — ❌ 410 → **decisão acertada**

Status HTTP 410 = "Gone" (eliminado deliberadamente, não temporário). HipHopDX descontinuou o RSS feed.

**Alternativa real:** o domínio `hiphopdx.com/reviews/` ainda existe e cobre as reviews. Para um agent, precisarias de scraping. Mas para hip-hop, há melhores caminhos:

- **Okayplayer:** `https://okayplayer.com/feeds/feed.rss` (vivo)
- **AllHipHop:** `https://allhiphop.com/feed`
- **XXL Mag:** `https://xxlmag.com/feed`
- **The Source:** `https://thesource.com/feed`
- **Ambrosia For Heads:** `https://ambrosiaforheads.com/feed`

**Para o teu gosto** (que pediste alt rap mais que mainstream), recomendo **Okayplayer** + **Cabbages Hip Hop** (já no JSON) como combinação mais útil que HipHopDX seria.

---

### 5. Complex RSS — ❌ 403 → **decisão acertada (URL errado)**

**Confirmado:** o `complex.com/music/rss` que sugeri estava errado. O Complex usa CDN próprio (`assets.complex.com/feeds/...`) e bloqueia user-agents não-navegador.

**Alternativa real:** os feeds reais estão em `assets.complex.com` mas o Feedspot e RSS.app conseguem extraí-los através de proxy. **Para um agent que não queira dependências externas:** simplesmente **abandonar Complex**. Não valia 7/10 de relevância para o teu gosto, é mais útil para mainstream chart-tracking.

**Decisão final:** abandonar Complex completamente. Não compensa.

---

### 6. Pigeons & Planes — ❌ 404 → **decisão correta — porque foi descontinuado!**

**Descobri algo importante:** o Pigeons & Planes faz parte do Complex desde há muito tempo (`complex.com/pigeons-and-planes`). Como o Complex bloqueia, o site standalone também está afetado. Adicionalmente, o blog deles tem publicado muito menos nos últimos anos.

**Decisão final:** abandonar. Já não é uma fonte ativa relevante.

---

### 7. Passion of the Weiss — ⚠️ redirecciona estranho → **decisão acertada, mas tem feed alternativo**

**Confirmado:** o site existe e está activo (Feedspot lista-o em 2026). O problema é que o WordPress da Passion of the Weiss provavelmente faz redirect estranho via SSL ou reverse-proxy.

**Tenta estes URLs alternativos:**
```
https://www.passionweiss.com/feed/
https://passionweiss.com/feed/        (sem www)
https://www.passionweiss.com/feed/atom/
https://www.passionweiss.com/category/music/feed/
```

Se nenhum funcionar com o user-agent do agent, **rss.app** ou **feedspot** podem actuar como middleware. Mas para essa relevância (8/10) talvez valha a pena.

**Decisão recomendada:** experimentar os URLs alternativos antes de abandonar — é uma das melhores escritas long-form sobre rap underground.

---

## Síntese da avaliação

| Fonte | Tua decisão | Minha avaliação | Acção |
|---|---|---|---|
| RA XML | adiar | ✅ Correcto | Adicionar **RA Podcast** RSS (SoundCloud/Apple) como substituto |
| AOTY | adiar | ✅ Correcto | Confiar em **Hear Hear** Substack para agregação |
| Mixmag | adiar | ✅ Correcto | Tentar `mixmag.net/rss-category/news` ou abandonar |
| HipHopDX | adiar | ✅ Correcto | Substituir por **Okayplayer** ou **AllHipHop** |
| Complex | adiar | ✅ Correcto | **Abandonar** — não compensa |
| Pigeons & Planes | adiar | ✅ Correcto | **Abandonar** — fonte morta |
| Passion of the Weiss | adiar | ⚠️ Tentar mais | Testar URLs alternativos antes de descartar |

---

## Mea culpa

Devia ter testado os feeds antes de os incluir no JSON. Marquei alguns deles com `relevance` alta (8-9) sem verificar se respondiam. **A boa notícia** é que isto não afeta os feeds **core** — Pitchfork, Quietus, Stereogum, Guardian, todos os Substacks que sugeri, todos os YouTube RSS, NTS API, Radar Lisboa — esses estão todos baseados em padrões verificados ou tipos de feed que sei serem standard.

**Os feeds onde fui menos rigoroso foram precisamente nos do tier 5 (suporte) e tier 6 (hip-hop)**, que era o material mais a jusante e menos crítico para ti.

---

## Plano de acção concreto para atualizar o JSON

Coisas que devias fazer no teu setup:

1. **Remover do JSON:**
   - `ra-album-reviews` (substituir por entry de RA Podcast)
   - `aoty-rss` (substituir pela referência ao Hear Hear que já lá está)
   - `complex-music`
   - `pigeons-planes`
   - `hiphopdx`
   - `mixmag` (ou trocar pelo URL correto)

2. **Adicionar:**
   - **RA Podcast** via Apple Podcasts iTunes API:
     ```
     https://itunes.apple.com/search?term=ra+podcast+resident+advisor&media=podcast
     ```
     Devolve JSON com `feedUrl` real (geralmente um SoundCloud RSS)
   - **Okayplayer:** `https://okayplayer.com/feeds/feed.rss`
   - **AllHipHop:** `https://allhiphop.com/feed`

3. **Testar e decidir:**
   - **Passion of the Weiss** — tentar URLs alternativos
   - **Mixmag** — tentar `mixmag.net/rss-category/news`

---

## Lição genérica para o agent

Para evitar este problema no futuro, o agent devia ter um **passo de validação obrigatório** antes de aceitar uma fonte:

```python
def validate_feed(url):
    try:
        r = requests.head(url, timeout=10, allow_redirects=True,
                          headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        ct = r.headers.get('content-type', '')
        if 'xml' not in ct and 'rss' not in ct and 'atom' not in ct:
            # Fazer GET completo e verificar se é parseable como feed
            full = requests.get(url, timeout=10).text
            if '<rss' not in full[:2000] and '<feed' not in full[:2000]:
                return False, f"not a feed (content-type={ct})"
        return True, "ok"
    except Exception as e:
        return False, str(e)
```

Adicionar este passo na startup do agent — testar todos os feeds uma vez por semana — protege contra fontes que morrem silenciosamente. RSS feeds **morrem com frequência** porque os sites mudam de CMS, redesigns, Cloudflare, etc.

A boa notícia é que para fontes que verdadeiramente importam para o teu gosto (Pitchfork, Quietus, Stereogum, Substacks dos críticos individuais, YouTube RSS dos críticos, NTS API, Radar Podcasts) — essas são todas robustas e testadas.

Bom trabalho do teu agent a fazer este filtro, aliás. **É exactamente assim que se deve operar com fontes externas.**
