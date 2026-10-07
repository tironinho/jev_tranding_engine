# Limites de segurança

A Evolution Engine não altera:

- limites de risco;
- credenciais;
- saque;
- alocação de capital real;
- kill switch;
- execução live.

Paths protegidos estão em `app/evolution/paths.py`. Incluem `app/risk/`, `binance_live.py`, `.env` e os prefixos conceituais `core/risk`, `security`, `secrets`, `kill_switch` e `capital_limits`. Qualquer diff nesses paths é `PROTECTED_CODE_CHANGED`. Se o `git diff` contra a base não puder ser calculado, o check termina com erro. Ele não passa em silêncio.

Paths permitidos para um experimento futuro: estratégias, features, sinais, filtros, regime e prompts. Peso de baseline e config de estratégia entram. Config de risco não.

`LIVE_PROMOTION_DISABLED` é a resposta de qualquer promoção para live, canary ou champion de conta real. Canary existe só como alvo recusado. `HUMAN_APPROVAL_REQUIRED` permanece verdadeiro e não é suficiente para ligar live.

O researcher devolve JSON estruturado. Ele não executa SQL e não tem shell. Sem `OPENAI_RESEARCH_MODEL`, ele não inventa hipótese: devolve `NO_ACTION`.

O agente de código mock não escreve no disco. O provider Cursor não chama rede.

Amostra abaixo de `MIN_EXPERIMENT_SAMPLE_SIZE` não vira anomalia nem promoção. Ausência de evidência é `NO_ACTION`, que é o resultado preferido.

Auto build, auto shadow e auto paper nascem desligados. Auto research só observa e registra.
