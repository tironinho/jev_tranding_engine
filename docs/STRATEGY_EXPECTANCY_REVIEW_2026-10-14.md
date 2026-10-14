# Trades, mudanças e expectativa líquida — 14/10/2026

Consulta capturada às 10:12:31 de Brasília (13:12:31 UTC). Foram recuperadas 3.300 decisões de 14/10, 219 detalhes completos de decisões, todos os 35 fechamentos live disponíveis, posições, limites de risco e logs da proteção. A janela da versão `3f7be9a` vai de 03:41 a 10:12 de Brasília. Os dados estão em `exports/strategy-review-2026-10-14/`; `summary.json` contém os cálculos e os identificadores para reprodução. Nenhum resultado financeiro foi reescrito.

## O objetivo econômico

O objetivo é maximizar resultado líquido acumulado, não acertar dez operações. A relação básica é:

`expectativa líquida = p × ganho líquido no alvo − (1 − p) × perda líquida no stop`

Em unidades de risco: `EV/R = p × R:R líquido − (1 − p)`. A probabilidade mínima para empatar é `1 / (1 + R:R líquido)`. Assim, um payoff líquido de 2 exige mais de 33,3% de acerto; payoff 3 exige mais de 25%; payoff 4 exige mais de 20%. Custos precisam estar dentro dos ganhos e perdas, não descontados novamente depois dessa conta.

Exemplo aritmético: três vencedoras de 0,30 USDT e sete perdedoras de 0,10 USDT resultam em +0,20 USDT, mesmo com sete derrotas. Aumentar o número de operações só amplia a oportunidade de lucro se a expectativa realmente for positiva.

O JEV fornece estimativas não calibradas (`UNCALIBRATED`). O desconto de cinco pontos percentuais usado nos cenários abaixo é um teste de sensibilidade, não um limite estatístico de confiança. Alvo, stop, custos e prazo avaliados pelo modelo devem corresponder ao plano executado. Saídas por tempo e execução diferente do plano fazem a distribuição real ir além de dois resultados fixos.

## Resultado financeiro observado

| Amostra | Trades | Vencedores | Perdedores | Líquido USDT | Profit factor |
| --- | ---: | ---: | ---: | ---: | ---: |
| Histórico live disponível, várias versões | 35 | 7 | 28 | −0,576867 | 0,6483 |
| Perfil simplificado após o reset | 5 | 1 | 4 | −0,184032 | 0,4864 |

O histórico agregado tem +0,270365 USDT bruto e 0,847232 USDT em taxas. Não é uma comparação controlada entre estratégias: houve mudanças de dados, geometria, prazos e execução ao longo dos 35 trades.

Nos cinco trades novos, o bruto foi −0,001645 USDT e as taxas 0,182387 USDT. Portanto, as taxas explicam quase todo o prejuízo líquido desse recorte. A vencedora rendeu 0,174308 USDT; as quatro perdas somaram 0,358340 USDT, média de 0,089585 USDT por perda. A vencedora cobriu 1,95 perdas médias. Mantendo esses tamanhos observados, o acerto necessário para empatar seria 33,95%; o observado foi 20%. Cinco trades são insuficientes para estimar um acerto estável.

| Abertura Brasília | Símbolo/lado | JEV | R:R líquido planejado | Saída | Líquido USDT |
| --- | --- | ---: | ---: | --- | ---: |
| 05:13 | ETH LONG | 56% | 1,7513 | STOP | −0,091220 |
| 05:38 | SOL SHORT | 59% | 2,0201 | TARGET | +0,174308 |
| 07:08 | SOL SHORT | 54% | 1,6701 | STOP | −0,096780 |
| 07:48 | SOL LONG | 59% | 1,5484 | STOP | −0,088478 |
| 07:59 | XRP LONG | 57% | 1,5920 | STOP | −0,081863 |

Os cinco planos apresentavam expectativa positiva tanto com a probabilidade bruta quanto descontando cinco pontos percentuais. A média do JEV foi 57%; o desfecho observado foi uma vitória em cinco. Isso não demonstra sozinho que o modelo é mal calibrado, mas impede tratar os 57% como taxa de acerto comprovada.

## Por que há poucas entradas

Das 1.765 leituras após o deploy:

- 1.527 ficaram sem candidato pela baseline (86,5%). Em 1.519 dessas leituras o score estava abaixo de 0,45. Divergência de componentes também apareceu em 635; os motivos se sobrepõem.
- 238 chegaram ao JEV (13,5%): 189 confirmações e 49 vetos.
- As 189 confirmações tiveram os seguintes resultados exclusivos de execução:

| Resultado | Quantidade |
| --- | ---: |
| R:R líquido abaixo de 1,5 | 73 |
| Circuito de perdas consecutivas | 49 |
| Quantidade/valor mínimo da ordem | 45 |
| Posição já existente no símbolo | 14 |
| Outros controles de conta/margem | 3 |
| Entrada autorizada | 5 |

As três recusas de conta continham `MARGIN_LEVEL`, duas combinadas com posição existente; uma também tinha intervalo mínimo de entrada. A telemetria do grupo de valor mínimo não registra contexto suficiente para atribuir cada recusa a caixa, lote ou arredondamento; não se presume uma correção de dimensionamento sem essa evidência.

O motor rejeita em etapas. As 49 recusas pelo circuito não chegaram necessariamente a todos os testes econômicos posteriores; mudar o limite não transforma essas 49 leituras em 49 entradas. Tampouco são 49 oportunidades independentes: há leituras consecutivas dos mesmos símbolos.

## Piso fixo e probabilidade

O perfil ativo usa `min_net_rr: 1.5` e não usa o valor esperado como critério final. Isso pode recusar um trade cuja probabilidade estimada compensaria um payoff inferior ao piso.

Entre as 73 confirmações recusadas por R:R, 28 apresentavam EV positivo usando a probabilidade bruta do JEV; 20 continuavam positivos com desconto de cinco pontos percentuais; nove ainda superavam +0,10R nesse cenário, mantendo os custos e planos registrados. Exemplo real: decisão `e716b961-54cb-4888-bae3-77082933e469`, p=62%, R:R líquido=1,3031; com p=57%, EV=+0,3128R. O piso fixo a recusou.

Isso quantifica uma diferença entre políticas, não o lucro das operações recusadas. Elas não foram executadas, os sinais se sobrepõem e não foi reconstruída uma carteira alternativa com capital, concorrência, todos os fills e observação contínua de alvo/stop. Não se atribui lucro retrospectivo a essas nove leituras.

O BTC das 03:55 ilustra o caso oposto: p=55% e R:R líquido=0,2917 resultam em EV de aproximadamente −0,2896R. Uma política baseada em probabilidade também recusaria esse plano. Um percentual acima de 50% não basta quando os custos reduzem muito o ganho e ampliam a perda.

## O que mudou e o que funcionou

- `e7a7dd6`: proteção baseada no fill líquido e taxas observadas na conta. Os cinco trades novos tiveram stop confirmado e nenhum `PROTECTION_FAILED`. Nos logs, o envio do stop aparece imediatamente após cada entrada. As quatro perdas novas foram stops de mercado, não a antiga falha de proteção.
- `33c0f05`: restaurou a política histórica, incluindo alvo pela amplitude de 60m e revisão após 60m; essa alteração foi mais ampla que apenas simplificar dados. É um fator de confusão na comparação com vencedores antigos.
- `c1492b0`: sete componentes na baseline, volume abaixo da média neutro e contrato `simple_market_v1` compacto. A coleta atual confirma chamadas reais ao JEV nesse contrato. O novo formato ainda não demonstrou melhora financeira com apenas cinco fechamentos.
- `fb98f5f`: apresentação do que entrou no JEV e explicação do bloqueio diário; não muda a expectativa econômica.
- `3f7be9a`: reset com horário fixo, preservando histórico. O motor abriu cinco trades e voltou corretamente a bloquear quando as três perdas mais recentes fecharam.

O SOL vencedor teve rompimento de baixa, fluxo vendedor em 1m e 5m, volume relativo 1,23 e R:R planejado 2,02. Ficou aberto cerca de 89 minutos, pois a revisão não encerra uma posição lucrativa só pelo relógio. O retorno realizado foi 1,84R, abaixo do plano; houve 0,01749 USDT de slippage registrado na saída parcial. Portanto, payoff planejado e realizado precisam ser acompanhados separadamente.

O ETH vencedor antigo, citado como referência, realizou 3,30R depois de aproximadamente 25h19. Seu alvo registrado era 2.524,1174 e o fechamento SHORT ocorreu a 2.502,98, além do alvo em direção favorável. Esse ganho adicional da execução não pode ser atribuído apenas aos sete indicadores nem usado como retorno garantido de uma nova entrada.

Os três LONGs perdedores tinham RSI de 69,69, 67,62 e 77,66, além de inclinação de contexto 15m negativa. Isso sugere estudar entradas após impulsos, mas não autoriza uma regra otimizada nesses três exemplos. O vencedor SHORT também estava contra a inclinação 15m isolada; um filtro simples de concordância teria eliminado esse vencedor. Fluxo, volume ou RSI sozinhos não explicam causalmente o resultado.

Há um resíduo SOL abaixo do mínimo, marcado `RESIDUAL`; a conciliação informa `blocks_entry: false`. Ele não ocupa vaga de posição gerenciada. Não é a causa principal das poucas entradas.

## Alteração solicitada e direção da estratégia

Foi alterado somente o limite de perdas live consecutivas de três para dez, incluindo o valor padrão e o fallback de carregamento. O contador não foi zerado outra vez. Ao publicar, a sequência existente deve aparecer como 3/10, sem bloqueio por essa sequência. Perda diária, drawdown, risco por trade, proteção, lotes e limites de exposição continuam sendo avaliados. Dez é o limite da sequência de perdas, não uma obrigação de abrir dez trades nem um novo limite de posições simultâneas.

A direção coerente com o objetivo do operador é selecionar por expectativa líquida de um plano único, usando probabilidade avaliada para aquele alvo/stop/prazo, custo da conta e margem para erro; medir calibração e payoff realizado por versão; e avaliar cenários alternativos numa carteira com regras de capital e cobertura de observação. O piso fixo de R:R e os limiares da baseline não foram afrouxados silenciosamente nesta alteração. Criar alvos mais distantes apenas para fazer a conta passar não produz vantagem estatística.

O próximo ajuste de política deve comparar EV com o piso fixo e registrar claramente plano, probabilidade, break-even e EV no diagnóstico de execução. O repositório já possui `score_probability`, mas o perfil ativo está com `jev_expectancy_gate: false`; habilitar a configuração antiga junto com `cost_aware_target_floor` também modifica alvos e não equivale a apenas usar probabilidade. Por isso, essa combinação não foi ativada como se fosse uma correção neutra.

Validação do aumento do limite: 415 testes do motor passaram. Os casos cobrem ausência de bloqueio por sequência em 3 e 9 perdas, bloqueio em 10 e 11, reset manual com limites 3/10, preservação do histórico, reinício com corte fixo e demais proteções. Não foi criada ordem real para testar a alteração.

Referência externa para conferência das taxas por conta/símbolo: [documentação oficial de comissões da Binance](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/commission_faq.md). Os custos deste relatório vêm dos registros reais do motor, com origem `binance_gateway`, e dos fechamentos; não de uma tarifa genérica da documentação.
