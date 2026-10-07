# Ciclo do experimento

```text
proposed
  → approved_for_build
  → building
  → testing
  → backtesting
  → oos
  → walk_forward
  → monte_carlo
  → shadow
  → paper
  → candidate
```

Saídas alternativas: `rejected` ou `failed`.

Cada transição entra na timeline do experimento e em `promotion_events` quando muda versão. O motivo de rejeição fica em `rejection_reason`.

Depois que o estágio OOS começa, `code_locked` deve ser verdadeiro. Mudou o código: outro experimento, outro `dataset_version`. O mesmo OOS não é reutilizado para ajustar de novo.

Reproduzir um experimento exige git commit, dataset, config, fees, slippage, seed, modelo e `prompt_version`. Esses campos existem na entidade. Um backtest histórico não é recalculado com outro dataset sem marcar a versão.

Cancelar grava `CANCELLED` e status `failed`. Rejeitar grava o código de razão, por exemplo `OOS_FAILURE`, `WALK_FORWARD_FAILURE`, `INSUFFICIENT_SAMPLE`, `PROTECTED_CODE_CHANGED`.

Não existe autopromoção para live. O estado `candidate` é o máximo automático previsto, e mesmo ele só existe depois dos gates. Trocar o champion de conta real continua exigindo aprovação humana, que esta versão ainda recusa.
