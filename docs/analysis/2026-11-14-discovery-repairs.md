# Correções após a primeira captura W46

## Âmbito

Corrigir resolução de edição e ruído nos nomes extraídos; diagnosticar Treble
antes de alterar o parser. Não refazer a W46, filas históricas, opiniões,
timestamps, seleção pública ou agendamento. Não acrescentar fontes, quotas ou
uma nova fórmula de gosto.

## Edição Spotify

A captura da execução `43520717694` mostra, nesta ordem, `Carnaval De Luz
(Deluxe Edition)` e `Carnaval De Luz`. O resolver aceitava o primeiro match
fuzzy; a admissão recusava depois a edição Deluxe, sem tentar a edição normal.

O normalizador partilhado de matching remove qualificadores como `(Deluxe
Edition)`. Por isso, a preferência de edição usa uma chave separada que conserva
todas as palavras do título, normalizando apenas caso, acentos e pontuação.
Entre candidatos que já passam os limiares de artista/título e têm URL, o título
exato solicitado tem prioridade. Sem título exato, mantém-se o primeiro match
qualificado. Um pedido explícito por uma edição Deluxe continua a obter essa
edição: não se troca silenciosamente o que foi pedido.

Não há segunda pesquisa nem aumento de candidatos/consultas. As verificações
normais de data efetiva, formato e arquivo continuam depois da resolução.
O search gravado não prova que a edição normal dos U2 seria elegível: os seus
metadados de admissão não foram consultados naquela execução. Os testes de
admissão usam metadados sintéticos, claramente distintos dessa observação.

## Nomes de artistas

- Gorilla vs. Bear: os marcadores editoriais `video:`, `video premiere:` e
  `premiere:` deixam de fazer parte do artista; filtros de fotos/live mantêm-se.
- Stereogum: cláusulas explícitas de mudança de nome (`Rebrands As … Following…`)
  ficam reduzidas ao novo nome; o caso Lambb é coberto.
- Contexto de afiliação em parênteses (`of`/`from`/`ex-` ou lista de três grupos)
  deixa de fazer parte do artista. Preservam-se créditos `feat.`/`with`, nomes
  entre parênteses não reconhecidos e nomes completos corroborados por tags.
- O prefixo observado `NYC's` só é removido quando o nome mais curto é corroborado
  por uma tag de artista ou âncora de tag/artista no conteúdo já recebido. Não
  se adivinham nomes por localização e não se fazem pedidos de artigos extra.

Estas regras aplicam-se às próximas extrações, não a uma reescrita dos dados,
filas ou relatórios anteriores. Não há listas fixas de artistas nem alterações
à semântica de votos/bloqueios.

## Treble: evidência e limite do diagnóstico

O log W46 registou erro XML em `line 2, column 1326`, zero entradas e status
`ok`/feed vazio. O corpo dessa resposta não foi preservado: não é possível provar
se foi HTML de erro, bloqueio ou outro problema transitório.

No probe de 14 de novembro, o transporte atual e HTTPX com user-agent feedparser
e browser devolveram o mesmo RSS válido: HTTP 200, dez entradas e três críticas
reconhecidas na primeira página. Não se reproduziu a falha; não há evidência
para mudar URL, categorias, transporte, user-agent ou limites de paginação.

A proteção nova distingue um vazio válido de uma resposta defeituosa: erro HTTP
ou parsing inválido sem entradas lança erro de fonte, em vez de sucesso vazio.
Avisos de parsing **com entradas utilizáveis** continuam tolerados. Uma falha na
página secundária conserva o que já foi lido e fica explícita no discovery trace,
marcando a captura como parcial. Não há retries adicionais.

### Diagnóstico de 23 de novembro

Na W47, o RSS geral devolveu HTTP 403. Um [diagnóstico só de leitura em GitHub Actions](https://github.com/slowdata/peel/actions/runs/45186385917),
com o mesmo `feedparser` e Ubuntu 24.04, reproduziu HTTP 403 com
`cf-mitigated: challenge` nos dois endpoints oficiais: `/feed/` e
`/category/album-reviews/feed/`. Ambos devolveram HTML em vez de RSS; ambos
responderam com RSS válido a partir do computador local. Não sabemos qual a
regra que disparou o desafio nem se explica o erro da W46.

O Peel passa a identificar o desafio explicitamente (`TrebleFeedChallenge`)
e a rejeitar também HTML sem entradas que o parser não marque como inválido.
Não tenta contornar o desafio nem altera a fonte, a agenda ou o ranking.
A recolha automática depende de um endpoint público utilizável no runner ou
de um ajuste no serviço do editor; a melhoria no diagnóstico não desbloqueia
o feed.

## Verificação

Fixtures reduzidas: os dois resultados Spotify efetivamente observados em W46 e
metadados públicos do feed Treble observado em 14 de novembro, não uma reconstrução
histórica. Testes cobrem preferência e pedidos explícitos de edição, manutenção
na admissão dos travões de idade/formato/arquivo, ruído versus nomes/créditos
legítimos, corroboração por tags/conteúdo, RSS válido vazio, erro HTTP, erro XML
sem entradas, warning com entradas e falha apenas de paginação secundária.

Passaram **986 testes em plain e 986 em ANSI**, incluindo 48 novos casos; Ruff,
formatação e `git diff --check` também passaram. As validações de admissão não
foram relaxadas e o resolver continua a efetuar uma única pesquisa.

A implementação não executa a pipeline de produção nem escreve em Spotify,
Telegram, site ou feedback. O estado local e as edições existentes são protegidos
por backups/hashes. O novo código é integrado sobre o estado W46 remoto, sem
publicar a DB local antiga por cima dele.
