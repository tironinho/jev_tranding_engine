# Reset manual da sequência de perdas live

`LIVE_LOSS_STREAK_RESET_AT` aceita um horário ISO com fuso, no passado. Deixe vazio por padrão. A alteração requer reiniciar o motor e só deve ser feita por solicitação explícita do operador.

O contador de `baseline_jev` considera apenas fechamentos live do dia UTC posteriores a esse horário. Os trades, resultados líquidos, taxas, saldo, perda diária e drawdown permanecem registrados. O limite continua sendo três perdas consecutivas: três perdas novas voltam a bloquear entradas. O horário é fixo; reiniciar ou fazer outro deploy não perdoa perdas posteriores ao reset.

O mesmo corte é usado na avaliação de risco e em `GET /api/engine/status`. O campo `risk.live_loss_manual_reset_at` identifica o corte; `live_loss_reset_at` continua sendo o próximo reset diário quando há bloqueio. Na inicialização, o motor registra `live_loss_streak_reset_loaded` na auditoria antes de iniciar o processamento de mercado. Nenhuma rota pública de reset foi criada.

No Render, alterar somente essa variável do serviço do motor, preservando as demais. Não aplicá-la ao gateway da Binance. Um horário sem fuso ou no futuro impede a inicialização. Manter o mesmo valor não afeta dias posteriores; esvaziá-lo antes da virada UTC restaura a contagem original daquele dia.

Validação: testes cobrem o corte exato, preservação dos prejuízos, contador em risco e status, escopo da estratégia, reabertura do bloqueio após três novas perdas, reinício com o mesmo horário, validação do fuso/futuro e registro de auditoria. O reset remove apenas o bloqueio de perdas consecutivas; os demais critérios de entrada continuam sendo avaliados.
