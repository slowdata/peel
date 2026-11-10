# Segunda opinião: fontes, ADM e seleção

Revisão de 10/11/2026, pedida pelo Dias.

## Âmbito e custo contido

- Claude Code 2.1.322, **Fable 5.2**, esforço **low**, via Claude Pro; modelo e modo confirmados no terminal.
- Uma única consulta, com resumo dos factos e resposta curta; o terminal indicou 26 segundos para a resposta. Não foi obtido um valor monetário ou contagem de tokens.
- Sem ferramentas, MCP, pesquisa, leitura de ficheiros ou subagentes. Instruções de sistema compactas, próprias para esta revisão, em vez do contexto completo da conversa/repositório.
- Revisão independente do raciocínio **sobre o resumo fornecido**, não uma segunda verificação independente dos dados.
- Contexto: auditoria W41–W45 em `2026-11-10-source-audit.md`, limitações do estudo de consenso e inspeção atual do AnyDecentMusic. Nenhuma alteração de produção autorizada ou executada.

## Resposta do Claude

Veredicto: gargalo está nos álbuns, não nas faixas. Faixas 55/77 positivas; álbuns 7/17 e W45 0/7.

  1) Sequência mínima

  Álbuns:
  1. Corrigir ingestão: grelha Bandcamp e validação transversal de data/formato (single em /album/, reedições, pré-vendas). É bug demonstrado, barato, sem risco de gosto.
  2. Congelar snapshot semanal do pool (candidatos, fontes, datas, estado ADM). Sem isto, nenhum teste futuro escapa a leakage.
  3. ADM como radar.
  4. Ranking: só depois de semanas avaliadas pós-correções de identidade/diversidade. Senão, efeitos confundidos.
  5. Clusters: último. Entretanto guardar descrições/excertos por álbum, para dados acumularem a custo zero.

  Faixas: não mexer no ranking. Só validação de datas. No máximo uma curadoria nova (So Young RSS, mais simples), a substituir slots de Consequence dentro das 28.

  2) ADM

  Radar de candidatos e índice de críticas individuais com links originais. Usar Just In/Unlisted; chart exige 5 críticas, logo atrasa e reforça consenso. Não usar como nota: DIIV 7.3 foi Dislike,
  fórmula não verificada, e estudo já sugere consenso sobreponderado. Não contar como família extra; serve para deduplicar famílias.

  3) Menor teste

  Prospetivo e intercalado nos mesmos 7 slots: 4 pelo ranking de produção, 3 por variante (consenso como desempate suave, pool corrigido + ADM). Braço oculto ao Dias na escuta. Regra de decisão
  escrita antes. Métricas, só sobre álbuns ouvidos: taxa de positivos por braço e fração de artistas sem votos prévios. 4–6 semanas dão ~12–18 álbuns por braço: indicativo, não prova. Nenhum álbum não
  ouvido conta como acerto.

  4) Premissa mais fraca

  "Mais ou melhores fontes resolvem álbuns." Afinidade aprende por artista, logo para artista novo vale zero e ranking colapsa em consenso + qualidade de fonte. Mais cobertura alimenta o mesmo seletor
  cego. Dias já rejeitou álbuns de géneros que aprecia: sinal de falha de seleção, não de pool.

  Hipótese barata, não demonstrada: usar faixas como sonda de álbuns. Love/Like numa faixa de artista novo promove o álbum respetivo. Sinal pessoal, transferível, sem escuta extra.

  Fazer agora
  - Corrigir leitor Bandcamp e validação de data/formato.
  - Snapshots semanais congelados.
  - Guardar descrições por álbum.
  - Medir fração semanal de artistas sem votos.

  Adiar
  - Mudança de consenso em produção, até teste intercalado.
  - ADM além de radar.
  - Hard of Hearing, Neu Bulletin, Stereogum AOTW: uma de cada vez, depois da ingestão.
  - Clusters e classificador.

  Não fazer
  - ADM como nota ou família.
  - Tirar conclusões por fonte destas amostras.
  - Aumentar cap como correção de cobertura.
  - Hearing Things pago, contornar API Tia Ho.
  - Avaliar retroativamente com dados ADM atuais.

## Leitura crítica do coordenador

- Concordância útil: corrigir cobertura/data/formato é uma prioridade demonstrável; ADM merece teste como radar e índice de críticas originais, não aprovação automática de gosto pessoal.
- Guardar o pool completo e os metadados **como eram na altura** complementa as filas entregues já congeladas. Não significa que as filas atuais não tenham snapshots ou que os estudos temporais anteriores sejam inválidos por definição.
- A afirmação de que o problema é de seleção, e não de pool, é demasiado forte: há uma falha de cobertura demonstrada e uma limitação de sinal pessoal. Ainda não isolámos os contributos de cada uma.
- O exemplo DIIV não demonstra sozinho que a nota ADM seja inútil; apenas não há validação suficiente para a adotar como gosto pessoal ou ranking principal.
- O teste sugerido de quatro posições de produção e três de variante **não foi aprovado nem iniciado**. Não adotar quotas fixas por rotina. Mistura pool corrigido, ADM e ranking alterado, pelo que não identifica isoladamente o efeito do ADM. Para isso, manter ranking comum e distinguir as diferenças de cobertura/seleção antes de avaliar resultados.
- Correção aritmética: nessa divisão 4/3, quatro a seis semanas dariam **16–24 posições de produção e 12–18 de variante**, não 12–18 em cada braço. Continuam a ser amostras pequenas; repetições não são observações independentes.
- Usar uma faixa apreciada como pista para o respetivo álbum é uma hipótese, não prova de transferência entre artistas. Há já afinidade por artista aprendida de faixas; um sinal específico do álbum teria de acrescentar algo a isso. Não resolve, por si só, o arranque sem feedback.
- Guardar excertos já disponíveis pode ser barato, mas não foi demonstrado custo incremental zero para recolher, normalizar ou classificar descrições.

## Recomendação resultante

**Agora:** corrigir Bandcamp e admissão por lançamento/formato; conservar evidência temporal dos candidatos; experimentar a cobertura ADM sem a tratar como voto independente extra.

**Depois:** testar poucas curadorias específicas e alterações de seleção de forma separável, sem aumentar as 28 faixas/7 álbuns e sem contar desconhecidos como acertos.

**Adiar:** classificador/clusters e novas regras de ranking até haver dados que permitam decidir. Isto não é uma conclusão de que caracterização musical não será necessária.

Nenhum teste futuro ficou agendado. Nenhuma fonte, fila, voto, playlist, serviço ou código de produção foi alterado nesta revisão.
