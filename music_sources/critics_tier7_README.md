# Críticos Individuais — Atualização v1.1

> Adição ao guia principal: agora tens **17 críticos individuais reconhecidos** com presença online acessível para um agent. Todos vão para o Tier 7 do `music_sources_for_agent.json` (versão 1.1).

---

## A descoberta técnica chave: YouTube tem RSS nativo

**Qualquer canal de YouTube tem um feed RSS automático.** Padrão fixo:

```
https://www.youtube.com/feeds/videos.xml?channel_id={UC_ID}
```

O `channel_id` começa sempre com `UC...`. Para encontrá-lo:
- Se o URL do canal é `youtube.com/channel/UCxxxxx` → o ID está ali
- Se é `youtube.com/@nome` → carregar a página, ver código-fonte (Ctrl+F), procurar `channelId`

O feed retorna os últimos 15 vídeos como Atom XML com título, descrição, video_id, data e thumbnails. **Sem API key, sem limites de rate visíveis para uso pessoal.**

Isto é grande porque significa que YouTube — que é onde muitos críticos vivem — fica acessível a um agent como qualquer feed RSS.

---

## Os 17 críticos individuais que adicionei

### Top 5 com match perfeito ao teu gosto (relevância 9-10)

**1. Indiecast (Steven Hyden + Ian Cohen)** — `relevance 10`
- Podcast semanal, sextas-feiras
- Feed: `https://rss.art19.com/indiecast`
- **Por que importa:** dois críticos consagrados (Hyden é colunista da Uproxx, Cohen ex-Pitchfork) discutem indie/post-punk semanalmente, com "Recommendation Corner" que surfa artistas under-the-radar
- Episódios recentes cobriram Nick Cave, Geese, This Is Lorelei — todos no neighborhood da tua playlist

**2. Simon Reynolds (Blissblog)** — `relevance 10`
- Blog Blogger com RSS nativo
- Feed: `https://blissout.blogspot.com/feeds/posts/default`
- **Por que importa:** o **maior crítico-historiador de post-punk vivo**. Autor de *Rip It Up and Start Again* (a história definitiva do post-punk). Cunhou o termo "post-rock"
- Direct hit para a tua paixão por post-punk

**3. Philip Sherburne (Futurism Restated)** — `relevance 10`
- Substack
- Feed: `https://philipsherburne.substack.com/feed`
- **Por que importa:** ex-crítico de eletrónica do Pitchfork por anos. Especialista em ambient/techno/experimental
- Direct match para o teu lado Caribou/Maribou State

**4. Anthony Fantano (theneedledrop)** — `relevance 9`
- YouTube + Substack + podcast
- Feed: `https://www.youtube.com/feeds/videos.xml?channel_id=UCt7fwAhXDy3oNFTAzF2o8Pw`
- **Por que importa:** o NYT chamou-lhe em 2020 "provavelmente o crítico de música mais popular ainda em pé". Reviews diárias com escala 0-10 e camisa de flanela colorida (amarela=positivo, vermelha=negativo). Cobre desde Drake até experimental obscuro — exatamente o teu brief
- Os vídeos "Track Roundup" semanais são óptimos para weekly digest porque cobrem 5-10 faixas de uma vez

**5. Amanda Petrusich (The New Yorker)** — `relevance 9`
- Feed: `https://www.newyorker.com/feed/contributors/amanda-petrusich/rss`
- **Por que importa:** a sua lista de melhores de 2025 incluiu Ben Kweller, Geese, Hannah Cohen, Hayley Williams, Oneohtrix Point Never. Vários no teu neighborhood

### Outros importantes (relevância 7-9)

| Crítico | Plataforma | Feed | Especialidade |
|---|---|---|---|
| **Lindsay Zoladz** (NYT) | Newsletter | scrape `nytimes.com/column/the-amplifier` | Indie + alternative pop |
| **Jenn Pelly** (Pitchfork) | Substack | `jennpelly.substack.com/feed` | Indie experimental, mulheres no rock |
| **Joe Muggs** | Substack | `joemuggs.substack.com/feed` | Bass music UK, dub, electronic underground |
| **Robert Christgau** | Substack | `anditdontstop.substack.com/feed` | O dean da crítica rock americana |
| **Pitchfork Review Podcast** | Podcast | `pitchfork.com/feed/podcast/the-pitchfork-review/rss` | Bastidores das reviews da Pitchfork |
| **Sound Opinions** (DeRogatis + Kot) | Podcast | `feeds.feedburner.com/SoundOpinions` | Rock criticism, desde 2005 |
| **All Songs Considered** (NPR) | Podcast | `feeds.npr.org/510019/podcast.xml` | NPR flagship discovery (Ann Powers + outros) |
| **Jon Caramanica** (NYT Popcast) | Podcast | `feeds.simplecast.com/54nAGcIl` | Pop e rap — o lado Drake-tier que pediste |
| **Switched on Pop** | Podcast | `feeds.megaphone.fm/switchedonpop` | Análise musicológica de pop |
| **Dead End Hip Hop** | YouTube | `youtube.com/feeds/videos.xml?channel_id=UCwNzLpk33SVrCgWuAW7Pwug` | Hip-hop discussions em grupo |

---

## Como integrar isto na tua app de lista semanal

Para uma aplicação que constrói listas semanais, sugiro este pipeline:

### Pipeline mínimo (todos os feeds são RSS/podcast_rss)

```python
# Pseudocódigo
import feedparser

CRITICS_FEEDS = [
    "https://rss.art19.com/indiecast",
    "https://www.youtube.com/feeds/videos.xml?channel_id=UCt7fwAhXDy3oNFTAzF2o8Pw",
    "https://blissout.blogspot.com/feeds/posts/default",
    "https://philipsherburne.substack.com/feed",
    "https://anditdontstop.substack.com/feed",
    "https://www.newyorker.com/feed/contributors/amanda-petrusich/rss",
    "https://feeds.simplecast.com/54nAGcIl",  # NYT Popcast
    "https://feeds.megaphone.fm/switchedonpop",
    "https://feeds.feedburner.com/SoundOpinions",
    "https://feeds.npr.org/510019/podcast.xml",
    "https://pitchfork.com/feed/podcast/the-pitchfork-review/rss",
    # ... mais
]

for url in CRITICS_FEEDS:
    feed = feedparser.parse(url)
    for entry in feed.entries[:5]:  # últimos 5
        # Extrair: title, description, published, link
        # Detectar menções de artistas (regex contra lista)
        # Detectar scores (regex contra "8/10", "Best New", etc.)
        # Adicionar à lista semanal
```

### Que campos extrair de cada item

Para cada review/episódio/vídeo do feed:

| Campo | Como extrair |
|---|---|
| **Artista mencionado** | Regex contra a tua lista de 28 artistas + lista de palavras-chave do título |
| **Álbum mencionado** | Detectar padrões em itálico/aspas no título e descrição |
| **Score/Verdict** | Regex específica por crítico: Fantano usa "/10", Christgau usa letras (A+, A-), Pitchfork score numérico, Indiecast diz "AOTY contender" |
| **Género detectado** | Lista de keywords matching contra `genres_covered` da fonte |
| **Sentiment** | Classificação simples positivo/negativo/neutro a partir de adjetivos no título/descrição |

### Triggers para destacar:

- ✅ **STRONG SIGNAL:** mesmo álbum mencionado por 3+ críticos individuais numa semana
- ✅ **HIGH RELEVANCE:** crítico do tier 7 menciona artista da tua lista de 28
- ✅ **NEW ARTIST INTRO:** crítico high-authority dá review positivo (Fantano 7+, Pitchfork BNM, Christgau B+)
- ⚠️ **WORTH INVESTIGATING:** menção positiva por 1 crítico de relevância 9-10

---

## Notas finais

- **Twitter/X dos críticos:** mais difícil — Twitter API é paga e restrita. Para um agent pessoal, melhor confiar em RSS dos seus blogs/Substacks
- **Instagram:** sem API pública prática para um agent. Geralmente os críticos mais sérios duplicam tudo no blog/Substack
- **Apple Podcasts API:** se o agent precisar de procurar feeds RSS de podcasts por nome, há a `iTunes Search API` gratuita: `https://itunes.apple.com/search?term={podcast_name}&media=podcast&entity=podcast` — devolve `feedUrl` no JSON

A versão atualizada do JSON (v1.1) está pronta para o agent. Inclui agora **40+ fontes** organizadas em 7 tiers (publicações, rádios, agregadores, Substacks, suporte, hip-hop específico, e críticos individuais).
