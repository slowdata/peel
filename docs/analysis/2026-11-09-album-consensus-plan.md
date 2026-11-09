# Plano — consenso limitado na escolha de álbuns

Tarefa: T-20261109-HFJK. Plano registado antes de executar este estudo.

## Pergunta

Um consenso limitado permite dar mais espaço ao gosto pessoal sem transformar a descoberta numa lista de artistas conhecidos?

A análise anterior já examinou estas semanas. Este estudo é **exploratório, não uma validação independente nem um teste em dados nunca vistos**. Não se vão escolher parâmetros por aquele que tiver mais acertos nesta amostra.

## Dados fixos

- Código de referência: `dba7228630db8c883669d87495afcd95635eab48` — já inclui as correções de identidade/diversidade e Meh neutro.
- Snapshots anteriores às avaliações: W41 `d89c442`, W42 `b5d12d6`, W43 `85f53c8`, W45 final `5f9ed2e`.
- Resultados observados: avaliações guardadas em `68e49f236c4cae4f353a79141b007ef06a581b52`. Não entram na escolha histórica; só na avaliação posterior.
- W38 fica excluída por conter avaliações posteriores à criação. W44 fica excluída por não estar completamente avaliada. W45 inicial não é mais uma semana independente.
- As 28 posições observadas representam 27 álbuns distintos: Cameron Winter aparece duas vezes.

## Variantes, sem afinação posterior

Todas conservam a mesma elegibilidade, frescura, consenso por publicação, qualidade das fontes, penalização de diversidade limitada e critérios finais de desempate do código de referência.

1. **`baseline`** — regra atual: famílias, patamar de afinidade, qualidade ajustada, critérios finais.
2. **`cap_2`** — igual, mas o primeiro critério é `min(famílias, 2)`. É uma alternativa conservadora: continua a fazer duas famílias vencerem uma, mesmo com maior afinidade nesta última.
3. **`bonus_020`** — primeiro critério contínuo: `afinidade + 0.20 × (1 − 1/famílias)`; depois a qualidade ajustada e os mesmos desempates.
4. **`bonus_040`** — a mesma fórmula com `0.40`. Serve para observar sensibilidade a um reforço editorial maior, não para escolher retrospectivamente o melhor valor.

Os bónus são inferiores a 0,20/0,40, independentemente do número de fontes. Não dependem da quantidade de avaliações da fonte. Não se mudam os pesos dos géneros nem se adicionam votos de álbuns à afinidade neste teste.

## Duas avaliações diferentes

### Ordem dos álbuns efetivamente ouvidos

Reordenar apenas os sete discos de cada fila observada. Medir positivos nos primeiros três e pares positivo/não positivo na ordem correta. Isto testa **ordenação**, não a seleção de outros discos. A W45 não tem positivos e não permite medir discriminação entre positivos e negativos.

Os pares dentro de uma semana e os discos repetidos entre semanas não são observações independentes; não apresentar significância estatística ou intervalos de confiança a fingir independência.

### Composição da pool completa

Simular os primeiros sete candidatos elegíveis e uma reserva até 14, sem resolver Spotify ou MusicBrainz. Mostrar mudanças face à baseline, artistas com/sem avaliações prévias de faixas, primeiras descobertas e reaparecimentos.

A tocabilidade conhecida vem apenas de links já congelados até à criação daquela fila. É metadado de diagnóstico, **não filtro usado para escolher candidatos**. Falta de prova não significa indisponível. Mesmo um link conhecido não reconstitui a aplicação atual de todos os filtros de novidade.

Esta saída é uma **pool não confirmada, não uma fila final**. Não se atribui uma nota aos discos não avaliados nem se calcula uma taxa de sucesso da seleção a partir só dos que têm nota. Podem existir singles, arquivos não marcados ou candidatos sem resolução.

## Guardas

- Ler código e DBs dos commits Git, em diretório temporário; ligações SQLite `mode=ro` e `query_only`.
- Verificar que os inputs históricos não têm avaliações, observações de faixas/álbuns ou cache de géneros posteriores à criação da fila.
- Confirmar que a baseline do simulador reproduz a pool da função de produção do commit de referência.
- Confirmar identidades únicas em cada pool e assinalar colisões de links conhecidos.
- Não executar weekly, refresh, resolução, publicação, sync ou envio de Telegram. Não tocar na DB real.

## Nota de implementação — prova de escuta

A reconstrução da W45 substituiu a sua snapshot inicial. Para não perder a prova já conhecida de que Gyrofield tinha link, o estudo consulta também `041e312`, cuja fila foi criada antes do refresh. Este commit fornece apenas links anteriores à entrega final, nunca avaliações ou variáveis de escolha; não conta como quinta semana. Isto corrige a cobertura do diagnóstico de tocabilidade, sem alterar as quatro variantes ou os resultados musicais.

## Decisão possível

O resultado pode justificar uma experiência futura delimitada, manter a regra atual ou concluir que faltam dados. Melhor ordem só entre artistas já conhecidos não é prova de descoberta melhor para artistas desconhecidos. A falta de sinal pessoal útil é tratada separadamente em T-20261109-W9R8.
