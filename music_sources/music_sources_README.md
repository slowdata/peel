# Fontes Musicais para um Agent — Guia Completo

> **Como usar este documento:** o ficheiro companheiro `music_sources_for_agent.json` contém a estrutura de dados completa pronta a ser consumida por um agent. Este `.md` explica o que está lá e como o agent deve usar.

---

## Estrutura por tipo de fonte

Há **4 tipos técnicos** de fonte:

| Tipo | O que é | Como consumir | Exemplos |
|---|---|---|---|
| **`rss`** | Feed RSS/Atom standard | Parser RSS qualquer (feedparser em Python, etc.) | Pitchfork, Stereogum, Guardian |
| **`api_json`** | API REST JSON pública | HTTP GET, parse JSON | NTS Radio (`/api/v2/live`) |
| **`scrape`** | Sem feed nativo, precisa scraping | BeautifulSoup/playwright + selector | NTS Latest, BBC 6 Music, AOTY |
| **`podcast_rss`** | Feed RSS de podcast | Mesmo que RSS, mas com `<enclosure>` áudio | KEXP, Radar Podcasts |

---

## TIER 1 — Críticos essenciais (todos com RSS) ⭐

Se o agent só consumir um tier, é este. Todas as fontes aqui têm **RSS direto** e **autoridade crítica máxima**.

### Pitchfork — 4 feeds disponíveis
```
https://pitchfork.com/feed/feed-best-albums/rss     ← Best New Albums (PRIORIDADE MÁXIMA)
https://pitchfork.com/feed/feed-album-reviews/rss   ← Todas as reviews
https://pitchfork.com/feed/feed-best-tracks/rss     ← Best New Tracks
https://pitchfork.com/feed/features/lists-guides/rss ← Listas e guias (essencial em Dezembro)
```
**Cobertura:** indie, post-punk, eletrónica, experimental, alt-rap, art-pop. **Match com gosto:** 10/10.

### The Quietus
```
https://thequietus.com/feed/    (verificar; tentar alt: /reviews/feed/)
```
**Cobertura:** o lado mais experimental e ensaístico. Colunas dedicadas: Album of the Week, Reissue of the Week, Metal, Psych Rock, Punk, Rum Music (avant), Cassettes, New Weird Britain, Electronic, International. **Match:** 10/10 para o lado post-punk e experimental.

### Stereogum — 3 feeds
```
https://www.stereogum.com/feed/                              ← Geral
https://www.stereogum.com/category/reviews/feed/             ← Reviews
https://www.stereogum.com/category/reviews/album-of-the-week/feed/ ← AOTW (alta sinal/ruído)
```
**Cobertura:** indie americano, menos pretensioso que Pitchfork. **Match:** 8/10.

### Resident Advisor
RA confirmou ter feeds RSS para News, Features, Album Reviews e Single Reviews. URL pattern a testar:
```
https://ra.co/xml/reviews-albums.xml
https://ra.co/xml/reviews-singles.xml
https://ra.co/xml/news.xml
https://ra.co/xml/features.xml
```
**Cobertura:** electrónica séria (techno, house, ambient, experimental). **Match:** 9/10 para o lado Caribou/Maribou State.

### The Guardian
```
https://www.theguardian.com/music/rss             ← Música geral
https://www.theguardian.com/music/musicblog/rss   ← Music Blog (descoberta)
```
**Cobertura:** crítica mainstream UK de qualidade (Alexis Petridis). **Match:** 9/10.

### NME
```
https://www.nme.com/feed
```
**Cobertura:** novas bandas britânicas. **Match:** 7/10 (bom para o lane Fat Dog).

---

## TIER 2 — Rádios (curadoria humana, mistura de tipos)

### NTS Radio — Tem API JSON pública não documentada ⭐
```
GET https://www.nts.live/api/v2/live
```

Retorna JSON com:
```json
{
  "results": [
    {
      "channel_name": "1",
      "now": {
        "broadcast_title": "...",
        "start_timestamp": "...",
        "embeds": {
          "details": {
            "name": "...",
            "description": "...",
            "genres": [],
            "location_long": "Berlin"
          }
        }
      },
      "next": { ... }
    }
  ]
}
```

**Repos GitHub úteis:**
- `mcmillan/nts-api` — API wrapper
- `tiktuk/NTS-Now-Playing-Example` — exemplo simples

**Outras URLs NTS para scrape:**
- `https://www.nts.live/latest` — novos shows (diário)
- `https://www.nts.live/radio/collections` — coleções temáticas
- `https://www.nts.live/infinite-mixtapes` — streams 24/7 (Slow Focus para post-punk, Poolside para balearic, Memory Lane para industrial/minimal wave)

### BBC Radio 6 Music
Não há RSS público para shows. Mas:
- **Schedule scraping:** `https://www.bbc.co.uk/schedules/p00fzl65`
- **Programme metadata:** `https://www.bbc.co.uk/programmes/{programme_id}.json` (público)
- **BBC Sounds áudio:** geo-locked a UK (precisa VPN)
- **Spotify mirror não-oficial Lauren Laverne:** `https://open.spotify.com/playlist/0VoVrAxmwv6MQADBE8Z6Bk`

**Programas-chave a seguir (2025/26):**
- Nick Grimshaw — Breakfast Show
- Lauren Laverne — Mid-morning (regressou em 2025)
- Mary Anne Hobbs — novo show 2025 (eletrónica experimental + ambient)
- Steve Lamacq — indie/post-punk
- Iggy Pop — Sábado à noite
- Gilles Peterson — jazz/groove

### Radar 97.8 FM Lisboa ⭐ MATCH PERFEITO
```
https://radarlisboa.fm/category/semana/    ← scrape semanal de picks
https://radarlisboa.fm/feed/               ← tentar RSS WordPress padrão
https://radarpodcasts.podbean.com/feed.xml ← RSS dos podcasts
https://open.spotify.com/playlist/7mrZmkFWM7RSpDCmAPQThD ← Spotify mirror (1486 tracks)
```

**Match perfeito:** picks recentes incluem Baxter Dury, Nation of Language, Nick Cave — tudo no neighborhood da playlist.

### KEXP (Seattle)
```
https://feeds.megaphone.fm/kexp                                   ← Song of the Day (diário)
https://www.omnycontent.com/d/playlist/.../podcast.rss            ← In Our Headphones
https://www.youtube.com/feeds/videos.xml?channel_id=UC2bw5W-3JMzN0M4EzfyHcTA  ← YouTube live sessions
```

---

## TIER 3 — Agregadores (consenso crítico)

### Album of the Year (AOTY) ⭐ AGREGADOR CHAVE
```
https://www.albumoftheyear.org/rss/   ← RSS limitado nativo
```
**Para programmatic access:** package Python `album-of-the-year-api` no PyPI (web-scraping wrapper). **Cuidado:** tem proteção Cloudflare, scrape direto bloqueado. AOTY agrega scores de 50+ publicações — **a melhor forma de detectar consenso crítico**.

### Metacritic
Sem RSS nativo. Scrapers da comunidade existem (`github.com/claytono/metacritic-rss`).
```
https://www.metacritic.com/browse/albums/score/metascore/year
```

### AnyDecentMusic
```
https://www.anydecentmusic.com/   ← scrape only
```
Agregador focado UK. Útil como segundo voto contra AOTY.

---

## TIER 4 — Newsletters Substack (todos com RSS standard)

Substack tornou-se a casa do jornalismo musical sério em 2025, à medida que media tradicionais encolhem.

```
https://firstfloor.substack.com/feed         ← First Floor (Shawn Reynaldo) — eletrónica long-form
https://hearhear.substack.com/feed           ← Hear Hear (Adam Offitzer) — agrega year-end lists
https://www.cabbageshiphop.com/rss/          ← Cabbages — alt rap underground
https://indiescientist.substack.com/feed     ← Indie Scientist — post-punk/indie
https://recentanddecent.substack.com/feed    ← Recent & Decent — playlist semanal
https://naturalmusic.substack.com/feed       ← Natural Music — multi-género com gosto
https://www.honest-broker.com/feed           ← Ted Gioia — análise cultural
```

Padrão Substack: qualquer URL `{nome}.substack.com/feed` é o RSS feed.

---

## TIER 5 — Suporte (RSS direto)

```
https://www.brooklynvegan.com/feed/                ← Brooklyn Vegan
https://feeds.feedburner.com/TheFader              ← The FADER
https://feeds.feedburner.com/thelineofbestfit      ← Line of Best Fit
https://www.factmag.com/feed/                      ← FACT Magazine (eletrónica)
https://mixmag.net/feed                            ← Mixmag (dance)
https://daily.bandcamp.com/feed                    ← Bandcamp Daily ⭐ (deep dives mensais)
https://drownedinsound.com/feed                    ← Drowned in Sound
```

---

## TIER 6 — Hip-hop específico

Para o pedido de Drake-tier mainstream + alt rap:

```
https://hiphopdx.com/feed                  ← HipHopDX
https://www.complex.com/music/rss          ← Complex
https://pigeonsandplanes.com/feed          ← Pigeons & Planes (discovery)
https://www.passionweiss.com/feed/         ← Passion of the Weiss (literária)
```

---

## Workflow recomendado para o agent

### Fluxo semanal típico:

```
1. STARTUP (todos os dias)
   - Fetch todos os feeds com fetch_priority="high"
   - Cache 6h
   - NTS API: poll a cada 30min para now-playing relevante

2. FILTER (por item)
   - Match contra user_taste_profile.examples_from_user
   - Boost se artista mencionado
   - Boost se label mencionado (Ninja Tune, Domino, Warp, etc.)

3. SCORE
   relevance = source.relevance * artist_match_boost * recency_decay
   onde artist_match_boost = 1.5 se nome do user_taste aparece, 1 senão

4. CONSENSUS
   - Strong buy: mencionado em 3+ Tier 1 sources em 7 dias
   - Investigate: 1 Tier 1 + 2 Tier 2-4
   - Skip: única menção

5. WEEKLY DIGEST (sextas)
   - Pitchfork Best New Music dessa semana
   - Stereogum Album of the Week
   - Quietus Album of the Week
   - RA Album Review (1-2 por semana)
   - Radar Observatório
   - Bandcamp Daily destaques

6. DECEMBER MODE
   - Switch para agregação year-end
   - Pull AOTY + todas as "best of" lists
   - Cross-reference para consenso
```

### Anti-patterns a evitar:

- ❌ Surface **todas** as reviews Pitchfork — filtrar por **Best New Music** ou score 7.8+
- ❌ Confiar em mention apenas de agregador sem ver fonte primária
- ❌ Confiar só em Spotify algorithm playlists — espelham só o que já gostas
- ❌ Fazer requests muito frequentes a NTS API — 30min é razoável

### Filtros de qualidade:

**Rejeitar** se géneros são apenas: country, christian, mainstream-pop-only, EDM-festival.
**Boost** se géneros incluem: post-punk, indie-electronic, alternative-rap, ambient, krautrock, balearic.
**Match perfeito** se mencionar artistas no neighborhood Cut Copy / Caribou / Khruangbin (eletrónica-melódica crossover).

---

## Resumo visual de scores

| Fonte | Tipo | Match | Autoridade | Frequência |
|---|---|---|---|---|
| Pitchfork BNM | rss | 10 | 10 | semanal |
| The Quietus | rss | 10 | 9 | diário |
| Resident Advisor | rss | 9 | 10 | semanal |
| Guardian Music | rss | 9 | 9 | diário |
| NTS Radio API | api_json | 10 | 10 | real-time |
| BBC 6 Music | scrape | 10 | 10 | real-time |
| Radar Lisboa | scrape/podcast | 10 | 9 | semanal |
| KEXP SOTD | podcast_rss | 9 | 9 | diário |
| AOTY | scrape | 9 | 9 | diário |
| First Floor | rss | 10 | 10 | semanal |
| Bandcamp Daily | rss | 9 | 9 | diário |
| Cabbages (rap) | rss | 8 | 9 | semanal |

---

## Notas técnicas finais

- **Feeds não-verificados:** alguns URLs RSS (Quietus, RA, Radar) são padrões prováveis mas devem ser **testados primeiro** pelo agent. Códigos HTTP 200 com Content-Type `application/rss+xml` ou `application/xml` confirmam.
- **Rate limits:** Pitchfork e Guardian aguentam scraping moderado; AOTY tem Cloudflare (usar package Python ou cache agressivo).
- **Geo-restrictions:** BBC Sounds áudio só funciona em UK. Tracklists e metadados são públicos globalmente.
- **Update do JSON:** revisitar a cada 6 meses. Publicações fecham (Drowned in Sound já fechou e reabriu) e RSS feeds mudam URL.

O ficheiro `music_sources_for_agent.json` está pronto para ser carregado por qualquer agent — Python, Node, LangChain, etc.
