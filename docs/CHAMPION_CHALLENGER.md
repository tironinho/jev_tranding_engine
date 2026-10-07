# Champion e challenger

Cada família ativa tem um champion. No bootstrap:

| Família | Champion |
| --- | --- |
| baseline | B-001 |
| baseline_jev | J-001 |
| baseline_openai_jev | OJ-001 |

`ensemble` e `ml` existem no enum e ainda não têm champion.

`run_analysis` cria o experimento com `parent_version` e `challenger_version` ao mesmo tempo (`B-001` gera `B-002`). A versão nova fica `draft` e é persistida. Ela ainda não entra no loop de mercado ao lado do champion e ainda não tem conta paper própria. Lucro maior num único backtest não substitui o champion.

Promoção histórica exige, na configuração inicial e ainda não calibrada:

- pelo menos 100 trades;
- melhora de expectativa de pelo menos 0,05R;
- drawdown que não aumenta;
- profit factor de pelo menos 1,2;
- walk-forward e Monte Carlo obrigatórios.

Os gates de shadow e paper existem na configuração e na API de promoção. O challenger ainda não recebe mercado em shadow nem uma conta paper separada. `MIN_PAPER_TRADES` e `MIN_PAPER_DAYS` continuam sendo exigência da promoção, não um runtime já medido.

Rollback volta `previous_champion`. Se um challenger em paper deteriorar, a troca de champion de paper pode ser desfeita. Isso não fecha posição live, porque live não é promovido aqui.

O score da estratégia é uma soma configurável em `packages/configs/evolution.yaml`. Os pesos não foram otimizados.
