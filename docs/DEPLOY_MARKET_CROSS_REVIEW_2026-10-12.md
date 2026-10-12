# Auditoria de deploys, baseline, JEV e mercado — 11/12 de outubro de 2026

## Conclusão

A engine permaneceu sem novas ações live porque nenhuma decisão da estratégia `baseline_jev` terminou em LONG ou SHORT. O bloqueio ocorreu antes do executor. A baseline produziu sinais, inclusive SHORTs, mas o JEV vetou ou a avaliação expirou/falhou. No último deploy, a maior parte das chamadas é explicitamente sombra e nunca pode promover uma baseline recusada.

O último período observado teve recuperação de preços pequena demais para os alvos atuais: nos 221 candidatos sombra com 60 minutos completos, nenhum alcançou sequer o deslocamento mínimo de 0,75% exigido por stop de 0,25% e alvo de 3R. Isso sustenta a abstenção nessa janela, mas não valida a estratégia em outros regimes.

A janela anterior contém dois SHORTs que merecem auditoria do plano original: ETH 20:56 UTC e SOL 21:06 UTC. A reconstrução aproximada por candles atingiria seus alvos. Também houve 13 sinais expirados, com um pico de carga de CPU durante a queda.

## Escopo e limites

- Corte fixo: 12/10/2026 00:41 UTC, equivalente a 11/10/2026 21:41 em Brasília.
- Janela principal: 11/10 17:24 UTC até esse corte; os quatro deploys mais recentes da engine.
- Render: 12 deploys de engine e oito de gateway consultados. Os quatro recentes terminaram com sucesso; as versões antigas aparecem como `deactivated`, situação normal após substituição.
- Logs coletados por paginação: 4.322 eventos comuns de decisão, 66 eventos acionáveis, 44 vetos detalhados e 370 avaliações sombra.
- Removida duplicação por estratégia, símbolo e minuto em sobreposição de deploy. Resultado: 2.179 decisões por estratégia, 4.358 no total.
- Instâncias atribuídas aos commits pela sequência de inicialização e pelos horários dos deploys.
- Candles Binance de 1 minuto dos cinco símbolos; somente candles fechados até o corte. A API e o significado dos campos seguem a [documentação oficial Binance](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market).
- Preço de referência para comparações: fechamento conhecido imediatamente anterior ao horário do log. A vela que contém a decisão não entra na janela futura, pois mistura preços anteriores e posteriores ao sinal.
- Não foram obtidos os planos completos e os snapshots do Neon. O endpoint privado exige autenticação; o dashboard informado no histórico ainda não está identificado nesta auditoria. O banco local aponta para outro PostgreSQL no Render; a consulta MCP a esse banco também falhou por TLS/acesso.
- Retornos, MFE e reconstruções são observações de preço, não simulação de fills, PnL ou garantia de execução. As janelas se sobrepõem e não representam amostras independentes.
- Nenhum parâmetro de trading ou serviço foi alterado nesta auditoria.

## Evolução dos deploys

Horários nesta tabela em UTC. Em Brasília, subtrair três horas.

| Commit | Engine live | Mudança principal | Baseline avaliada | Sinais baseline | Resultado JEV |
|---|---|---|---:|---|---|
| `63c2e06` | 17:24:33 | Documentação do banco efetivo | 155 | 11 LONG, 0 SHORT | 11 vetos; nenhuma ação |
| `0c30e90` | 17:56:52 | Contexto normalizado e veto detalhado | 50 | 0 LONG, 5 SHORT | 5 vetos; nenhuma ação |
| `f05050f` | 18:06:34 | Resumo determinístico Binance | 1.234 | 1 LONG, 32 SHORT | 25 vetos, 2 timeouts, 13 expirações; nenhuma ação |
| `e46aab5` | 22:13:43 | Revisão sombra de baseline recusada | 740 | 17 LONG, 0 SHORT | 14 vetos de sinais válidos, 3 timeouts; mais 370 chamadas sombra |

No `f05050f`, seis das 33 oportunidades acionáveis da baseline coincidiram com expiração da avaliação JEV. As outras sete expirações ocorreram onde a baseline também recusou. Isso explica por que a coluna de expirações não deve ser somada às chamadas JEV como se todas fossem pedidos enviados ao modelo.

No último deploy:
- 740 leituras de baseline: 723 NO_TRADE e 17 LONG.
- Os 17 LONG válidos tiveram 14 respostas vetadas e três timeouts.
- 370 candidatos adicionais foram avaliados em sombra: 306 LONG e 64 SHORT.
- Destes, 310 tiveram resposta vetada e 60 falharam por timeout.
- Total de avaliações que tentaram JEV: 387; 324 respostas e 63 timeouts, taxa de falha de 16,3%.
- Nenhuma confirmação ativa ou sombra até o corte.

Os commits do gateway também ficaram live com sucesso. O Dockerfile desses dois serviços publica somente a aplicação Python. Portanto, o sucesso desses deploys não comprova que o Next.js do dashboard foi publicado com as mesmas mudanças.

## O que ocorreu no mercado

Entre 17:24 e 00:41 UTC houve alta inicial curta, queda até aproximadamente 21:16 UTC e recuperação parcial. A comparação apenas entre início e fim esconderia a queda e o repique.

| Símbolo | Preço inicial | Mínima | Horário mínima UTC | Preço final | Retorno total | Retorno após 22:14 UTC |
|---|---:|---:|---|---:|---:|---:|
| BTCUSDT | 83.992,69 | 83.276,75 | 21:17 | 83.930,01 | -0,075% | +0,356% |
| ETHUSDT | 2.546,35 | 2.515,84 | 21:16 | 2.545,82 | -0,021% | +0,466% |
| SOLUSDT | 112,25 | 110,00 | 21:16 | 111,48 | -0,686% | +0,441% |
| BNBUSDT | 756,04 | 750,15 | 21:16 | 754,13 | -0,253% | +0,162% |
| XRPUSDT | 1,4229 | 1,3974 | 21:16 | 1,4124 | -0,738% | +0,377% |

A alta inicial gerou sinais LONG da baseline perto das máximas: por exemplo, SOL às 17:36 UTC com score 0,686. Nas próximas 60 velas completas, o fechamento de referência perdeu aproximadamente 0,471%, enquanto o máximo favorável foi somente 0,169%. A recusa do JEV evitou perseguir esse topo segundo essa comparação.

O maior SHORT da baseline apareceu em SOL às 21:17:53 UTC, score 0,760, logo após a mínima. A direção SHORT perdeu aproximadamente 0,991% nas 60 velas seguintes. BTC, ETH e XRP também emitiram SHORTs fortes ao redor do fundo. Isso evidencia que aumentar score não equivale a melhorar entrada: indicadores de tendência/momentum podem confirmar movimento já avançado.

## Por que a baseline quase sempre recusou

No último deploy, os motivos observáveis das 723 recusas incluem:

| Motivo | Leituras |
|---|---:|
| BELOW_THRESHOLD | 701 |
| CLASS_DIVERGENT | 255 |
| CLASS_FLOW_AGAINST | 168 |
| LOW_LIQUIDITY | 176 |
| CLASS_SINGLE_DRIVER | 56 |

Os motivos se sobrepõem. O log comum registra apenas os primeiros quatro, então os números são contagens observáveis, podendo subestimar motivos que ficaram depois do quarto.

O score é uma soma ponderada de tendência, momentum, volume, fluxo e estrutura. Os pesos direcionais somam 0,90, e o corte habitual é 0,55. Volume abaixo da média recebe magnitude direcional zero; volume/fluxo ausentes não transferem seu peso aos demais componentes. Tendência usa contexto lento de 15m além do estado de 1m. Assim, movimentos modestos ou contraditórios dificilmente superam o corte.

Há uma exceção importante: reclaim confirmado usa corte 0,40. Portanto, não é correto dizer que todo sinal live exige score de 0,55. O ETH SHORT com score 0,487 às 20:56 é compatível com essa exceção. A classe exibida pode priorizar CLASS_ALIGNED/BREAKDOWN, e o log truncado não revela todos os campos usados na confirmação.

LOW_LIQUIDITY não é sempre um bloqueio isolado absoluto. O código faz bloqueio duro por BAD_SPREAD/HIGH_VOLATILITY, recusa determinadas classes e aplica o score. O motivo de liquidez pode coexistir com um sinal recusado por outra condição.

Código: [baseline_score.py](../apps/engine/app/strategies/baseline_score.py), [baseline_weights.yaml](../packages/configs/baseline_weights.yaml).

## O que o JEV avaliou e por que recusou

O prompt pede probabilidade de atingir o alvo antes do stop em até 60 minutos quando há um plano. Não é a probabilidade de qualquer subida/queda nem a confiança absoluta no score da baseline.

Nas 310 respostas sombra:
- continuação média: 34,49%;
- mínima: 25%;
- máxima: 47%;
- corte: 55%;
- 221 casos têm uma hora futura completa;
- 151 desses 221 terminaram na direção candidata, mas a média de retorno direcional foi apenas +0,034%;
- máximo favorável observado entre esses casos: 0,367%;
- zero atingiram deslocamento favorável de 0,75%.

Direção correta com movimento pequeno não paga automaticamente o plano proposto. O resumo Binance frequentemente dizia BULLISH/BEARISH, mas resume os mesmos inputs: não produz probabilidade de alvo, não é evidência independente e não autoriza ordens. Qualidade 1,0 mede completude dos campos, não acerto.

Além disso, candidatos sombra continuam NO_TRADE mesmo se o JEV confirmasse. No último deploy não existiu qualquer confirmação para testar esse caminho em produção.

Código: [real.py](../apps/engine/app/providers/jev/real.py), [runners.py](../apps/engine/app/strategies/runners.py).

## Correção da análise econômica anterior

A inferência anterior de que aproximadamente um terço de probabilidade bastaria era incompleta para estes planos reais. Ela desconsiderava a dimensão dos custos em relação ao stop.

Nos 44 vetos detalhados, a identidade usada pela engine permite recuperar o R:R líquido aproximado:

`expected_net_r = (p - haircut) × (net_rr + 1) - 1`

Com haircut 0,05 e expectativa mínima 0,10R:

`p_mínimo_econômico = 0,05 + 1,10 / (net_rr + 1)`

R:R líquido mediano implícito: aproximadamente 1,326, embora o plano bruto seja 3R. Piso bruto econômico mediano: aproximadamente 52,28%. A diferença para o veto de 55% é bem menor do que a estimativa anterior sugeria.

Nenhum dos 44 planos registrados alcançou expectativa prevista de 0,10R. Cinco foram positivos, mas o maior foi apenas 0,0694R. Mesmo retirar o veto de continuação não liberaria esses planos se o filtro econômico fosse preservado.

O resultado recomenda auditar a taxa real por operação e seu peso no stop, e não reduzir a exigência do JEV indiscriminadamente. Esses R:R foram derivados de números arredondados dos logs; não substituem o plano original.

## Três casos durante a queda

| Caso UTC / Brasília | Baseline | JEV | Expectativa prevista | MFE em 60m | Reconstrução aproximada |
|---|---|---:|---:|---:|---|
| SOL 20:25 / 17:25 | SHORT 0,579 | 33% | -0,3486R | +0,963% | STOP antes do alvo |
| ETH 20:56 / 17:56 | SHORT 0,487 | 33% | -0,3486R | +0,797% | Alvo antes do stop |
| SOL 21:06 / 18:06 | SHORT 0,621 | 36% | -0,2132R | +0,919% | Alvo antes do stop |

Reconstrução: fechamento conhecido como entrada, ATR de candles fechados, swing das sete velas anteriores, amplitude recente e piso de stop atual; alvo 3R conforme identificador do log. Spread, entrada real no segundo do sinal, limites do book, fees e stop móvel não foram simulados.

O SOL das 20:25 é o exemplo de por que olhar somente a queda posterior engana: houve oscilação adversa de aproximadamente 0,387%, suficiente para cruzar o stop aproximado de 0,25% antes da queda.

ETH e SOL posteriores são candidatos a falso negativo do JEV. O alvo de SOL ficou próximo da mínima, tornando a classificação sensível à entrada exata. Não se pode registrar esses casos como lucro perdido confirmado sem consultar o Neon.

## Falhas e latência

- 65 indisponibilidades JEV na janela inteira, todas classificadas como timeout: duas no deploy anterior e 63 no último.
- O código usa timeout padrão de 3s por chamada HTTP. O valor efetivo de ambiente não foi obtido. Existe também timeout de 8s em avaliação de plano e orçamento live configurado de 30s.
- 13 decisões EXPIRED_SIGNAL: 21:15 (5), 21:17 (5) e outras três, todas no deploy anterior.
- Uma ocorrência QUEUE_SATURATED em SOL às 21:18.
- CPU chegou a 0,1428 core às 21:18, frente ao limite de 0,15: aproximadamente 95,2% do limite na média de um minuto.
- Memória ficou em aproximadamente 202–223 MB, abaixo do limite de 537 MB; não há evidência de OOM nessa janela.
- O fluxo faz build de features e escrita durável do snapshot antes de iniciar as estratégias; a avaliação ainda precisa persistir decisões e riscos. Esses passos e processamento de ticks compartilham o orçamento.
- Há evidência de pressão de processamento durante o movimento, mas os logs não discriminam tempo em recepção, features, persistência e fila. Não é possível atribuir sozinho o atraso à CPU ou ao banco.

O sistema recusou seis SHORTs/ações que a baseline considerou válidos por expiração antes da ação final. Nos casos fortes junto ao fundo, a recusa coincidiu com uma reversão posterior e pode ter evitado entrada ruim. Isso não elimina o defeito operacional: em outro movimento, a mesma latência pode bloquear uma entrada válida.

Não foram encontrados eventos live_risk_reject, live_blocked ou live_error na janela consultada. A explicação principal não é ordem recusada pela Binance, saldo ou circuito diário: nenhuma ação passou ao executor live. LONG_DISABLED permanece uma restrição adicional para futuras confirmações de LONG; não foi a causa imediata observada nesta janela.

## Prioridades de melhoria coerentes com a arquitetura

1. **Completar a auditoria no Neon.** Exportar request/response JEV, snapshot, feature_snapshot, candidate_plans, risk e labels por decision_id. Essa é a condição para validar os dois possíveis falsos negativos e custos reais.
2. **Medir a latência por etapa.** Registrar tempo de recepção, build de features, save_snapshot, context_for, espera, chamada JEV e persistência. Reduzir recomputações e trabalho síncrono sobre o loop. Comparar p95/p99 com o orçamento live.
3. **Reduzir timeouts JEV.** Medir distribuição de resposta e ajustar concorrência/timeout dentro do orçamento de frescor. Avaliações sombra devem ter prioridade inferior à entrada ativa para não competir com sinais executáveis.
4. **Auditar custos e geometria.** Conferir maker/taker efetivo, juros, slippage e custo/stop. Um 3R bruto com 1,33R líquido muda fortemente o ponto de equilíbrio.
5. **Avaliar timing por classe.** Medir score e distância ao início do rompimento, excursão já percorrida, pullback e regime. Separar entradas precoces dos sinais fortes emitidos depois da maior parte do movimento. Validar fora da amostra.
6. **Alinhar horizonte com execução.** O JEV prevê alvo antes do stop em 60m, mas time_exit_enabled=false e a execução move stop em 1R/2R. Persistir rótulos tanto do plano fixo de 60m quanto da política real; são resultados diferentes.
7. **Versionar evidência e interface.** Registrar commit/hash da configuração na decisão e versão do payload/prompt, pois os campos enviados mudaram mantendo o nome do prompt. Publicar o dashboard separadamente se necessário; o deploy do contêiner Python não publica Next.js.

Não há evidência para liberar LONGs, baixar o score ou o JEV apenas para produzir trades. A primeira prioridade prática é corrigir cobertura/latência e verificar os planos originais; a segunda é calibrar seleção e custos com uma amostra por versão.

## Artefatos reproduzíveis

Os arquivos locais de evidência ficam em `exports/audit-2026-10-12/` (ignorados pelo Git):

- `deploys.json`: respostas dos deploys;
- `logs.json`: registros usados no funil;
- `evidence.json`: filas, expirações, erros e métricas;
- `*-klines.json`: candles fechados Binance;
- `summary.json`: agregações por versão e mercado;
- `crossed_candidates.csv`: 414 avaliações com referência de preço e observação futura;
- `veto_economics.json`: reconstrução dos pisos econômicos;
- `three_cases_geometry_proxy.json`: barreiras aproximadas dos três casos;
- `analyze.py`: análise reproduzível, somente leitura de endpoints públicos.

A ausência de históricos privados completos está explicitamente preservada nas conclusões.

## Ajustes operacionais após a auditoria

O ajuste solicitado foi implementado preservando os critérios econômicos e de risco:

- O agendador JEV reserva três chamadas simultâneas para candidatos ativos e uma para pesquisa live em sombra. A pesquisa é adiada quando há sinal ativo pendente ou outra pesquisa em execução; o adiamento não é registrado como falha do provedor.
- A leitura sombra de cada símbolo é espaçada por pelo menos 120 segundos. Ela continua obrigatoriamente NO_TRADE quando a baseline original recusou.
- Timeout de avaliação ativa de 6s e sombra de 4s, limitados pelo prazo restante do sinal e com reserva de 2s. O adaptador HTTP respeita esse timeout por chamada. O orçamento live e as verificações de book/drift continuam ativos.
- O rotulador só percorre sinais pendentes quando algum horizonte vence. Ele conserva o primeiro preço observado depois de cada horizonte, por símbolo.
- A baseline sem JEV deixa de montar o contexto externo; o contexto continua disponível à estratégia baseline_jev.
- Snapshot e features são gravados na mesma transação, com flush do pai antes da feature. A barreira de persistência durável anterior à decisão live permanece.
- Cada decisão registra commit e tempos de build de features, persistência inicial, contexto e avaliação. O log decision_latency adiciona o tempo de persistência da decisão. Sinais expirados são recusados antes da construção do contexto.
- O log comum passa a registrar todos os motivos; leituras adiadas recebem efeito shadow_deferred e motivo explícito. A interface inclui o rótulo SOMBRA · ADIADA.

Validação local: 261 testes passaram; verificação de tipos do frontend passou. Os testes incluem prioridade de chamadas, capacidade reservada, cancelamento, sampling sem bloquear sinal ativo, respeito ao orçamento de frescor, ausência de chamada quando há adiamento, fidelidade dos rótulos futuros e transação única de snapshot/features. A cobertura de testes não demonstra redução de timeouts em produção; essa melhora deve ser medida nos novos logs.
