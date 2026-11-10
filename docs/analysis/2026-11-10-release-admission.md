# Primeira etapa: Bandcamp, datas e formatos

Implementação de 10/11/2026, após autorização do Dias para avançar. Não é uma alteração do modelo de gosto nem uma integração ADM.

## O que muda

- Bandcamp combina os cartões visíveis com `data-client-items`, mantendo a grelha atual primeiro e eliminando duplicados por URL/identidade.
- Até 20 candidatos inspecionados por editora, para devolver até cinco lançamentos elegíveis. Arquivo, datas antigas/futuras, pré-vendas, dados incompletos e falhas não preenchem as cinco vagas de recolha.
- A página de cada lançamento confirma data e formato. Singles/EPs Bandcamp de 1–3 faixas vão para faixas, não para álbuns; o refresh só de álbuns já não os regista como discos.
- Uma coleção de material inédito explicitamente proveniente de um vault/archive, na abertura da descrição, é identificada como arquivo. Não basta uma referência a samples ou à discografia antiga na biografia.
- Novas admissões exigem data de edição dentro dos últimos **35 dias**, inclusive, e já lançada. Datas parciais representam intervalos: não se inventa 1 de janeiro/primeiro dia do mês para as declarar recentes.
- Faixas aproveitam metadados da pesquisa Spotify, sem um pedido extra por candidato. Versões explicitamente remasterizadas e uma edição antiga da mesma gravação, identificada por ISRC nos resultados, impedem falso rejuvenescimento.
- Uma faixa bónus nova não é excluída apenas por o álbum-pai ser deluxe. A heurística de títulos de álbuns não se aplica indiscriminadamente a títulos de singles.
- As filas novas também verificam pendentes antes do preenchimento. Pool de consultas limitada a `max(14, 2 × vagas livres)`; zero consultas de backfill quando as novidades já preenchem as 28 vagas. A fila pode ficar menor, sem enchimento com arquivo ou lançamentos não confirmados.
- Álbuns verificam o link Spotify/Bandcamp. Um single/EP Spotify curto não ocupa uma vaga de álbum, mas um LP explicitamente identificado pelo Spotify não é excluído apenas por ter poucas faixas longas. Compilações/arquivo identificados ficam de fora.
- Falhas de metadados de lançamento não inventam elegibilidade. Falhas/ambiguidade MusicBrainz continuam a não excluir, por si só, um lançamento de outra forma elegível.
- `albums refresh --dry-run` avisa que a pré-visualização não confirma os metadados finais de todos os links. `--fetch` pode verificar dados na recolha, sem equivaler à validação de todos os candidatos.

Não se alteraram a fórmula de scoring, os escalões de afinidade, a precedência do consenso ou as regras dos votos/controlos. A pool elegível e o orçamento de resolução mudam, não a função de preferência.

## Pendentes e disponibilidade

Não se apagam pendentes antigos nem se reescrevem os seus votos. Um pendente fora da janela ou sem metadados confirmados deixa de servir para preencher uma **nova** fila, mas continua acessível na sua semana histórica.

Faixas futuras/sem data confirmada são guardadas no mecanismo existente de retry para não desaparecerem quando a notícia sai do feed. Isso não cria votos, bans ou vagas ocupadas. O contador de falhas de pesquisa da execução permanece distinto. O total do ledger `unmatched` continua a entrar no sinal de disponibilidade da fórmula de scoring existente; não representa desaprovação musical. Não foi introduzida uma nova tabela ou migração de estado.

## Verificação real, só de leitura

Cinco editoras Bandcamp ativas consultadas. A recolha recuperou **12 candidatos datados**, sete encaminhados como álbuns e cinco como faixas. Isto não são 12 acertos, 12 artistas novos nem uma fila entregue; faltam as restantes verificações de seleção/arquivo e feedback pessoal.

- Sub Pop: Twisted Teens — *The Holy Cross Tigers*, PISS — *you made me / skin on linen*, Shabazz Palaces — *It's a Style Sport*; Hannah Jadagu — *Curse* e waterbaby — *Creek* como faixas.
- Sacred Bones: Jozef Van Wissem & Jim Jarmusch — *Bocca di Lupo and Mouth of the Wolf*.
- Stones Throw: N8NOFACE — *Tomorrow's World*; Sudan Archives e Automatic como faixas.
- Ghostly: Sylvie — *New Season* e Goya Gumbani — *Bean Pie*; Nabihah Iqbal como faixa.
- DFA: nenhum lançamento elegível encontrado no orçamento de inspeção. Não se preencheu com catálogo antigo.

The Gits — *Etcetera* tem uma data de edição recente, mas a própria descrição declara uma coleção de material inédito selecionado do arquivo pessoal. Foi excluída; a data da edição não a torna gravação nova.

A pesquisa Spotify de Kiosk — *Baby Teeth* forneceu data de 21/10/2026 e ISRC sem consultas adicionais de álbum. O filtro de backfill, em consulta Spotify direta, aceitou essa faixa e reteve Pigeon — *NRG*, datada de 01/05/2026. O EP *At Work in the Fields of the Bomb*, dos Deafheaven, foi identificado como single/EP de três faixas, não LP. Nenhum voto foi alterado.

Evidência estruturada: `docs/analysis/data/2026-11-10-release-admission.json`.

## Testes e integridade

- **810 testes em modo plain e 810 em modo ANSI**, além de lint/formatação.
- Fixtures de candidatos Spotify passam a fornecer datas explícitas; não se afrouxou a validação para acomodar os mocks antigos.
- Os testes de integração de `main` deixam de depender de feeds reais: só usam fontes/fixtures configuradas explicitamente. As fixtures antigas de publicação continuam úteis, com datas simuladas de lançamento explícitas e independentes.
- Testes cobrem grelha/continuação divergentes, deduplicação, dados inválidos, orçamento de pedidos, preenchimento após exclusões, arquivo declarado, singles em `/album/`, datas parciais, pré-vendas, retry, backfill, few-track LPs e ausência de votos criados.
- Verificação de hashes do código/testes antes e depois da suite final: nenhuma alteração funcional posterior ficou sem teste.
- `data/peel.db` manteve SHA-256 `737f75a2bb25cf3c71fb50f5adfdf5790a666c0bc4d61d602ae94236ee41b37e`.
- `data/reports/2026-W45.md` manteve SHA-256 `fd1daeb2c75a98ec47155e06fb1dd9ff215daf14b8dff877ddfe179c9b25f47e`.
- Sem execução semanal, reconstrução W45, escrita Spotify, Telegram ou publicação Sept. As consultas reais limitaram-se a leitura.

Artefactos completos: `/tmp/peel-release-fix-6sNVF9/`, incluindo backups, hashes, logs plain/ANSI e probes. Os testes automatizados e fixtures estão no repositório.

## Limites e próximo passo

Datas são da edição devolvida pelo fornecedor, não uma prova universal da primeira publicação de cada gravação. Reedições silenciosas ou arquivos não identificados podem escapar. O orçamento de inspeção também não cobre todo o catálogo de cada editora. Melhor cobertura/novidade não demonstra, por si só, melhor gosto pessoal.

ADM **ainda não está integrado**, nem há novas curadorias ativas. Segue-se o teste como radar/índice de críticas, sem voto independente extra, nota pessoal automática ou mínimo obrigatório de cinco críticas. A captura completa e prospetiva de candidatos não selecionados também continua por implementar; não a confundir com snapshots já existentes das filas entregues.

A primeira fila posterior a `dba7228` já conterá estas alterações de admissão. Uma avaliação W46 mede o conjunto entregue, não isola causalmente a correção de identidade/diversidade. O teste prospectivo de ranking sugerido pelo Claude não foi iniciado nem agendado.
