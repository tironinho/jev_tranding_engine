# Champion e challenger

Cada família ativa tem um champion. No bootstrap:

| Família | Champion |
| --- | --- |
| baseline | B-001 |
| baseline_jev | J-001 |
| baseline_openai_jev | OJ-001 |

`ensemble` e `ml` existem no enum e ainda não têm champion.

Um challenger nasce de um experimento, com `parent_version` apontando para o champion da época. Lucro maior num único backtest não substitui o champion.

Promoção histórica exige, na configuração inicial e ainda não calibrada:

- pelo menos 100 trades;
- melhora de expectativa de pelo menos 0,05R;
- drawdown que não aumenta;
- profit factor de pelo menos 1,2;
- walk-forward e Monte Carlo obrigatórios.

Shadow e paper usam conta ou registro isolado. Paper ainda exige `MIN_PAPER_TRADES` e `MIN_PAPER_DAYS` antes de seguir. Os dois contam.

Rollback volta `previous_champion`. Se um challenger em paper deteriorar, a troca de champion de paper pode ser desfeita. Isso não fecha posição live, porque live não é promovido aqui.

O score da estratégia é uma soma configurável em `packages/configs/evolution.yaml`. Os pesos não foram otimizados.
