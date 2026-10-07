# Trading Engine

Plataforma experimental para comparar, no mesmo mercado e com os mesmos custos, três estratégias de cripto:

- `baseline` — score determinístico, sem IA
- `baseline_jev` — o mesmo score, com veto probabilístico do Jev
- `baseline_openai_jev` — interpretação estruturada da OpenAI e depois o Jev

O objetivo é medir se cada camada adiciona resultado líquido ou só custo, latência e complexidade. Pesos e limiares nascem conservadores e **não estão calibrados**. Nada aqui é recomendação de investimento, e nenhum número de performance deve ser inventado: a interface mostra `NO DATA` até existirem trades registrados.

A arquitetura está em [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## O que esta versão faz

- Processo persistente em Python (`asyncio`), com API FastAPI, SSE e Postgres opcional.
- Coleta pública de mercado Binance (spot ou futures, nunca misturados), com reconnect, backoff e book stale virando `NO_TRADE`.
- Feature engine explicável, baseline determinística, risk engine, simulador paper e três contas virtuais com o mesmo capital inicial.
- Jev: interface, schemas, mock determinístico e `RealJevProvider`. O real só faz POST na `JEV_BASE_URL` configurada. Sem key e URL, `JEV_PROVIDER=auto` continua no mock.
- OpenAI: Responses API com JSON Schema. `call_model` nasce ligado na estratégia OpenAI. Sem chave, ela fica em standby e não gasta.
- Live existe atrás de `TRADING_LIVE_ENABLED` e `ALLOW_REAL_ORDERS`. Os dois vêm `false`. O dashboard não consegue ligar esses flags.
- Backtest e replay reutilizam feature, estratégia, risco e o simulador.

## Pré-requisitos

- Python 3.12+
- Node.js 20+
- Docker, só se for usar Postgres local

O disco C: desta máquina estava sem espaço. O repositório ficou em `D:\trading-engine`.

## Variáveis

Copie `.env.example` para `.env` na raiz e, para o frontend, `apps/web/.env.example` para `apps/web/.env.local`.

Segredos ficam só no processo do engine e no servidor Next. Não use prefixo `NEXT_PUBLIC_` para chave nenhuma.

| Variável | Função |
| --- | --- |
| `DATABASE_URL` | Postgres. Vazio sobe em memória e o status mostra isso. |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | Opcionais. Mercado público não precisa. |
| `OPENAI_API_KEY` / `OPENAI_MODEL` / `OPENAI_PROMPT_VERSION` | Interpretação. Sem chave, a estratégia não chama a API. |
| `OPENAI_INPUT_USD_PER_1M` / `OPENAI_OUTPUT_USD_PER_1M` | Se vazios, tokens são gravados e o custo fica `null`. |
| `JEV_*` | `JEV_PROVIDER=auto` usa mock enquanto key e URL estiverem vazios. Com os dois, o POST vai para `JEV_BASE_URL`. |
| `ENGINE_API_SECRET` | Bearer da API. Obrigatório quando `ENVIRONMENT=production`. |
| `TRADING_ENGINE_ENABLED` | Kill switch de boot. |
| `TRADING_LIVE_ENABLED` e `ALLOW_REAL_ORDERS` | Os dois precisam ser `true` para existir ordem real. |
| `MARKET_TYPE` | `futures` ou `spot`. |
| `SYMBOLS` | Default `BTCUSDT,ETHUSDT,SOLUSDT`. |
| `MIN_NET_RR`, risco e exposição | Limites do Risk Engine. |

`exposure ≈ risk_per_trade / distância_do_stop`. Um risco de 0,5% com stop de 2% usa cerca de 25% do capital. Stop curto demais estoura exposição e a ordem é rejeitada, em vez de crescer a quantidade em silêncio.

## Subir localmente

Postgres:

```bash
docker compose up -d postgres
```

Migração, a partir de `apps/engine`, com `DATABASE_URL` apontando para o banco:

```bash
alembic upgrade head
```

O engine também cria tabelas ausentes no boot. Alembic continua sendo a migração versionada. Trocar o banco de produção é trocar `DATABASE_URL` e rodar de novo.

Engine:

```bash
cd apps/engine
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
python main.py
```

Em desenvolvimento, com reload:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

O servidor escuta `0.0.0.0` e a porta `PORT`, para um processo contínuo em container, VPS, Render, Railway ou Fly.io. Ele não depende de request serverless para avaliar o mercado.

Frontend:

```bash
cd apps/web
npm install
npm run dev
```

Abra `http://localhost:3000`. O browser fala só com o Next. O Next chama `ENGINE_API_URL` e anexa `ENGINE_API_SECRET`.

Sem credenciais o processo sobe. Binance privado fica desligado, OpenAI fica sem chamadas, Jev fica no mock. Isso não derruba a baseline.

## Docker

```bash
docker compose up -d postgres
docker compose --profile full up --build engine
```

O Dockerfile do engine fica na raiz do repositório (`Dockerfile`), que é o caminho que a Render usa por padrão. O contexto de build também é a raiz.

## Binance

Para coleta e desenvolvimento, use chave **read-only** se for usar algo autenticado. O book, klines, trades, mark price, funding e open interest públicos não exigem chave.

Paper trading não precisa de permissão de ordem nem de saque.

Se um dia o live for armado de propósito, a chave deve ter só a permissão de trading necessária. O sistema não chama endpoint de saque e não deve receber permissão de withdrawal.

Spot e futures não são misturados. `market_type` vai no snapshot.

## OpenAI

A chamada usa `POST /v1/responses` com `text.format` JSON Schema strict. A resposta passa por Pydantic. JSON inválido vira rejeição, nunca decisão lida de texto livre. O modelo não define quantidade, alavancagem, stop, alvo ou ordem.

`call_model` nasce `true` na estratégia OpenAI. Sem `OPENAI_API_KEY` a chamada não acontece. O prompt `market_interpreter_v1` não deve ser editado no lugar: crie `market_interpreter_v2`.

O cache de market state existe e fica desligado.

## Jev

`JevProvider.evaluate_market_state` devolve `JevAssessment`. `MockJevProvider` é determinístico, marcado `is_mock=true`, e só existe para testar o encanamento. Não é um modelo de mercado.

`RealJevProvider` envia o `JevMarketRequest` para `JEV_BASE_URL` e exige as probabilidades do `JevAssessment`. URL vazia, HTTP ruim ou JSON inválido caem em `NO_TRADE` (default), não na baseline. `FALLBACK_TO_BASELINE` existe na configuração e não é o default.

## Modos

| Modo | Efeito |
| --- | --- |
| `disabled` | não calcula |
| `shadow` | calcula e grava, não executa |
| `paper` | simula na conta virtual daquela estratégia |
| `live` | só envia ordem se os dois flags de ambiente e o modo da estratégia estiverem armados |

Default de boot: as três estratégias em paper, com o mesmo capital inicial. Fill é simulado. Ordem real continua impossível sem `TRADING_LIVE_ENABLED` e `ALLOW_REAL_ORDERS`, e o dashboard não consegue marcar `live` enquanto esses flags estiverem desligados. Contas já criadas não são zeradas pelo dashboard.

`STOP ALL TRADING` impede entrada nova, mantém stop e alvo das posições paper já abertas, e grava auditoria. Não fecha posição sozinho.

Book antigo demais produz `NO_TRADE` / `STALE_MARKET_DATA`. Decisão mais lenta que `max_signal_age_ms` vira `EXPIRED_SIGNAL` e não entra.

## Evolution Engine

Camada separada em `apps/engine/app/evolution`. Ela lê resultados e não participa da ordem. Champions iniciais: `B-001`, `J-001`, `OJ-001`. Sem amostra mínima, saúde e expectativa ficam vazias. Live não é promovido. O agente de código default é mock e não edita o repositório. Detalhe em [docs/EVOLUTION_ENGINE.md](docs/EVOLUTION_ENGINE.md) e [docs/SAFETY_BOUNDARIES.md](docs/SAFETY_BOUNDARIES.md).

A página é `/evolution`.

## Testes

```bash
cd apps/engine
pytest
```

Cobrem fees, RR líquido, sizing, slippage, limites, drawdown, staleness, isolamento entre estratégias, idempotência de ordem, gate de live, look-ahead e o fato de o adapter real do Jev não ter cliente HTTP.

## Deploy

- Frontend: Vercel, root `apps/web`. Defina `ENGINE_API_URL` e `ENGINE_API_SECRET` como variáveis de servidor, nunca públicas.
- Engine: `Dockerfile` na raiz, processo contínuo. Filesystem de PaaS é efêmero; o estado que importa está no Postgres.
- Um tier que dorme depois de inatividade não serve para o websocket de mercado.

## Segurança

Em `ENVIRONMENT=production` a API de negócio exige `ENGINE_API_SECRET`. `/health` e `/ready` ficam abertos para o orquestrador. `/ready` significa que o processo subiu. Prontidão de trading está em `/status`: Binance, banco, circuitos, fila e modo de cada estratégia.

Live continua impossível se qualquer um dos flags estiver ausente ou falso.
