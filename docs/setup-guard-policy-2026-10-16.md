# Política de entrada após auditoria de 16/10/2026

A produção passa de `jev_break_size_v1` para `jev_setup_guard_v1`. Isso corrige autorização e viabilidade; não representa uma estratégia com rentabilidade ou taxa de acerto já comprovadas.

- Novas entradas LONG live ficam pausadas. O bloqueio ocorre antes da chamada JEV e novamente no risco. Gestão das posições abertas, stops e saídas permanecem ativos.
- Rompimentos respeitam o piso de continuação. Uma resposta abaixo do piso vira veto, em vez de apenas reduzir o tamanho. A exceção antiga só existe na versão histórica explicitamente selecionada para reprodução.
- O gerador de candidatos conserva os scores conhecidos, mas a autorização passa a exigir setup: contexto disponível, estrutura fechada de 5m para continuação e reclaim/reteste sustentado para reversão. Estrutura ausente, vencida, futura, estendida ou perdendo impulso impede a chamada. O risco repete a verificação antes de aceitar a ordem.
- O seletor v2 completo continua em shadow. Este guard não promove seus filtros completos de fluxo, VWAP ou retornos a uma estratégia comprovada. Seus critérios semânticos são registrados separadamente em `setup_guard`.
- `setup_guard_2026_10_16` limita o alvo pela amplitude observada de 60m e pelo obstáculo direcional mais próximo conhecido em 5m/15m. `extend_target_for_costs=false`: se o alvo não cobre o R:R líquido mínimo, o plano retorna `STRUCTURAL_TARGET_BELOW_COST_FLOOR`, sem consultar o JEV. Custos de execução ainda são reavaliados pelo risco.
- O prompt `jev_setup_market_v1` inclui a estrutura de 5m, horário da estrutura, tipo de setup, informações ausentes, alvo/ATR, alvo/amplitude, distância até obstáculo e custo estimado. Não envia resultados futuros. Continua identificando probabilidades como UNCALIBRATED. Elegibilidade não é apresentada como evidência de lucro.

As configurações de prazo e proteção de lucro não foram ativadas: as comparações disponíveis ainda não demonstraram vantagem líquida. O contrato continua descrevendo a saída realmente executada, inclusive TARGET_OR_STOP quando não há prazo. Não foram alterados os limites de perdas, risco ou saldo da conta.

A frequência pode cair em baixa amplitude: rejeitar uma oportunidade que não paga custos é o comportamento esperado. Não foi ativado SHORT-only como estratégia supostamente lucrativa; SHORT apenas continua sujeito às novas verificações.

Os testes históricos usam fixtures explicitamente antigas para conservar reprodução. Os testes de produção verificam o veto abaixo do piso, a pausa LONG, reversão sem reteste, dados vencidos/futuros, plano pequeno rejeitado, request HTTP enriquecido, backstop de risco e um caminho SHORT válido realmente aceito. Há regressão para confirmar que um LONG já protegido não perde seu stop nem é encerrado pela pausa de entradas.
