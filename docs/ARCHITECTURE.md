# Arquitetura — trading-engine

Plataforma experimental para comparar, no mesmo mercado e com os mesmos custos, três estratégias:

1. `baseline` — score determinístico, sem IA
2. `baseline_jev` — o mesmo score, com veto probabilístico do Jev
3. `baseline_openai_jev` — o mesmo score, com interpretação semântica da OpenAI e depois o Jev

O sistema não afirma edge. Pesos, limiares e prompts nascem conservadores e **não calibrados**. Quem responde se uma camada melhora a outra são os trades registrados, o PnL líquido e a comparação por `opportunity_id`.

## Princípio de fronteira

```text
Mercado → Features → Estratégia (ação + confiança + razões)
                         ↓
                    Risk Engine (tamanho, stop, alvo, RR líquido, limites)
                         ↓
                    Execution Provider (paper ou live bloqueado)
```

- Estratégia não define quantidade, risco monetário, stop final, alvo final nem alavancagem.
- OpenAI não envia ordem, não lê API autenticada da Binance e não altera limites.
- Jev não envia ordem. O adapter real só chama a `JEV_BASE_URL` informada no ambiente. Sem key e URL, o processo usa o mock.
- O Risk Engine é determinístico. Nenhum LLM o altera em runtime.
- `LIVE` só atravessa o provider se `TRADING_LIVE_ENABLED`, `ALLOW_REAL_ORDERS` e `mode=live` forem verdadeiros ao mesmo tempo.

## Visão de runtime

```text
Dashboard Next.js  --HTTP/SSE-->  API FastAPI  -->  Trading Engine (processo persistente)
                                                      |
                                                      +--> PostgreSQL (quando DATABASE_URL existe)
                                                      +--> Binance market data (público)
                                                      +--> OpenAI Responses API (opcional)
                                                      +--> JevProvider (mock, ou real na URL configurada)
```

O engine sobe com o processo (`python main.py` ou Uvicorn). Trabalho de mercado, decisão e execução acontece em workers `asyncio`, não em request/response serverless. A API observa e configura esse processo.

Deploy alvo do frontend: Vercel. Deploy alvo do engine: container, VPS, Render, Railway, Fly.io ou equivalente. O servidor HTTP escuta `0.0.0.0:$PORT`.

## Fluxo de dados

```text
Binance WS/REST (um market_type só)
        |
        v
MarketStateStore          ticker SSE (UI, não dispara ordem)
        |
        | kline 1m fechado, ou replay/backtest
        v
FeatureEngine  ---> MarketSnapshot imutável (snapshot_id)
        |
        | mesmo objeto para as três estratégias
        +-------------------+----------------------+
        |                   |                      |
   baseline            baseline_jev         baseline_openai_jev
        |                   |                      |
        +-------------------+----------------------+
                            |
                     ConsensusEngine (só analítico)
                            |
                     RiskEngine por estratégia
                            |
              shadow: grava e para
              paper:  PaperExecutionProvider + conta virtual isolada
              live:   BinanceExecutionProvider, se armado
                            |
                     posições, fills, MFE/MAE, labels futuros
                            |
                     performance líquida e comparação
```

Triggers:

- Cada mensagem de mercado só atualiza estado e métricas de latência.
- Entradas nascem no fechamento do candle de timing (default `1m`), configurável.
- Stops e alvos de posições paper são avaliados em todo book ticker. Isso é gestão da posição já aberta, não um sinal novo.
- OpenAI e Jev rodam em tasks isoladas. Se passarem de `MAX_SIGNAL_AGE_MS`, o sinal fica `EXPIRED_SIGNAL` e não entra.

## Módulos

| Módulo | Responsabilidade |
| --- | --- |
| `app/market` | Adapter Binance, validação, reconnect, backoff, heartbeat, staleness. Não mistura spot e futures. |
| `app/features` | Indicadores explicáveis. Só candles fechados com `close_time <= T` e trades/book com timestamp `<= T`. |
| `app/strategies` | `score_baseline` único. As outras estratégias reutilizam esse resultado. |
| `app/providers/openai` | Responses API, JSON Schema, validação Pydantic, custo, timeout, retry, circuit breaker. |
| `app/providers/jev` | `JevProvider`, `MockJevProvider`, `RealJevProvider` (POST só na URL configurada). |
| `app/risk` | Stop estrutural, alvo, custos, RR bruto/líquido, sizing por risco, limites, kill switch. |
| `app/execution` | Slippage (`fixed_bps`, `spread_based`, `orderbook_based`), paper, live protegido, idempotência. |
| `app/accounts` | Três contas virtuais com o mesmo capital inicial. |
| `app/consensus` | Acordo só entre quem votou: 3/3, 2/3, 2/2 se um ficou skipped, conflito, sem consenso. Não opera. |
| `app/analytics` | PnL líquido, expectativa, drawdown, Sharpe/Sortino, comparação e ROI de IA quando houver custo real. |
| `app/events` | `EventBus` sobre `asyncio.Queue`. Troca futura por Redis/Kafka/NATS fica atrás da interface. |
| `app/db` | SQLAlchemy + Alembic. Memória se não houver `DATABASE_URL`. |
| `app/backtest` | Replay e backtest chamam Feature, Strategy, Risk e o simulador. Sem segunda cópia das regras. |
| `apps/web` | Dashboard. Segredos só no servidor Next. Sem número inventado. |

## Snapshot

`MarketSnapshot` é imutável e carrega `snapshot_id`, tempo UTC, símbolo, `market_type`, referência do dado bruto, preço, bid/ask, spread, features, qualidade, latência e regime quantitativo.

As três avaliações de um mesmo evento compartilham `opportunity_id` e `correlation_id`. Cada decisão tem `decision_id`. Cada ordem paper/live usa `client_order_id` de 32 caracteres derivado do `decision_id` (limite da Binance). Reenvio devolve a ordem existente.

Cadeia auditável:

```text
snapshot → features → strategy decision → openai_calls? → jev_calls? → risk decision → order → fills → trade → pnl
```

## Modelo de eventos

Tópicos do barramento: `market_update`, `snapshot_created`, `decision`, `risk`, `execution`, `trade`, `engine_status`.

O barramento não é a fonte das regras. Ele desacopla ingestão, UI (SSE) e persistência. A orquestração das estratégias usa tasks com isolamento: exceção em uma estratégia vira `STRATEGY_ERROR` / `NO_TRADE` e as outras seguem.

Política de fila cheia: a estratégia saturada registra `QUEUE_SATURATED` e não bloqueia o coletor.

## Modos

| Modo | Decisão | Execução |
| --- | --- | --- |
| `disabled` | não calcula | não |
| `shadow` | calcula e grava | não |
| `paper` | calcula e grava | simulada na conta virtual da estratégia |
| `live` | calcula e grava | só se os dois flags de ambiente e o modo da estratégia estiverem armados |

`disabled` é o modo operacional de “não calcular”. O kill switch `TRADING_ENGINE_ENABLED` é outra chave: impede abertura nova e mantém stop/alvo das posições já abertas. Não fecha posição por conta própria.

Se o book estiver stale, a decisão é `NO_TRADE` / `STALE_MARKET_DATA`.

Se Postgres foi configurado e está indisponível, não abre trade (`PERSISTENCE_UNAVAILABLE`). Sem `DATABASE_URL`, o processo sobe em memória para desenvolvimento e o status deixa isso explícito. Live nunca abre sem persistência saudável.

## Estratégias e regras de combinação

Versões gravadas em toda decisão:

- `baseline_classified_v1`
- `jev_strict_signal_v4` / `jev_target_3r_v1`
- `openai_jev_break_size_v1`
- `market_interpreter_v1` (prompt OpenAI; versão nova = chave nova, sem edição silenciosa)
- `jev_market_v1` (rótulo do prompt Jev; o host vem só de `JEV_BASE_URL`)

`score_baseline` pesa cada família em `[-1, +1]`, combina o composto e só então classifica. A ordem sai depois da classe. A classe recusa o lado do composto quando os componentes divergem, quando um único componente carrega o score, ou quando o fluxo vai contra esse lado. Hard blocks (spread, volatilidade extrema, histórico insuficiente) continuam `NO_TRADE`. Constantes de escala apenas normalizam grandeza; não são resultado de otimização.

`baseline_jev` só chama o Jev em produção quando a baseline já classificou um `LONG` ou `SHORT` com score absoluto mínimo de 0,55. Candidatos abaixo desse piso continuam disponíveis em paper/shadow para pesquisa, sem chegar à execução live. O pedido leva os pesos e a classe. O Jev responde continuação, reversão e falso rompimento sobre esse candidato, num único passe. Continuação abaixo de 55% veta; reversão alta e falso rompimento claro também vetam. O Jev não cria um lado que a baseline recusou e não inverte LONG/SHORT. Falha do Jev: default `NO_TRADE` (`FALLBACK_TO_BASELINE` existe na configuração e não é o default).

`baseline_openai_jev` faz o mesmo, com um passo anterior: a OpenAI devolve `MarketState` validado por Pydantic. Schema inválido rejeita. A interpretação pode vetar por regime/anomalia antes do passe do Jev. Sem chave, ou com chamadas desligadas, a estratégia fica em standby e não finge decisão. Cache de estado de mercado existe como interface e nasce desligado.

O mock do Jev é determinístico, marcado `is_mock=true`, e existe para testar encanamento. Não é modelo de mercado.

## Risk Engine

Entradas: decisão, snapshot, conta, posições, taxas, book, funding.

1. Stop estrutural a partir do swing recente e de um buffer de ATR. Se o swing estiver do lado errado, há fallback ATR explícito na razão `STOP_ATR_FALLBACK`. Stop largo demais rejeita `STOP_TOO_WIDE`.
2. O alvo usa 3R como piso. Quando faixa/estrutura calculam um alvo mais distante, esse alvo maior é preservado. Produção não encerra por tempo; fecha por target, stop ou falha de proteção. O stop passa ao break-even líquido em +1R e trava +1R depois de +2R.
3. Custos separados: fee de entrada, fee de saída, slippage, funding. O spread entra no preço estimado de execução quando o modelo é `spread_based` ou `orderbook_based`, sem cobrar de novo.
4. `gross_rr`, `net_rr` e expectativa permanecem no registro para auditoria. Nenhum deles veta uma entrada.
5. Quantidade = perda máxima da conta / perda líquida por unidade até o stop. Sem tamanho fixo.
6. Limites: risco por trade, perda diária, drawdown diário, posições abertas, exposição do símbolo, exposição total, margem, uma posição por símbolo, intervalo mínimo entre entradas. Estouro: `RISK_REJECTED` com código específico.
7. Spot não abre short (`SPOT_SHORT_NOT_SUPPORTED`).

Taxas vêm de `FeeProvider` (configuração ou `commissionRate` da Binance quando a chave permitir). A taxa usada fica gravada no trade. Não há 0,1% cravado no código de execução.

Slippage default: `orderbook_based`, com fallback registrado se o book não chegar.

## Contas e comparação

Três contas, mesmo capital inicial, mesmos custos, relógios iguais:

- `baseline`
- `baseline_jev`
- `baseline_openai_jev`

Métrica principal: **PnL líquido**. Bruto, fees, slippage e funding ficam separados. Win rate, profit factor, expectativa em R, drawdown, Sharpe e Sortino só aparecem quando há amostra; caso contrário a API devolve `null` e a UI mostra `NO DATA`.

Comparação por oportunidade: só baseline operou, só Jev, só OpenAI+Jev, acordo, divergência, trades que o Jev eliminou e o resultado desses trades na baseline. Custo de IA só vira dólar se `OPENAI_INPUT_USD_PER_1M` e `OPENAI_OUTPUT_USD_PER_1M` estiverem preenchidos. Sem isso, tokens são gravados e o custo fica `null`.

Consensus é gravado e exibido. Não há estratégia `ensemble` operando. O contrato de consenso já deixa essa quarta estratégia possível.

Regime: classificador quantitativo determinístico no snapshot. O regime da OpenAI, quando existe, é outra coluna. Performance pode ser fatiada pelos dois, sem tratar a classificação do modelo como verdade.

## Labels que não entram na decisão

Depois do fato, o labeler preenche `future_return_30s/1m/3m/5m/15m`. MFE e MAE são atualizados na vida da posição. Isso alimenta export CSV (Parquet se `pyarrow` estiver instalado) e calibração futura. O objeto lido pela estratégia não contém esses campos.

## Persistência

Postgres 16 via `DATABASE_URL`. Migrações Alembic em `infra/migrations`, modelos em `app/db/models.py`.

Tabelas: `symbols`, `market_snapshots`, `feature_snapshots`, `strategy_configs`, `strategy_decisions`, `openai_calls`, `jev_calls`, `risk_decisions`, `orders`, `fills`, `positions`, `trades`, `paper_accounts`, `paper_balances`, `performance_snapshots`, `consensus_results`, `future_labels`, `engine_events`, `audit_logs`.

Configuração operacional (modo, enabled, limiares editáveis no dashboard) grava no banco e em `audit_logs`. YAML/env são o default de boot. Limiares de risco editáveis pela UI são só os listados no README. Alavancagem e flags de ordem real não têm endpoint.

## API

REST em `/api/...` e saúde em `/health`, `/ready`, `/status`, `/metrics`. SSE em `/stream` (`market_update`, `decision`, `trade`, `position`, `engine_status`).

Autenticação: `Authorization: Bearer $ENGINE_API_SECRET` em tudo que não é health/ready. Sem segredo, só `ENVIRONMENT=development` aceita chamada. Em `production`, segredo vazio recusa a API de negócio. O browser fala apenas com rotas Next.js; o servidor Next anexa o segredo.

## Deployment

- Frontend: `apps/web`, Next.js, variável de servidor `ENGINE_API_URL` + `ENGINE_API_SECRET`.
- Engine: `Dockerfile` na raiz do repositório, `docker compose up -d postgres`, processo `python main.py`.
- Filesystem do engine em PaaS é efêmero. Estado que importa está em Postgres, não em disco local.
- Free tier que dorme não serve para o coletor websocket. O engine precisa de processo contínuo.

## Segurança

- Chaves Binance, OpenAI e Jev só no processo do engine.
- Binance, no desenvolvimento: chave read-only. Paper não precisa de trade nem de saque. Live futuro: só permissão de ordem. Nunca withdrawal. O código não chama endpoint de saque.
- Idempotência por `client_order_id` / `newClientOrderId`.
- Circuit breakers para Binance, OpenAI, Jev e database. Rate limit com token bucket. Sem retry agressivo. `429`/`418` abrem o breaker.
- Baseline continua se OpenAI ou Jev caem.

## O que esta versão deliberadamente não faz

- O HTTP do Jev existe só contra `JEV_BASE_URL`. Não há host padrão no código.
- Não liga cache agressivo de Market State.
- Não opera consensus.
- Não liga trailing, saída parcial ou break-even.
- Não promete lucro. Pesos `baseline_weights_v1` precisam de backtest antes de qualquer uso live.
