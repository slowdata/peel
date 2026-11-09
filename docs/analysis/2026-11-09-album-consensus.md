# Resultado — testar consenso limitado nos álbuns

Tarefa: **T-20261109-HFJK**. Estudo de 2026-11-09.

## Decisão

**Não mudar já o consenso em produção.** Os bónus testados antecipam mais discos apreciados quando já existe gosto conhecido sobre os artistas. Não demonstram seleção melhor para artistas desconhecidos, nem resolvem os seis discos sem sinal pessoal que dominaram a W45.

Concluir esta tarefa com esta decisão, sem confundir «estudo concluído» com «nova fórmula implementada». A próxima investigação útil é T-20261109-W9R8, sobre um sinal pessoal para artistas desconhecidos. A medição da fila após as correções continua em T-20261031-VZQ3.

## O que foi comparado

O [plano](2026-11-09-album-consensus-plan.md) foi registado antes da execução. As mesmas semanas já tinham sido analisadas: é exploração, não validação independente. Não escolhemos o coeficiente que ficou melhor nos dados antigos.

A baseline é o código **dba7228**, publicado após corrigir identidade/diversidade e tornar Meh neutro. Não é exatamente a regra antiga que produziu cada fila. As quatro variantes usam essa mesma base:

- `baseline`: famílias primeiro, depois patamar de afinidade e qualidade/diversidade.
- `cap_2`: o primeiro critério passa a `min(famílias, 2)`.
- `bonus_020` / `bonus_040`: `afinidade + B × (1 − 1/famílias)`, com B fixo em 0,20/0,40; depois qualidade/diversidade e os mesmos desempates.

Nos bónus, a afinidade também passa de patamares a valor contínuo. **O efeito das duas alterações está combinado**; não se pode atribuir todo o ganho ao consenso limitado. Os valores contínuos ainda dependem de votos Love/Like cuja calibração pessoal mudou ao longo do tempo.

Entradas históricas: W41 d89c442, W42 b5d12d6, W43 85f53c8, W45 final 5f9ed2e. Resultados musicais: avaliações de 68e49f2, usadas só depois da escolha. A W45 inicial (041e312) fornece apenas prova anterior de link de escuta para Gyrofield, não é mais uma semana.

## 1. Ordem dos mesmos discos já ouvidos

São os sete discos efetivamente entregues em cada uma das quatro semanas completas. As 28 posições representam **27 álbuns distintos e 11 positivos** (Cameron Winter repetido). Aqui não se escolhem outros discos: só se muda a ordem daqueles que têm avaliação observada.

| Variante | W41: positivos nos primeiros 3 | W42 | W43 | W45 | Total nos primeiros 3 de cada semana |
|---|---:|---:|---:|---:|---:|
| Regra atual | 1 | 2 | 1 | 0 | 4/12 |
| Teto de 2 famílias | 2 | 3 | 1 | 0 | 6/12 |
| Bónus 0,20 | 2 | 3 | 3 | 0 | 8/12 |
| Bónus 0,40 | 2 | 3 | 3 | 0 | 8/12 |

Os pares positivo/não positivo corretamente ordenados passam de 14/34 para 18/34 no teto e 28/34 nos bónus. Estes pares são correlacionados, não 34 observações independentes. A W45 tem zero pares comparáveis: todos os discos foram não positivos; não é possível discriminar positivos nessa semana.

### De onde vem o ganho

«Conhecido» significa aqui ter feedback musical não neutro de faixas já guardado no Peel, não se o Dias conhece realmente o artista. Johnny Marr, por exemplo, pode ser perfeitamente conhecido do Dias sem o Peel ter esse sinal.

Nos 12 primeiros lugares acima, os artistas com votos prévios de faixas passam de **2 para 6 no teto, ou 8 nos bónus**. São artistas como Emma Ruth Rundle, Tinashe, The Avalanches, Teenage Fanclub, Fine e June McDoom.

O resultado apoia uma hipótese limitada: **um sinal pessoal já existente pode ajudar a decidir o que ouvir primeiro**. Não prova que o sistema tenha aprendido o estilo de artistas novos. Conhecer já o artista não invalida descobrir um álbum novo; apenas é uma evidência diferente da que falta na W45.

## 2. Composição da pool completa

Simulámos candidatos elegíveis, com uma reserva até 14. A baseline do simulador reproduz a função de produção de dba7228 em todas as semanas. As identidades são únicas: Fontaines D.C./DC agregam cinco famílias e não ocupam dois lugares.

| Variante | Entradas diferentes da baseline: W41 / W42 / W43 / W45 | Artistas com votos prévios, nos 28 lugares | Primeiras descobertas de álbuns nessa semana |
|---|---|---:|---:|
| Regra atual | 0 / 0 / 0 / 0 | 8 | 9 |
| Teto de 2 famílias | 0 / 0 / 0 / 1 | 8 | 9 |
| Bónus 0,20 | 2 / 1 / 3 / 1 | 14 | 13 |
| Bónus 0,40 | 0 / 0 / 2 / 0 | 9 | 11 |

«Primeira descoberta» é uma data de recolha, não prova de lançamento novo. Os bónus podem trazer mais álbuns descobertos agora e, simultaneamente, favorecer nomes já conhecidos: não confundir essas duas dimensões de descoberta.

**Estas pools não são filas confirmadas.** Na baseline faltam avaliações musicais em quatro dos 28 lugares e prova de escuta histórica em cinco. No bónus 0,20 faltam ambas em sete lugares. Ausência de prova de link não significa indisponível; a resolução e os filtros completos de novidade não foram reexecutados. Não calcular uma taxa de sucesso a partir só dos candidatos que por acaso têm nota.

### O caso W45

- Os bónus mantêm os mesmos seis discos de três famílias no topo: não existe um sinal pessoal positivo que distinga esses artistas desconhecidos.
- Com 0,40, os sete candidatos continuam iguais à baseline atual, incluindo Gyrofield.
- Com 0,20, PISS sobe para o sétimo lugar no lugar de Gyrofield. Só tem um Like prévio de faixa; o álbum não foi avaliado e a sua elegibilidade como álbum continua por confirmar. Não é um acerto demonstrado.
- O teto de duas famílias troca Dis Fig (Meh) por Shovel Dance Collective (Dislike), mantendo Gyrofield e mudando a ordem. Também não demonstra melhoria.

Assim, reduzir consenso por si só não permite afirmar que a W45 passaria a ter álbuns de que o Dias gostasse.

## Guardas e reprodução

```bash
uv run --frozen python scripts/album_consensus_shadow.py > /tmp/peel-consensus.json
uv run --frozen pytest tests/test_album_consensus_shadow.py
```

É necessário ter no Git os commits indicados; o script não faz fetch nem chamadas de rede. Materializa o pacote de código e as DBs dos commits em ficheiros temporários, abre SQLite em `mode=ro`/`query_only` e escreve JSON em stdout. Não depende dos antigos scripts em `/tmp` nem do estado atual da DB real.

As guardas rejeitam votos, observações de faixas/álbuns e cache de géneros posteriores à criação da fila. Os resultados atuais não fazem parte do objeto de candidato e nunca entram no ranking. Links servem apenas de diagnóstico. Todos os controlos de gosto/indisponibilidade ficam separados.

Não foram executados weekly, refresh, sync, resolução Spotify/MusicBrainz, publicação ou Telegram. Não foram alterados o ranking de produção, as filas, a DB ou os relatórios históricos.

## Próximo passo

Investigar **T-W9R8**, sem voltar a ajustar pesos às cegas: medir primeiro que informação editorial/de estilo existe para artistas desconhecidos e se permite um sinal pessoal útil. Manter as quatro variantes como hipóteses documentadas, não como fórmulas validadas.

A W46 será a primeira observação após as duas correções já publicadas. Nenhuma análise posterior foi automaticamente agendada; quando houver avaliações, a tarefa de medição pode ser retomada.
