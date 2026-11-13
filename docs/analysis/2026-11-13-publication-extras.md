# Publicação de álbuns fora da fila semanal

## Objetivo e fronteira

Permitir uma escolha editorial explícita de álbuns já avaliados positivamente,
mas ausentes da fila da edição em publicação. Ouvir no radar ou escrever Love/Like
não é autorização para publicar. Esta implementação prepara uma proposta; não
publica uma edição, não reconstitui semanas em falta nem muda a descoberta ou o
ranking.

## Contrato

- `publicar --album-extras PATH` recebe uma lista JSON de metadados explícitos:
  `artist`, `album`, `listen_url`, `listen_kind` e, opcionalmente, `editorial_url`.
- Até sete entradas, Love/Like atual, links diretos e identidades/links únicos.
  A combinação com a fila continua limitada a sete álbuns, sem mínimo artificial.
- Os códigos de escolha dos extras seguem os números da fila, mas aparecem como
  `Outside queue`: não se inserem nem renumeram linhas da fila privada.
- A seleção validada guarda `AdditionalAlbum` em `PublicationPlan`, incluindo os
  links, fontes locais e momento de captura. Fontes da mesma família contam uma
  só vez. Ausência de menções significa `Personal pick`/zero famílias; o radar
  ADM não cria uma fonte ou voto editorial adicional.
- O export guarda `selection_origin=outside_queue` e posição de fila `null`,
  sem inventar uma observação ou participação histórica na fila.
- Os novos extras passam pelo `NoveltyCheck` existente antes da substituição da
  playlist pública: datas efetivas no intervalo de 35 dias, formato, pré-venda e
  arquivo identificado. O lookup Spotify confirma álbum e artista. Falha ou
  metadados insuficientes impedem a publicação antes de escrever no Spotify.
- A verificação não prova a origem de todas as gravações: continua dependente
  dos metadados e sinais explícitos dos fornecedores, como a admissão semanal.
- Extras já publicados com a mesma identidade/link não são re-admitidos como
  novidades numa republicação posterior. Um novo link obriga a nova verificação.
- O sucesso da escrita Spotify mantém a leitura ordenada de confirmação do
  cliente existente; só depois são guardados seleção e registos das verificações.
- Reexportar usa a seleção congelada. Um `--refazer` explícito recupera os extras
  anteriores e propõe apenas os que continuam Love/Like; não altera retroativamente
  uma edição sem essa confirmação.

## Preview e compatibilidade

`publicar --dry-run` usa `DB.open_readonly`, `mode=ro` e `PRAGMA query_only=ON`.
Não chama auto-sync, inicialização/migração do schema ou serviços. Mostra os
candidatos e deixa explícito que a validação de lançamento/formato fica para a
publicação real. Não cria uma DB em falta.

O campo `additional_albums=[]` é compatível com planos históricos. A comparação
com uma seleção guardada normaliza os modelos antes de decidir se houve mudança;
não obriga a refazer nem reescreve seleções antigas por faltar o novo campo.
A exportação e o sync transportam o payload congelado sem mudar o schema.

## Verificação

A suite nova cobre seleção mista e ordem, Love/Like versus controlos, mudança de
opinião após preview, aliases, links inválidos/duplicados, limite conjunto de
sete, metadados e famílias congelados, round-trip export/sync, planos legados,
DB em falta/readonly, preview sem serviços e verificação antes de qualquer
escrita Spotify. Testa também que uma confirmação Spotify falhada não guarda a
seleção e que formato/pré-venda/arquivo Bandcamp continuam a bloquear admissão.

A proposta real foi preparada sobre SQLite só de leitura. O caminho de exportação
foi exercitado apenas numa cópia descartável da DB e o site construído numa cópia
isolada em `/tmp`, com a nova edição apenas nessa cópia. Os hashes da DB
canónica, relatórios e JSONs das edições públicas são comparados com a captura
anterior. Não se atualizam essas edições, a fila privada, Spotify ou Telegram
para preparar a proposta.

Resultados locais: **913 testes passaram em modo plain e 913 em ANSI**; o grupo
focado passou 205 testes. Ruff, formatação e `git diff --check` passaram. A build
isolada gerou 24 páginas em 6,39 s. O preview real da CLI apresentou sete faixas e
cinco álbuns, sem escrever estado; os 54 ficheiros protegidos (DB, relatórios e
JSONs públicos) mantiveram os hashes anteriores.
