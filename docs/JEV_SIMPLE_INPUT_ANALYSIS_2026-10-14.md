# Baseline simples e dados essenciais para o JEV

Implementação local concluída após a análise, conforme o pedido posterior de ajuste. A seção final registra as mudanças e a validação; a publicação depende do próximo push.

## Escopo corrigido

O pedido do usuário é simplificar a baseline e o conteúdo de `decisions`, mantendo os dados úteis para a avaliação do JEV. A restauração ampla de `33c0f05` excedeu esse objetivo ao alterar também geometria, prazo e filtros. Este documento analisa o contrato de entrada. Não publica outra política nem altera execução, alvos, stops, prazos, tamanho ou circuitos.

## Dados examinados

- Código ativo da baseline, features, normalização do JEV, perguntas e combinação das respostas.
- Detalhes de **30 decisões que originaram as 30 entradas live** disponíveis no extrato capturado em 14/10. Todos os snapshots foram recuperados, incluindo perdedores.
- Saídas: quatro `TARGET`, 14 `STOP`, oito `TIME` e quatro `PROTECTION_FAILED`. Seis resultados foram positivos e 24 negativos. Excluindo falhas de proteção de ambos os sinais financeiros, restam cinco positivos e 21 negativos.
- As primeiras 20 leituras da versão `33c0f05` recuperadas após o deploy estavam como `idle`, sem candidato válido. Não há nesse recorte uma chamada nova ao JEV que permita avaliar a qualidade das respostas do contrato restaurado.

São observações de operações executadas sob várias versões, não um experimento controlado. A amostra não permite declarar um conjunto de indicadores como o mais rentável. Os quatro encerramentos por proteção precisam permanecer no resultado financeiro, mas não são desfechos normais para selecionar indicadores de mercado.

## Problemas concretos

1. **Sete componentes não significam sete votos direcionais.** Tendência, momentum, volume, fluxo e estrutura participam do score. Volatilidade e liquidez têm peso zero e atuam como condições de mercado. Isso é coerente: baixa volatilidade ou spread pequeno não indicam compra em vez de venda. Dar peso positivo a esses dois componentes enviesaria o score para LONG.
2. **O volume histórico pode inverter a evidência.** Na v1, volume relativo menor que um multiplica o sinal do fluxo por uma magnitude negativa. O BTC vencedor teve fluxo comprador de `+0,6796`, mas `volume_score=-0,3571`, porque seu volume relativo era `0,4643`. A correção simples é atividade fraca retirar confirmação, sem criar uma direção oposta. A v2 já contém essa correção.
3. **O fluxo misturado pode esconder conflito.** O XRP vencedor era SHORT, com fluxo executado de 1m `-0,0906` e de 5m `-0,3519`. O desequilíbrio momentâneo do book era `+0,4269`; o score misturado de fluxo ficou `+0,0647`. Mostrar os fluxos executados diretamente preserva a leitura que essa mistura esconde.
4. **Clipping perde magnitude relevante.** O score de estrutura satura em -1/+1. Ele não distingue um preço pouco além do rompimento de outro que já avançou vários ATRs. Distância ao nível rompido em ATR acrescenta informação sobre extensão, sem adicionar outro indicador complexo.
5. **`baseline_confidence` não é probabilidade.** É o módulo do score determinístico, sem calibração. Não deve ser apresentado ao JEV como chance de acerto nem duplicado junto com scores, classe e labels como se fossem evidências independentes.
6. **A pergunta precisa descrever o evento.** O contrato histórico pergunta se o candidato chega ao alvo antes do stop, mas não informa as distâncias desses níveis. É necessário transmitir a geometria vigente e a regra temporal real, sem alterar essa geometria para obter uma resposta maior.

## Sete componentes a manter

| Componente | Informação compacta para o JEV | Papel na baseline |
| --- | --- | --- |
| Tendência | Alinhamento das médias e inclinação da EMA20 | Direção e força da tendência |
| Momentum | RSI14 e ROC de 10 candles de 1m | Força e extensão do movimento |
| Volume | Volume do candle relativo à média dos 20 anteriores | Participação; volume baixo não inverte direção |
| Fluxo executado | Desequilíbrio taker de 1m e 5m, separados | Pressão realizada e sua persistência |
| Estrutura | Evento explícito `BREAKOUT/BREAKDOWN/NONE` e extensão em ATR desde o nível | Situação do preço e distância já percorrida |
| Volatilidade | ATR14 em percentual e amplitude de 60m em percentual | Escala do movimento e contexto para o alvo |
| Liquidez | Spread em bps, proveniente de book válido | Custo imediato e qualidade de execução |

Essas informações já existem nas features ou são razões simples entre valores existentes. Não exigem outro provedor, outra IA, novos indicadores de 4h ou pesquisa externa. O snapshot deve usar candles fechados e dados disponíveis no instante da decisão. A [documentação oficial da Binance](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md) descreve volume, volume comprador taker e identificação do fechamento do candle.

O fluxo de 1m e de 5m não são provas independentes: as janelas se sobrepõem. O de 5m serve para distinguir um impulso recente de pressão persistente. Na amostra, o fluxo de 1m favorecia o candidato em **30/30 entradas**, incluindo perdedoras. Portanto, concordância de fluxo não comprova vantagem por si só e não deve virar outra promessa de assertividade.

## Contrato mínimo recomendado

1. Identificação: símbolo, mercado, lado candidato e período do candle.
2. Os sete componentes acima, com nomes e unidades explícitos. Não enviar também o vetor completo de scores e todas as suas features duplicadas.
3. Descrição da operação: distância percentual do alvo e do stop, mais a regra temporal que a execução realmente utiliza. Se fecha apenas posições sem lucro líquido após 60m, esse fato precisa aparecer; `60m` não pode ser apresentado como prazo máximo de todas as posições.

O exemplo local `exports/decision-input-analysis-2026-10-14/proposed-request-state-eth.json` usa o snapshot anterior à entrada do ETH vencedor e o plano que constava no pedido daquele momento. É uma ilustração do contrato, não nova ordem ou reprodução de sua rentabilidade. Nenhum resultado futuro, preço de saída ou probabilidade gravada foi colocado nas evidências. Valores `_pct` são percentuais; fluxo taker é assinado em [-1,1], positivo para compradores.

Na implementação, a descrição do plano deve ser desacoplada de `cost_aware_target_floor`: informar alvo/stop ao modelo não pode reativar extensão de alvo, filtros de amplitude ou produzir outro contrato de saída. Para a saída temporal baseada em lucro líquido, o evento deve ainda especificar o ponto de equilíbrio por custos que a execução usa, ou ser tratado explicitamente como uma previsão de mercado distinta do resultado financeiro. Não assumir prazo fixo nem custo zero.

## O que retirar do pedido padrão

- `baseline_confidence`, classe genérica de alinhamento e listas extensas de labels quando repetem os mesmos fatos.
- Preços de todas as médias, todos os retornos em vários prazos e indicadores equivalentes de volume enviados simultaneamente.
- Resumo do Binance Agent junto com seus próprios dados de origem.
- On-chain, notícias e sentimento diário no contrato padrão de 1m enquanto não houver evidência de contribuição neste sistema.
- Blocos `UNKNOWN`, campos ausentes preenchidos com zero e explicações sobre fontes indisponíveis.
- Piso exigido para aprovar, tamanho da posição, saldo da conta e confiança desejada. Esses dados não são evidência de que o preço alcançará um nível.
- `expected_net_r`, haircut, R:R e probabilidade de equilíbrio apresentados como indícios de sucesso. A decisão de custo/risco pertence ao motor; ao JEV basta a geometria necessária para definir o evento.

Não apagar ausência genuína: valores opcionais desconhecidos devem ser omitidos ou marcados como desconhecidos, com semântica consistente. Falta de fluxo não equivale a fluxo equilibrado. Dados essenciais inválidos ou atrasados devem resultar em motivo claro de indisponibilidade, sem inventar valores.

## Perguntas e responsabilidade de cada etapa

Manter três respostas curtas: chance do candidato atingir o alvo antes da invalidação, chance de reversão contra o candidato e chance de falha do rompimento identificado. A primeira precisa obedecer ao plano e à regra temporal fornecidos; as outras devem ter eventos definidos e não ser tratadas como probabilidades complementares que somam um.

Quando não houver rompimento, o terceiro resultado é inaplicável à decisão; não inventar um evento usando `CLASS_ALIGNED`. O motor mantém a regra existente que ignora falso rompimento nesse caso. A baseline propõe o lado; o JEV avalia esse candidato; o motor verifica custos, tamanho, proteção e autorização da entrada.

Não aumentar probabilidades nem reduzir cortes para produzir mais trades. Também não converter os exemplos vencedores em gates obrigatórios: o BTC vencedor não tinha rompimento e tinha volume relativo abaixo de um; o BNB vencedor não tinha fluxo de 5m disponível. Uma regra rígida inventada a partir de poucos exemplos descartaria esses casos.

## Como demonstrar melhora

Comparar contrato atual e contrato compacto sobre os mesmos snapshots e a mesma operação proposta, registrando versão e respostas reais, sem ordens adicionais. A comparação deve medir disponibilidade, taxa de chamadas/erros, estabilidade, calibração do evento especificado e resultado líquido em observação completa; aumento da probabilidade média ou de aprovações não é evidência suficiente.

Avaliar em sequência temporal, preservando oportunidades vetadas e separando falhas operacionais de desfechos de mercado. A [documentação de validação temporal](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html) explica por que misturar futuro e passado produz uma avaliação inadequada. Não usar as mesmas 30 entradas para escolher pesos e alegar validação de desempenho.

Recomendação: conservar a baseline determinística com cinco componentes direcionais e duas condições de qualidade; corrigir a inversão do volume; enviar ao JEV as sete observações compactas e a definição real da operação. O objetivo verificável é informação clara, completa para o evento e sem duplicação. Ganho de rentabilidade continua dependendo de validação futura.

Durante a análise inicial não foi alterado código de decisão nem configuração de produção.

## Implementação local concluída

- Novo `jev_input_profile: simple_market_v1`, independente de `jev_request_profile`, que continua controlando a política de veto/tamanho existente. O prompt efetivo é `jev_simple_market_v1`; ambos aparecem no status e no log de inicialização.
- Ativada `baseline_first_ten_v2`: somente o voto de volume fraco muda para neutro. Os outros seis componentes, pesos e corte absoluto de 0,45 permanecem iguais.
- O corpo HTTP real contém identificação, sete grupos de evidência e a descrição curta da operação. Não envia o vetor de scores, a suposta confiança probabilística, classes/labels duplicados, resumo do Binance Agent ou inteligência externa.
- Alvo e stop são observados pela função de geometria existente e enviados como distâncias percentuais estimadas. `observation_plan` é uma descrição para o modelo, separada de `jev_trade_plan`; ela não é usada pelo motor para substituir níveis ou gravar outro prazo na posição.
- A regra temporal distingue prazo explícito, alvo/stop sem prazo e revisão de posições sem lucro líquido. O ponto de equilíbrio estimado por comissões acompanha a revisão, usando a mesma fórmula do gerenciamento atual. Não apresenta 60 minutos como prazo máximo de uma posição vencedora.
- Não se monta contexto externo nesse caminho. Um gate de um provedor externo ausente não pode vetar dados exclusivamente da Binance. Dados essenciais inválidos geram `JEV_INPUT_UNAVAILABLE`, com causa em `input_error`, antes de chamar o provedor. Campos opcionais desconhecidos são omitidos, mantendo zero quando ele é uma observação real.
- As três respostas do JEV permanecem sem transformação. `NONE/UNKNOWN` não fabrica evento de rompimento. Mantidos os vetos, a redução de tamanho histórica nos rompimentos e a marcação de probabilidades não calibradas.
- `risk.yaml` e `fees.yaml` permanecem idênticos aos do commit `33c0f05`. Não houve mudança de alvo, stop, tempo, taxas, proteção, dimensão de risco ou circuito de perdas nesta implementação.

### Validação

**404 testes Python passaram.** Os casos novos verificam o corpo HTTP, sete componentes, respostas sem transformação, dados desconhecidos, ausência de dependência externa, prazo explícito e revisão sem encerrar vencedora. Com a mesma resposta simulada e a mesma baseline, representações antiga/nova produzem quantidade, entrada, alvo, stop, risco e taxas iguais. O risco ainda recusa R:R insuficiente e circuito de perdas aberto.

Replay de **835 snapshots**: 192 candidatos válidos, cada um encaminhado uma vez ao provedor simulado, zero entradas indisponíveis por erro do novo formato. O perfil v1 anterior gerava 181 candidatos nesses mesmos dados; a diferença de 11 decorre da correção do volume. Em 496 leituras o valor do componente de volume mudou; nenhum dos outros seis componentes teve alteração de fórmula ou resultado. Os 30 candidatos de entradas históricas também puderam ser serializados com o novo contrato.

Esse replay verifica scoring e encaminhamento, usando probabilidade sintética fixa. Não representa respostas novas do JEV nem desempenho financeiro. A qualidade preditiva precisa ser observada após publicação.

Artefatos locais em `exports/decision-input-analysis-2026-10-14/`: `verify_implementation.py`, `implementation-verification.json`, `implemented-state-eth.json` e `pytest-final.txt`. O exemplo implementado usa as regras de geometria atualmente vigentes sobre o snapshot histórico do ETH; ele não reproduz a variante de alvo estendido que existia na entrada original.
