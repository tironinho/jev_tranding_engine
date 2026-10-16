# Melhorias após auditoria de 15/10/2026

## Implementado

- Seleção de reentrada `post_entry_retest_v1`: rompimento, passagem de tempo e avanço de preço já não bastam no seletor. Requer reteste/reclaim-reteste posterior à entrada ou retomada cujo candle de pullback começou após a entrada. Mantém distância e intervalo mínimos. Datas ausentes, antigas ou futuras não autorizam o setup. Continua em **shadow**, acompanhando `entry_selection_mode`; nenhuma mudança nos critérios de execução live.
- Samples preservam locked, borrowed, interest e gross, inclusive quando saldo líquido é zero. Posições incluem IDs de decisão/proteção e estado de recuperação.
- Eventos `exit_touch`, `stop_reconciled`, `live_exit_blocked` e `protection_critical` preservam correlação, cotação, identificação do STOP e causa original. Escrita de eventos ocorre em segundo plano para não atrasar a ordem; checkpoints de bloqueio continuam aguardados. Eventos de auditoria podem se perder se o processo terminar antes de concluir a escrita. Isso não substitui o histórico da corretora.
- Resolução de entrada distingue sinal expirado antes de submissão, ordem terminal sem fill e ausência de ordem após janela de reconciliação.
- `app.analytics.exit_replay` compara, offline, plano fixo, prazos de 30/60 minutos e proteção após +1R líquido com devolução de 0,5R. Usa bid para LONG, ask para SHORT, taxas, slippage explícito e juros por hora arredondada. Limite temporal é obrigatório nas variantes de tempo, inclusive para vencedoras. Essa é uma hipótese de pesquisa, não alteração da saída live.

## Execução do replay

`python scripts/compare_exit_policies.py INPUT.json OUTPUT.json` a partir de `apps/engine`.

Entrada JSON:

```json
{
  "source": "bid_ask",
  "slippage_bps": 2,
  "entries": [{
    "entry": {
      "decision_id": "identificador",
      "side": "LONG",
      "opened_at": "2026-10-15T12:00:00Z",
      "entry_price": 100,
      "quantity": 1,
      "entry_fee": 0.1,
      "exit_fee_rate": 0.001,
      "borrow_hourly_rate": 0.0001,
      "initial_net_risk": 1.2,
      "stop": 99,
      "target": 103
    },
    "quotes": [{"timestamp": "2026-10-15T12:00:01Z", "bid": 100.1, "ask": 100.2}]
  }]
}
```

Não há simulação de profundidade, filas nem latência: até uma série contínua de quotes não comprova fills. Gaps superiores a cinco segundos tornam a cobertura incompleta. O campo `coverage_complete` descreve apenas cobertura temporal. Uma trajetória censurada mantém `net_pnl=null`; o mark fica separado. Não somar marcas censuradas com saídas simuladas. Não otimizar usando somente os perdedores de um dia.

## Diagnóstico executado com os dados disponíveis

Arquivos privados ignorados pelo Git em `exports/today-trade-review-2026-10-15/exit-replay-input.json` e `exit-replay-results.json`.

Oito entradas com STOP conhecido, excluindo falhas imediatas, encerramentos externos e posição aberta. Comparação pareada e isolada, sem simulação do portfólio, capital liberado ou novas entradas. Foram usados somente fechamentos completos posteriores à entrada, spread hipotético de 2 bps, slippage adverso adicional de 2 bps, taxa de entrada registrada, taxa de saída do plano e taxa de empréstimo de stress do plano. Juros reais podem diferir. Os caminhos contrafactuais seguem até o fim dos candles disponíveis, mesmo após a saída real.

|Hipótese|Resultado líquido simulado por proxy (USDT)|Cobertura bid/ask verificada|
|---|---:|---:|
|Plano fixo sem prazo|−0,510052|0/8|
|Prazo 30 minutos|−0,063485|0/8|
|Prazo 60 minutos|−0,175795|0/8|
|Proteção +1R e prazo 60 minutos|−0,172595|0/8|

Todas as oito trajetórias produziram saída em cada hipótese. **Nenhuma variante ficou positiva nesse cenário.** A redução de perda sugere estudar duração, mas não valida 30 minutos como ótimo e não demonstra melhora futura. O plano fixo por candle difere do resultado real por construção; não serve para auditar o toque ETH das 10:55. Alvos intraminuto e a sequência bid/ask ficam invisíveis.

## Pendências de evidência

- Não há credenciais locais Binance/gateway disponíveis para consultar fills manuais. Os três PNLs externos permanecem desconhecidos; não registrar lucro/prejuízo presumido no banco.
- Candles não recuperam o ask histórico do ETH às 10:55. Novos eventos darão evidência prospectiva do toque e da gestão, mas não permitem reconstruir uma cotação que não foi armazenada.
- Não ativar automaticamente o seletor, proteção de lucro ou prazo de saída por este resultado. Próxima validação exige amostra temporal fora da seleção deste dia, custos reais e análise por regime, direção, setup e versão. Preservar os limites agregados de risco já existentes.

## Validação

Suíte completa do engine, incluindo simetria LONG/SHORT, custo líquido, censura/gaps, reentrada com evento antigo/futuro, invariância de execução em shadow, preservação de dívida e correlação de falhas. Nenhuma chamada de envio/cancelamento de ordem nem alteração em produção durante a implementação.
