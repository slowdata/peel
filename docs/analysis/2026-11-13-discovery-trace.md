# Registo prospetivo da descoberta

## Decisão

Integrar observação passiva na execução semanal antes da primeira fila real
pós-correções. Não introduzir mais alterações de seleção, fontes, quotas ou
pedidos de metadados para preparar a medição. O pedido autoriza o registo no
Peel; não uma nova execução de produção, alterações ao agendamento, novas
publicações ou instrumentação do agente Pi.

## Onde fica

- Código: `src/peel/discovery_trace.py`; observadores na pipeline e nos caminhos
  de ordenação/admissão já executados.
- Dados: `data/reports/discovery/<ISO-week>/run-<id>-<attempt>-<UTC>-<unique>.jsonl`.
- Ativação automática: apenas no passo de execução de `.github/workflows/weekly.yml`,
  com `PEEL_CAPTURE_DISCOVERY=1`.
- Os registos de execuções bem-sucedidas acompanham o commit semanal dos relatórios.
- Artifact GitHub por tentativa, sete dias, upload `always()`/não bloqueante.
  A seleção do artifact não inclui ficheiros de outras tentativas ou backups.

Cada stream é criado exclusivamente, sem substituir os anteriores. O início
regista schema v1, revisão `GITHUB_SHA` quando conhecida, hash SHA-256 da DB
inicial, semana e configuração. Os eventos são escritos e flushed linha a linha;
a ausência de footer indica execução/captura incompleta, não sucesso.

## O que se observa

1. Entradas devolvidas pelos parsers, timestamps editoriais e filtragem temporal.
   No Bandcamp, também o conjunto limitado de inspeção e decisões sobre formato,
   datas, pré-venda/arquivo, encaminhamento para faixa e itens não inspecionados.
2. Retry de unmatched, pesquisas/matches efetivamente feitos, aliases de URI,
   bloqueios de item, elegibilidade de lançamento e caps de ingestão.
3. Pool de rotação de faixas, menções e metadados já lidos localmente, prioridade
   de novidades, feedback presente, orçamento de verificação dos pendentes,
   fallback, chaves de diversidade e ordem final.
4. Menções e agregados de álbuns, fontes/famílias, feedback vigente, janelas
   editorial/pending, qualidade ajustada e afinidade/chaves já calculadas.
5. Pool limitada de resolução, links e metadados efetivamente consultados,
   exclusões reais e candidatos que ficaram por avaliar quando a fila encheu.
6. Seleção planeada versus Spotify confirmado e snapshots efetivamente persistidos.
   Uma escrita Spotify falhada nunca passa a aparecer como entrega confirmada.

Os mapas do perfil por artista/género são guardados como pares, para não confundir
nomes musicais com nomes de campos de credenciais. Não se guardam comentários
pessoais, mensagens brutas de exceção, HTML, respostas HTTP integrais ou segredos.
URLs com credenciais/hosts locais são omitidos; queries são removidas, exceto
parâmetros numéricos públicos limitados (`p`, `page`, `year`, `month`).

## Fronteiras e segurança

É o conjunto efetivamente observado dentro dos parsers, janelas e orçamentos
existentes, não a internet inteira. Não se inventam informações sobre candidatos
não resolvidos. Outros descartes internos dos parsers não se reconstituem.
A captura não prova novidade de todas as gravações nem transforma scores críticos
em afinidade pessoal. Não haverá reconstrução retroativa das filas W41–W45.

O observer usa contexto local à execução e só recebe valores já disponíveis.
Não devolve inputs ao ranking, não aumenta orçamentos e não consulta fornecedores.
Não há novas tabelas ou migrações. Erros de captura são best-effort e não bloqueiam
seleção/entrega; os limites de 50 mil registos/20 MiB, truncagem e erros observados
são explícitos. Um processo morto abruptamente pode deixar um stream parcial; um
runner perdido pode impedir também o upload do artifact.

`--dry-run` não persiste o registo. A implementação e os testes não fazem uma
execução antecipada na DB canónica, Spotify, Telegram ou site. A W45 aprovada,
feedback/comentários/timestamps e filas existentes são preservados.

## Validação

Os testes comparam execuções equivalentes com captura desligada, ligada e com
disco indisponível: mesmas filas, mesmas linhas de todas as tabelas, mesmas
pesquisas/consultas Spotify e MusicBrainz e mesma entrega Telegram. Exercitam
limites, pedidos não efetuados, controlos, títulos de arquivo, datas antigas,
links não resolvidos, fallbacks, orçamento pending, serialização segura, streams
parciais, isolamento dry-run e ausência de falsa confirmação após falha Spotify.

Resultado: **938 testes passaram em plain e 938 em ANSI** (22 casos novos),
Ruff/formatação/diff limpos. As comparações ligadas/desligadas/com falha de disco
preservam integralmente as tabelas e chamadas externas. Os 55 ficheiros protegidos
(DB, relatórios e JSONs públicos, incluindo a W45) conservaram os hashes anteriores.
Não foi criada uma captura de produção nem antecipada a execução semanal.
