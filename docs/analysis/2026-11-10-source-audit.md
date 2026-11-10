# Fontes e curadores: primeira auditoria W41–W45

Investigação em 9–10 de novembro de 2026. Janela: **5 de outubro a 8 de novembro**, inclusive, as cinco semanas completas W41–W45.

## Decisão proposta

1. **Corrigir a cobertura Bandcamp antes de acrescentar mais editoras.** O leitor ignora os cartões atuais da página e lê apenas `data-client-items`, que nos dois casos inspecionados contém outros títulos, mais antigos.
2. **Faixas:** manter a base Stereogum; experimentar curadorias pequenas de **So Young, Hard of Hearing e DIY Neu Bulletin**. **Tia Ho** é uma candidata complementar interessante, mas a sua amostra acessível é parcial.
3. **Álbuns:** experimentar **Hard of Hearing** e a secção específica **Stereogum Album of the Week**, não o noticiário geral de mais uma publicação. Não há ainda votos suficientes para afirmar que estas candidatas acertam mais.
4. **Validar lançamento, formato e disponibilidade antes da entrada.** Uma notícia nova ou uma adição recente a uma playlist não tornam a gravação nova. Não confundir um single alojado em `/album/` com um LP.

Não foram ativadas fontes, alterados pesos, votos, filas ou código de produção. Não houve escritas Spotify, publicação no site ou Telegram. Não foi integrado um classificador nem comprado qualquer acesso.

## Método e limites

- Estado local em `d23630f`, com feedback musical preservado na base cujo SHA-256 consta abaixo.
- Filas efetivamente entregues: 140 posições de faixas, **135 URIs distintas**, e **31 identidades de álbuns/EPs**. Repetições entre semanas contam uma vez nas tabelas por fonte.
- Leitura SQLite em `mode=ro` e `query_only`; metadados Spotify obtidos por operações de leitura para as 135 faixas e os 31 discos, sem erros nessas consultas.
- As origens são menções persistidas cujo primeiro registo, agregado entre aliases por identidade/fonte, pertence à janela. **Estas datas são de recolha, não de publicação.** Não atribuir causalmente à fonte a seleção ou o voto.
- Este é um levantamento **do que o Peel entregou e conservou**, complementado por amostras públicas datadas de novas candidatas; **não é um censo integral de todos os lançamentos publicados por cada site**. Os títulos perdidos no cap não ficaram todos registados.
- Positivo significa `love` ou `like`. Sem voto é desconhecido. Não converter os votos antigos nem comparar médias como se a calibração nunca tivesse mudado.
- W41 e W44 não estão completamente avaliadas. Votos são as opiniões mais recentes conservadas, sem histórico integral de revisões. Fontes sobrepõem-se; não somar as suas linhas nem tratar as observações como independentes.
- O primeiro filtro usa **data da edição associada no Spotify**. Isso não prova, por si só, primeira publicação da gravação: uma reedição silenciosa pode escapar. As recomendações concretas abaixo têm também contexto editorial de lançamento; não prometer deteção perfeita de arquivo.

## Faixas: o sinal observado

Das 135 URIs, 121 têm edição datada dentro da janela; 13 ficam fora e uma é excluída por indicação de arquivo no título. Entre as 121, há **77 avaliadas, 55 positivas e 44 sem voto**.

| Fonte/secção | Positivas / avaliadas | Faixas distintas na amostra datada |
| --- | ---: | ---: |
| Stereogum — New Music | 29 / 36 | 55 |
| Stereogum — Best Songs | 13 / 16 | 25 |
| Pitchfork Selects | 12 / 20 | 26 |
| Consequence — Music | 3 / 8 | 12 |
| Gorilla vs. Bear | 3 / 3 | 3 |
| The Quietus — Tracks of the Month | 3 / 3 | 6 |
| The Line of Best Fit — News | 2 / 2 | 4 |
| NPR — New Music Friday Starting 5 | 2 / 2 | 6 |
| Pitchfork — Best New Tracks | 1 / 2 | 2 |
| Pitchfork — News | 0 / 1 | 1 |

**Leitura:** Stereogum continua a ser uma boa base nas duas secções observadas. Selects não é inútil. Consequence merece comparação com curadorias mais ajustadas, não um banimento por oito votos. Os 3/3 e 2/2 são promissores, não provas de superioridade.

### A data editorial não chega

Exemplos com mais de cinco semanas entre a data Spotify da edição e a primeira entrega na janela:

- SHEE — *Woman*: 19/06, entregue W41, intervalo de 112 dias.
- Troye Sivan — *She's The Best*: 14/08, W41, 56 dias.
- 10KDunkin — *Freaked Out*: 13/04, W42, 186 dias.
- Holy Tongue — *Vuoto 25*: 02/09, W43, 51 dias.
- Pigeon — *NRG*: 01/05, W44, 182 dias.

Não são votos errados nem necessariamente más escolhas musicais; são exemplos que não satisfazem uma política de lançamentos das últimas 3–5 semanas. Inversamente, ficar fora da janela global não prova um erro à data da entrega: uma faixa de setembro ainda podia ser recente em W41. Não contar automaticamente as 13 exclusões como 13 bugs.

## Álbuns: avaliar separadamente

Das 31 identidades, 22 têm edição na janela. Uma é **Deafheaven — At Work in the Fields of the Bomb**, identificada como EP de três faixas: fica à parte, sem reescrever a fila histórica ou o seu voto. Restam **21 edições classificadas como álbum**, 17 avaliadas, **7 positivas** e quatro sem voto.

| Fonte/secção | Positivos / avaliados | Álbuns distintos na amostra datada |
| --- | ---: | ---: |
| DIY — Album Reviews | 4 / 4 | 4 |
| Clash — Album Reviews | 5 / 8 | 10 |
| Pitchfork — Best New Albums | 2 / 3 | 3 |
| Pitchfork — Album Reviews | 5 / 12 | 14 |
| Beats Per Minute — Album Reviews | 2 / 7 | 7 |
| The Quietus | 1 / 3 | 4 |
| The Guardian — Albums | 1 / 7 | 7 |
| Treble — Album Reviews | 0 / 2 | 2 |

**Leitura:** DIY e Clash merecem atenção pelo sinal nesta parte ouvida, não uma garantia de precisão futura. Treble não pode ser condenada com dois discos. A amostra é condicionada pelo ranking/consenso e não representa toda a música que cada publicação recomendou. Também não prova que reduzir Guardian ou Pitchfork melhoraria os sete escolhidos.

Na verificação atual, o feed geral DIY tem notícias e Neu Bulletins recentes, mas não fornece uma amostra recente de críticas de álbuns no recorte visível. Isso não prova que a publicação tenha deixado de fazer críticas. Não confundir atividade do site, cobertura do feed e afinidade musical.

## Bandcamp: problema de cobertura confirmado

Inspeção de `https://subpop.bandcamp.com/music` e `https://sacredbonesrecords.bandcamp.com/music`, comparando a grelha HTML e `data-client-items` da respetiva página:

| Editora | Entradas em `data-client-items` | Cartões visíveis inspecionados | Cartões visíveis ausentes dessa lista |
| --- | ---: | ---: | ---: |
| Sub Pop | 782 | 16 | 16 |
| Sacred Bones | 381 | 16 | 16 |

O código lê **apenas aquela lista** e limita-a antes de verificar as páginas/datas. Os primeiros 20 itens inspecionados de cada lista não tinham lançamentos na janela. Aumentar simplesmente o cap de cinco para 20 não recupera os cartões ausentes.

Exemplos atuais da grelha Sub Pop, confirmados nas páginas dos próprios lançamentos:

| Artista — lançamento | Data Bandcamp | Faixas | Destino correto |
| --- | --- | ---: | --- |
| [Twisted Teens — The Holy Cross Tigers](https://twistedteens.bandcamp.com/album/the-holy-cross-tigers) | 06/11/2026 | 13 | Álbum |
| [PISS — you made me / skin on linen](https://pisstheband.bandcamp.com/album/you-made-me-skin-on-linen) | 30/10/2026 | 15 | Álbum |
| [Hannah Jadagu — Curse](https://hannahjadagu.bandcamp.com/album/curse) | 04/11/2026 | 1 | Faixa, apesar do URL `/album/` |

Nenhum está em pré-venda. A grelha também inclui reedições/aniversários; **não basta passar a aceitar todos os cartões**. Manter exclusões de arquivo, datas reais, pré-vendas e separação de formatos. Evitar que itens excluídos consumam todo o orçamento de inspeção, mantendo pedidos limitados.

Isto confirma uma falha **atual** em duas editoras, compatível com o vazio Bandcamp recente. Não reconstrói os HTML de todas as sextas-feiras nem demonstra que as outras três editoras tenham exatamente o mesmo problema.

**Esclarecimento posterior ao estudo de consenso:** PISS é efetivamente um LP de 15 faixas, não um single duplo. A elegibilidade marcada como incerta nesse estudo fica esclarecida por esta verificação. Continua sem voto de álbum: não passa a contar como acerto de uma alternativa simulada.

## Novas curadorias: o que consegui verificar

### Faixas

- **[So Young](https://soyoungmagazine.com/feed/)**: duas páginas RSS cobrem a janela, com 17 entradas, incluindo eventos/edição da revista que não devem virar música. Notícias de singles incluem Kiosk, Heartworms, Nabihah Iqbal e Another Country $$$$. Páginas individuais por vezes apresentam CAPTCHA, mas há conteúdo RSS utilizável. Candidata próxima das guitarras alternativas, sem excluir eletrónica.
- **[Hard of Hearing](https://hardofhearingmagazine.com/feed/)**: roundups de faixas datados de **09/10, 16/10, 30/10 e 06/11**, com contexto musical e links. Separar os roundups de entrevistas/concertos. Lloyd Bolton aparece entre os autores da amostra. É uma hipótese de curadoria, não qualidade pessoal já medida.
- **DIY — The Neu Bulletin**: artigos de **23/10, 30/10 e 06/11** lidos integralmente, com **5, 7 e 13 faixas**, respetivamente. O corpo tem mais escolhas do que os três nomes do título; não extrair apenas o título ou slug. Inclui guitarras, pop e eletrónica, com textos individuais. Exemplos: Kiosk, Another Country $$$$, 1000 Rabbits. Secção nova para o Peel, distinta da fonte DIY de álbuns.
- **[Tia Ho](https://linktr.ee/tiahqho)**: seguir a playlist específica **[Hot New Singles](https://open.spotify.com/playlist/7BqeFIwpw3FYPIukKkGimJ)**, não todas as músicas mencionadas em entrevistas. A playlist **[2026 Drops](https://open.spotify.com/playlist/6QCumttutzfSdmCJlJMoje)** não demonstra, por si só, recomendações de álbuns completos. A API recusou a leitura integral dos itens; a página pública só permitiu uma amostra. Das seis faixas verificadas, quatro têm edição na janela e duas são anteriores: Lucy Park (28/08) e BICEP/SOARR (25/09). Nenhuma das seis tinha voto local de faixa. **Não atribuir uma taxa de acerto a Tia nem fingir um arquivo completo de cinco semanas.**

Neu Bulletins consultados: [23/10](https://diymag.com/feature/the-neu-bulletin-bed-piss-jo-from-school-and-more-2), [30/10](https://diymag.com/feature/the-neu-bulletin-bathing-suits-little-grandad-the-healing-power-of-horses), [06/11](https://diymag.com/feature/the-neu-bulletin-silver-gore-1000-rabbits-dermot-henry).

### Álbuns

- **[Hard of Hearing — October Roundup](https://hardofhearingmagazine.com/2026/11/05/october-roundup-key-releases-from-hmltd-cameron-winter-silverwingkiller-and-more/)**, 05/11: mistura LPs e EPs, com comentários críticos; nem todos são elogios/recomendações positivas. Separar formatos e validar datas. Exemplos para investigação: Radio Free Alice, RIP Magic, HMLTD e PISS.
- **[Stereogum — Album of the Week](https://stereogum.com/2514364/album-of-the-week-sophia-stel-the-greatest-album-of-all-time/reviews/album-of-the-week)**: cinco escolhas na janela editorial — Cameron Winter, Sophia Stel, Josh Johnson, DIIV e Willi Carlisle. Há um Like local para Cameron Winter e um Dislike para DIIV; os outros três álbuns não têm voto. Isto é **1/2 na parte conhecida**, não prova de afinidade suficiente. A secção oferece uma seleção explícita em vez de todo o catálogo de críticas.
- **[Hearing Things — Five Albums](https://www.hearingthings.co/tag/five-albums/rss/)**: cinco artigos datados na janela, mas as listas completas não ficam todas acessíveis no conteúdo público consultado. Promissora para exploração manual; não há base para ativação ou avaliação de precisão. **[Five Songs](https://www.hearingthings.co/tag/five-songs/rss/)** inclui artigos temáticos de Halloween; nem o título nem a data do artigo provam que as faixas sejam novas. Não usar automaticamente como feed de lançamentos.
- **Bill Pearis / Indie Basement, Eli Enis, John Kennedy / X-Posure e Under the Radar / Songs of the Week**: candidatos descobertos na pesquisa de críticos/curadores US/UK, mas sem uma amostra integral, datada e verificável de lançamentos nesta janela. Algumas páginas estão bloqueadas ou limitadas. Ficam por verificar, não reprovados por gosto musical. Não houve subscrições nem tentativas de contornar acesso pago.

## Recomendações concretas encontradas

**Não são uma nova fila nem acertos previstos.** Datas da edição confirmadas por leitura Spotify/Bandcamp; artigos/curadorias identificados acima. Não há votos de álbum para os LPs desta tabela. As faixas abaixo também não têm voto local.

### Faixas

| Artista — faixa | Lançamento | Curadoria onde apareceu |
| --- | --- | --- |
| [Kiosk — Baby Teeth](https://open.spotify.com/track/4mhH137pWiEqSxSL1B6f9Z) | 21/10 | So Young, Hard of Hearing, DIY Neu Bulletin |
| [Heartworms — Guess I'm Stuck In The Dark](https://open.spotify.com/track/6vhGYTtmSsk2JsCjO4Ngp0) | 26/10 | So Young; também noticiada pela DIY |
| [Another Country $$$$ — WANT](https://open.spotify.com/track/2tUG60GZjYMbZTYFBBSvw7) | 04/11 | So Young, Hard of Hearing, DIY Neu Bulletin |
| [Font — Punish Task](https://open.spotify.com/track/3TUJkCw8IXrFs0CiRF8CDI) | 07/10 | Tia Ho |
| [Nabihah Iqbal — Heaven Can Wait](https://open.spotify.com/track/3K44mxnFOVgHwFWMeRKrbd) | 20/10 | Tia Ho, So Young |
| [Deki Alem — Pretty Toes](https://open.spotify.com/track/6W0tPbrogUy2E0BAY2AkUn) | 22/10 | Tia Ho |

### Álbuns

| Artista — álbum | Lançamento | Curadoria |
| --- | --- | --- |
| [Radio Free Alice — What's In Between](https://open.spotify.com/album/0o3Zhp5zTfwhgCjPxPYZOj) | 30/10 | Hard of Hearing |
| [RIP Magic — 1,2,3](https://open.spotify.com/album/1tMPaXMpEEWTYeDEiAlcVv) | 30/10 | Hard of Hearing |
| [HMLTD — FANGS](https://open.spotify.com/album/5wkVUK1l05V4kXvVcWwNS7) | 23/10 | Hard of Hearing |
| [Sophia Stel — the greatest album of all time](https://open.spotify.com/album/604KxCjle0KCw0VXdoMNgj) | 16/10 | Stereogum Album of the Week |
| [PISS — you made me / skin on linen](https://pisstheband.bandcamp.com/album/you-made-me-skin-on-linen) | 30/10 | Hard of Hearing, Sub Pop/Bandcamp |
| [Twisted Teens — The Holy Cross Tigers](https://twistedteens.bandcamp.com/album/the-holy-cross-tigers) | 06/11 | Sub Pop/Bandcamp |

Nota de disponibilidade: Josh Johnson — *Animative* tem edição física/download de 23/10, mas chegada ao streaming anunciada para 10/11. Não o contar sem ressalva como alternativa Spotify que já podia ter sido entregue nas sextas-feiras da janela.

## Próximo passo recomendado

Corrigir a entrada Bandcamp e definir um filtro transversal de lançamento/formato; depois testar poucas secções curadas, dentro do esforço semanal existente (28 faixas/7 álbuns), sem quotas por género, obrigação de consenso ou listas de artistas fixas. Guardar separadamente o que é novo de verdade, o que apenas recebeu nova menção e o que continua por ouvir. Ativar mais feeds, sem verificar a admissão/ranking, pode não alterar os sete álbuns que chegam ao utilizador.

Qualquer comparação posterior tem de conservar as alternativas desconhecidas como desconhecidas. Não marcar a pesquisa como uma melhoria musical comprovada, nem prometer verificações futuras automáticas.

## Evidência e integridade

Resumo estruturado e observações de metadados: `docs/analysis/data/2026-11-10-source-audit.json`.

Artefactos completos de investigação: `/tmp/peel-source-window-E96JVh/` — `collect_state.py`, `local-cohort.json`, `spotify-release-metadata.json`, `dated-cohort.json`, `candidate-feeds.json`, `tia-visible-track-dates.json`, `bandcamp-coverage.json`, grelhas/listas das duas editoras e `sample-candidate-metadata.json`. São caches de investigação, não entradas de produção.

Hashes preservados antes/depois:

- `data/peel.db`: `737f75a2bb25cf3c71fb50f5adfdf5790a666c0bc4d61d602ae94236ee41b37e`.
- `data/reports/2026-W45.md`: `fd1daeb2c75a98ec47155e06fb1dd9ff215daf14b8dff877ddfe179c9b25f47e`.
