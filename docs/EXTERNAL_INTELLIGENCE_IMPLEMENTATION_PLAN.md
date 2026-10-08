# External Market Intelligence

A camada entra ao lado da Trading Engine. Ela não substitui o book, o baseline, o veto do Jev nem o risco.

## O que já existe e não será duplicado

O feature engine já calcula, no snapshot de 1 minuto, e já filtra `timestamp <= as_of`:

- `taker_flow_1m`, `orderflow_delta_ratio`, `aggressive_buy_volume`, `aggressive_sell_volume`
- `imbalance_5`, `imbalance_10`, `imbalance_20`, `bid_liquidity_*`, `ask_liquidity_*`
- `spread_bps`, `microprice`, `cvd`, `cvd_slope`
- `volatility_percentile`, retornos e estrutura de preço

O feed de mercado já faz depth, book ticker e klines no host spot (`data-api.binance.vision` em produção). Funding e open interest de futuros já têm poll em `/fapi/v1/premiumIndex` e `/fapi/v1/openInterest`, mas o motor roda `MARKET_TYPE=margin` no livro spot. Nesse modo o feature engine zera funding e OI. Oregon também recebe HTTP 451 no REST de futuros.

O Jev real recebe só um estado curto e três nouls: continuação, reversão e falso rompimento. O veto fica no nosso código. O Jev não abre trade que o baseline recusou e não define tamanho, alavancagem nem permissão.

Token bucket e circuit breaker já estão em `app/resilience/guards.py`. O Postgres sobe tabelas com `Base.metadata.create_all`. Não há Alembic no repositório.

## Como encaixa

```text
Feature engine (microestrutura já pronta)
        +
Workers externos, em paralelo, com falha isolada
        ↓
Observação crua imutável
        ↓
Normalização versionada (market_normalization_v1)
        ↓
Snapshot de inteligência, só com observed_at <= T
        ↓
Contexto curto do Jev, 30–60 campos, null quando falta
        ↓
Veto atual (três nouls) + Strategy Policy + Risk Engine
```

O pipeline de decisão não espera essas APIs. Ele lê o último snapshot válido já em memória. Se o worker falhar, o campo fica `null` e `available=false`. Não vira zero. Não derruba o book.

## Decisões que o texto longo pedia e que mudam aqui

1. Não buscar de novo aggTrades, depth, trades, klines e bookTicker. Isso já alimenta o snapshot. A inteligência só relê essas features e aplica z-score, percentil e freshness.
2. Derivativos da Binance (OI histórico, taker long/short, top trader) são um worker à parte no host `fapi.binance.com`, não no host do livro. Se vier 451, o provider fica `DEGRADED` e o resto segue.
3. Coinalyze descobre o símbolo em `/v1/future-markets` e grava o mapping. Nenhum símbolo é inventado.
4. CryptoQuant e Coin Metrics nascem desligados. Sem catálogo da chave, nenhuma métrica é habilitada e nenhum endpoint antigo é chutado.
5. Fear & Greed é contexto lento, escopo `BTC_MARKET_SENTIMENT`, não sentimento de SOL.
6. O gate `MIN_JEV_DATA_QUALITY` não transforma falha externa em `NO_TRADE` da estratégia que já opera. Qualidade baixa omite o contexto novo e mantém o veto atual. `NO_TRADE` por qualidade só vale para um feature set que declare dependência dessa fonte. Isso cumpre ao mesmo tempo “não derrubar a engine” e “não tratar desconhecido como zero”.
7. O contrato do Jev não é trocado nesta base. Os três nouls continuam. Campos novos (`crowding_risk`, `liquidity_risk`, `event_risk`) só entram numa versão de prompt explícita, sem o Jev passar a escolher o lado.
8. Tabelas novas entram no `create_all` já usado. Alembic não será introduzido só para esta camada.

## Ordem

I1: schemas, tabelas, normalização, freshness, qualidade, as-of.

I2–I5, nesta entrega: o worker lê as APIs e monta o `jev_feature_set_v1`. O veto de três nouls continua. Qualidade baixa não vira `NO_TRADE` enquanto `JEV_INTELLIGENCE_GATE=FALLBACK_TO_BASELINE`.

O que cada fonte grava:

- Binance futuros: OI e funding em `/fapi/v1/openInterest` e `/fapi/v1/fundingRate`. Com `X-MBX-APIKEY`, também OI em USD, taker buy/sell e os três ratios de conta/posição. Preço, book, CVD e spread continuam no livro que já existe. HTTP 451 deixa o provider degradado.
- Coinalyze: primeiro `/v1/future-markets`. O símbolo usado é o que o catálogo devolve. Depois OI em USD, funding, predicted funding, histórico de OI, liquidações (`l`/`s`), long/short (`r`/`l`/`s`) e OHLCV. Sem chave fica `DISABLED`. 429 para a rodada e respeita `Retry-After`.
- Alternative.me: `/fng/` com escopo `BTC_MARKET_SENTIMENT` e `/v2/global/` para dominância. O índice é diário. A variação de 1, 3 e 7 dias distingue 20 vindo de 8 de 20 vindo de 60.
- CryptoQuant: desligado. Sem catálogo da chave, nenhum endpoint de métrica é chamado.
- Coin Metrics: desligado. Com a flag, só o catálogo `/v4/catalog-all-v2/asset-metrics` é lido, e só a métrica que aparecer para aquele ativo fica disponível.

Liquidação positiva significa shorts liquidados, não compra. Funding alto vira crowding, não sinal de alta.

I2: worker Binance de derivativos, reusando o livro atual para microestrutura. Degrada em 451.

I3: Coinalyze, com rate limit configurável e `Retry-After`.

I4: Alternative.me, Fear & Greed e dominância.

I5: `MarketIntelligenceSnapshot` e o builder do contexto do Jev, ainda sem substituir o veto. O inspector mostra raw, normalizado, freshness, qualidade, fonte e timestamp.

I6: página `/intelligence`.

I7 e I8: descoberta de capacidade da CryptoQuant e da Coin Metrics, só com chave e catálogo.

News e macro ficam como interface desligada.

## O que não entra no Jev

Preço em dólar, OI em bilhões, funding cru e Fear & Greed 0–100 não vão no estado. Vai o valor normalizado em `[-1, +1]` quando a feature tem direção, ou intensidade `0..1` quando não tem. Funding positivo vira crowding, não sinal de compra. Nenhuma regra do tipo “Fear & Greed abaixo de 20 compra” será escrita.
