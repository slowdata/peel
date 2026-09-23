# Reconciliações explícitas

## 23 de Setembro de 2026

As alterações locais de 7 de Setembro ainda não estavam publicadas. Entretanto,
o GitHub executou W37 (11/09) e W38 (18/09). O merge automático de feedback não
serve para esta situação: perderia descobertas, recuperações ou snapshots.

`2026-09-23.json` conserva hashes dos inputs, decisões, observações em conflito
(ambas as versões) e filas históricas divergentes. Os inputs completos ficaram
em backup local ignorado pelo Git; os commits GitHub preservam também os estados
remotos originais.

### Estado escolhido

- **Activa: W38**, 28 faixas na ordem relida no Spotify em 23/09, com os 11 álbuns
  da execução de 18/09. Não houve escrita Spotify, mensagem Telegram nem export
  para o site. A confirmação é de 23/09, não foi retrodatada para 18/09.
- **W36 recuperada:** preservados exactamente os 14 candidatos e 11 álbuns,
  incluindo Interpol e Jungle, bem como a proveniência da recuperação de 7/09.
- **W37 extraordinária:** preservado o snapshot confirmado de 7/09, com 6 faixas
  e 1 álbum. As filas registadas pela execução de 11/09 estão no JSON, e o seu
  relatório original em `../reports/archive/remote-runs/2026-W37.md`. Não se
  fabricou confirmação Spotify para essa execução histórica.
- **W32–W35:** continuam arquivadas, sem avaliações negativas artificiais.
- **W31 e edições públicas:** sem alteração.

### Regras de preservação

- União das descobertas locais/remotas; primeira observação mais antiga e última
  observação mais recente. Datas de descoberta não são datas de lançamento.
- Feedback mais recente vence; empates contraditórios interrompem a operação.
- `source_runs` usa `(source_id, run_at)` como identidade; IDs inteiros de duas
  branches independentes podem colidir e são remapeados sem perder as métricas.
- Os relatórios locais anteriores permanecem byte a byte. Os relatórios remotos
  divergentes W34/W35/W37/W38 estão em `../reports/archive/remote-runs/`; apenas o relatório
  activo W38 foi explicitamente regenerado para corresponder à playlist actual.
  W34/W35 diferiam apenas em entradas unmatched/contadores; ambas as versões
  foram preservadas, sem recalcular retrospectivamente esses relatórios.
- Algumas faixas antigas ainda figuram na W38 produzida pelo código remoto
  anterior. A reconciliação espelha essa playlist, não a modifica. O corte do
  backlog preservado aplica-se à selecção das próximas execuções.

`peel.reconciliation.merge_discovery_history` é uma operação offline para uma
**cópia descartável**, não um novo merge automático. Não escolhe filas, não mexe
em edições finalizadas e não substitui a validação do estado/Spotify antes de
promover a cópia. Qualquer nova reconciliação exige revisão e autorização.
