# Auditoria de entradas, perdas e validação de rentabilidade

Data: 11/10/2026 em Brasília. Corte fixo dos dados: 12/10/2026 às 01:46 UTC, equivalente a 11/10 às 22:46 em Brasília. Código: `aa71190677ca4ead663ae7c18cb31cfd325e9986`.

## Conclusão

A ausência de novas ordens tem explicação no funil: 200 de 205 avaliações da baseline não geraram sinal; os cinco sinais restantes foram vetados pelo Jev e nenhum alcançou a expectativa estimada mínima de 0,10R. Na janela atual não apareceram timeouts, expiração, saturação de fila ou rejeição de execução live. As melhorias operacionais recentes funcionaram nessa janela, mas não demonstram vantagem financeira.

As perdas anteriores têm outra explicação: na tela de 25 operações, o ganho bruto agregado de aproximadamente 0,08 USDT virou perda líquida de 0,43 USDT. A diferença de aproximadamente 0,51 USDT entre bruto e líquido consumiu a vantagem bruta. A taxa de acerto observada foi 20%, o profit factor 0,62 e a expectativa -0,41R.

Também há uma lacuna concreta na validação: o replay ignora toques intrabar de stop/alvo; o CLI força Jev mock; challengers não recebem mercado nem conta paper própria. A engine opera uma baseline paper e tem testes funcionais, mas sua estrutura de Evolution não executa a comparação de rentabilidade descrita pelos nomes dos estágios.

## Fontes e limites

- Logs Render da engine no deploy `aa71190`, instância `srv-db36slcs728c73bhs3l0-xxzcl`, entre 01:04:28 e 01:46 UTC; engine live desde 01:04:26 UTC. Logs antigos e deploys estão na auditoria anterior.
- Cinco imagens fornecidas pelo usuário. Os números das 25 operações vieram da tela, não de uma consulta a seus registros individuais. As duas últimas imagens são duplicadas.
- Código local e configuração versionada. Defaults de Evolution e taxas não comprovam valores de ambiente ou controles persistidos em produção.
- Candles Binance públicos de 1 minuto, fechados até o corte. Preço de referência: último fechamento conhecido antes do log. A vela da decisão é excluída da janela futura para não usar movimentos anteriores ao sinal.
- A conexão PostgreSQL fornecida anteriormente foi consultada em modo somente leitura com TLS: retornou zero tabelas no schema `public`. Esse banco Render não é o banco utilizado pela produção. Logs identificam a produção no Neon; seus endpoints privados exigem autenticação. A URL do dashboard foi solicitada para localizar o acesso existente, mas ainda não foi informada.
- Portanto, não foi possível cruzar no Neon todos os snapshots, planos originais, fills, comissões e juros por `decision_id`. Nenhuma credencial foi gravada neste relatório ou nos artefatos.
- Observações com candles não reproduzem fills, book, preços no segundo da decisão ou PnL real. Janelas sobrepostas da mesma sessão não são amostras independentes.

Artefatos locais, ignorados pelo Git: `exports/profit-audit-2026-10-12/{logs.json,summary.json,matured_candidates.json,*-klines.json,reproductions.json}`. Scripts reproduzíveis: `matured_outcomes.py` e `reproduce_replay_stop.py`.

## Por que não abrimos novas operações

| Etapa, na janela atual | Resultado |
| --- | ---: |
| Avaliações baseline | 205 |
| Baseline NO_TRADE, todas BELOW_THRESHOLD | 200 |
| Baseline acionável | 5: dois LONG e três SHORT |
| Jev NO_TRADE final | 205 |
| Vetos Jev dos sinais acionáveis | 5 |
| Candidatos sombra | 91: 35 avaliados/vetados, 56 adiados |
| Falhas de provedor/timeout encontradas | 0 |
| Ordens novas autorizadas por baseline_jev | 0 |

O adiamento da sombra é esperado pelo scheduler: três vagas para sinais ativos, uma para sombra, intervalo mínimo de 120s por símbolo e prioridade para avaliações ativas. Ele não impediu nenhuma das cinco avaliações acionáveis. Um candidato sombra permanece NO_TRADE mesmo que o modelo o confirme; serve para observação e não constitui uma conta paper experimental.

Horários abaixo em Brasília; probabilidade Jev não calibrada, relativa ao plano proposto:

| Horário | Candidato | Jev | EV líquido previsto |
| --- | --- | ---: | ---: |
| 22:06:06 | XRP LONG | 44% | +0,0030R |
| 22:06:08 | ETH LONG | 48% | -0,0003R |
| 22:33:09 | BTC SHORT | 34% | -0,3254R |
| 22:33:11 | BNB SHORT | 37% | -0,2556R |
| 22:44:09 | BNB SHORT | 32% | -0,3719R |

Todos receberam `JEV_LOW_CONTINUATION`. Mesmo retirar apenas o corte de continuação de 55% não faria esses planos atingir o mínimo econômico de 0,10R. A baseline usa score, não probabilidade; seu corte habitual é 0,55, com exceção de reclaim confirmado em 0,40.

Não apareceram eventos `live_risk_reject`, `live_blocked`, `EXPIRED_SIGNAL` ou `QUEUE_SATURATED` na janela consultada. Saldo, proteção e Binance não explicam esse conjunto de recusas: o fluxo não chegou a uma ação para executar. A restrição live de LONG continua sendo uma condição adicional posterior ao Jev, não a causa imediata desses vetos.

Persistir snapshot ainda leva mediana de 2,71s e p95 de 4,01s; idade inicial p95 de 5,51s. É um ponto de desempenho, mas não houve evidência de expiração nesta janela. A mediana zero de `jev_ms` inclui NO_TRADE/adiamentos e não mede a latência típica do modelo.

## Mercado: acertar a direção não significa atingir o plano

Entre 22:05 e 22:46 em Brasília:

| Símbolo | Retorno de preço | Amplitude máxima/mínima relativa ao início |
| --- | ---: | ---: |
| BTC | -0,390% | 0,446% |
| ETH | -0,386% | 0,506% |
| SOL | -0,554% | 0,679% |
| BNB | -0,330% | 0,411% |
| XRP | -0,574% | 0,701% |

Os LONGs de ETH/XRP às 22:06 apontavam contra a queda que se seguiu. Os SHORTs das 22:33/22:44 chegaram depois de parte do movimento. Nos SHORTs das 22:33, o máximo favorável posterior observado foi somente 0,081% em BTC e 0,109% em BNB até o corte. Essas janelas têm 12 minutos, e a última BNB apenas um minuto: ainda não permitem classificar o resultado completo de 60 minutos.

Reavaliei os 370 candidatos sombra da sessão anterior com 60 velas futuras completas. 247 terminaram na direção candidata, mas o retorno direcional médio foi apenas +0,0564%; o maior deslocamento favorável foi 0,6199%. Nenhum atingiu 0,75% nessa janela de referência. O plano mostrado na tela exige alvo bruto de aproximadamente 0,75%, antes de stop de aproximadamente 0,25%.

Isso demonstra incompatibilidade entre a amplitude observada e essa geometria no recorte, não calibração correta do Jev. Os planos exatos de todos os candidatos e o preço dentro da vela da decisão não foram obtidos. Na sessão mais antiga houve dois possíveis falsos negativos durante a queda, descritos no relatório anterior; não foram classificados como lucro perdido confirmado.

O resumo Binance é determinístico e reaproveita os inputs de mercado. `BULLISH/BEARISH` não é probabilidade de atingir alvo nem evidência independente; qualidade 1,0 significa completude dos campos, não taxa de acerto.

## Por que as operações anteriores perderam dinheiro

Na tela: 25 trades, cinco ganhos, 20 perdas, 20% de acerto, PF 0,62, expectativa -0,41R, bruto +0,08 e líquido -0,43 USDT. Valores arredondados permitem estimar:

- Arrasto agregado entre bruto e líquido: aproximadamente 0,51 USDT.
- Resultado líquido médio: aproximadamente -0,0172 USDT por trade.
- Ganho bruto relativo ao notional acumulado de entrada de 415,68: aproximadamente 0,019%; arrasto aproximadamente 0,123%. Isso é diagnóstico de escala, não taxa de comissão contratual.
- Com os payoffs realizados dessa mesma amostra, PF 0,62 e cinco ganhos/20 perdas implicam ganho médio de aproximadamente 2,48 perdas médias. O equilíbrio retrospectivo seria aproximadamente 28,7% de acerto, superior aos 20% observados. Isso não prevê desempenho futuro.

Muitas saídas antigas foram `TIME`, além de stops e uma `PROTECTION_FAILED`. Agora a saída temporal está desativada e o stop é movido em 1R/2R; misturar versões impede atribuir os 25 resultados à política nova. Os três fechamentos paper da janela atual também não equivalem aos valores live: o paper usa capital de referência de 10.000, enquanto a tela mostra ordens live pequenas.

### Reconstrução econômica do ETH da imagem

Entrada 2.540,18; stop 2.546,5304; alvo 2.521,1286. O cálculo da engine reproduziu o R:R líquido da tela com 0,075% de taxa por lado, 2bp de estresse de saída e 1bp de juros estimados:

| Medida | Valor |
| --- | ---: |
| Stop / alvo bruto | 0,25% / 0,75% |
| R:R bruto / líquido | 3,00 / 1,3263 |
| Probabilidade de equilíbrio líquido, sem haircut | 42,99% |
| Probabilidade bruta para EV >=0,10R após haircut 5 pontos | 52,28% |
| Jev bruto / cenário de estresse | 30% / 25% |
| EV do cenário de estresse | -0,4184R |

Os custos próximos de 0,18% pesam muito sobre um stop de 0,25%. Aumentar quantidade ou alavancagem não corrige essa proporção. Encurtar alvo melhora alcançabilidade, mas piora payoff; ampliar stop muda risco e equilíbrio. Ambos precisam de avaliação comparativa com custos.

A taxa de 0,075% foi usada para reproduzir o plano, não confirmada como comissão atual da conta. A documentação oficial explica que comissões dependem da conta, símbolo e descontos; é necessário conferir os fills e a fonte de comissão aplicada: [Binance Commission Rates](https://developers.binance.com/en/docs/products/spot/faqs/commission_faq).

## Por que ainda não existe um teste convincente de lucro

1. **Falha reproduzida no replay.** Abri uma posição paper local com stop 98,56 e passei uma vela seguinte com mínima 97,56 e fechamento 100. A mínima atravessou o stop; a posição continuou aberta e nenhum trade foi fechado. `_replay_bar` usa preço/book no fechamento e só então gerencia posições. Stops, alvos e stop móvel intrabar ficam incorretos. Isso afeta a confiabilidade em ambas as direções, não autoriza concluir que todo backtest superestimou lucro.
2. **Jev real não é testado pelo CLI.** `backtest/__main__.py` força `jev_provider="mock"`; o replay usa book sintético e não reproduz aggTrades/fluxo original. Um resultado desse CLI não valida a estratégia live com Jev real. As cotações sintéticas também usam spread de 2bp, enquanto o book construído começa com 1bp.
3. **Evolution registra etapas, sem executar toda a pesquisa.** `historical_gates` recebe booleanos; não roda OOS, walk-forward ou Monte Carlo. Challengers draft não entram no loop de mercado nem ganham conta paper própria, conforme a própria documentação do projeto.
4. **Agendamento e amostra dificultam a reação.** `_daily_loop` espera 24h desde cada inicialização. Reiniciar com deploy antes desse prazo adia a primeira análise automática. O detector de expectativa negativa exige, por default, 100 trades por estratégia/símbolo, não 100 no total. Os 25 da tela não satisfazem isso. Defaults `auto_build`, `auto_shadow_promotion` e `auto_paper_promotion` são false; coding agent é mock. Ambiente/controles restaurados precisam ser conferidos antes de atribuir seu estado efetivo.
5. **Rótulo diferente da pergunta do Jev.** `FutureReturnLabeler` guarda retorno até 15m, mas não alvo-antes-do-stop em 60m. A execução move stop e não tem saída temporal ativa. Precisamos medir separadamente resultado do plano fixo previsto e resultado da política real para calibrar probabilidades.
6. **Custo estimado pode diferir do executado.** O Jev recebe `fee_provider.estimate`; a consulta de taxa em execução ocorre posteriormente. Há fallback silencioso e cache sem TTL. Risk recalcula custos reais conhecidos, mas não repete o gate de EV mínimo com essa nova economia. Não foi verificado se o fallback ocorreu em produção; a divergência é uma lacuna do código.
7. **Juros realizados não são alocados ao trade margin.** O plano inclui juros estimados, mas `accounts.close_position` usa bruto menos comissões mais funding; `_finish_exit` calcula funding, não juros reais de empréstimo margin. O saldo pode refletir dívidas/juros, enquanto o `net_pnl` de cada trade não concilia esse custo. Isso pode tornar o resultado exibido otimista, não explicar artificialmente a perda já negativa.

Os 261 testes funcionais aprovados no ajuste anterior verificaram comportamentos do software. Não eram uma demonstração de rentabilidade. Os últimos ajustes trataram disponibilidade, prioridade e observabilidade; faltou a validação financeira experimental descrita acima.

## Próximos ajustes necessários, na ordem

1. Corrigir a simulação de barreiras intrabar, gaps e stops móveis, com política conservadora explícita quando uma vela toca stop e alvo e a ordem dos eventos é desconhecida. Usar dados mais granulares quando disponíveis.
2. Conciliar por ordem/trade taxa estimada, comissão dos fills, juros realizados, slippage e resultado líquido. Atualizar a taxa antes de selecionar planos e repetir EV com a economia final.
3. Persistir desfechos de 60m para cada plano vetado/aceito, sem olhar o futuro na decisão; separar resultado fixo e política executada. Deduplicar ou tratar estatisticamente janelas sobrepostas.
4. Executar challengers isolados em paper sobre os mesmos snapshots/fluxo de mercado, comparando baseline, Jev e variantes de timing/geometria. A sombra atual só observa o modelo, não mede lucro de uma conta.
5. Fazer comparação por regimes e períodos fora da amostra, incluindo sessões distintas, sem escolher parâmetro pelo mesmo trecho usado para medir ganho. Calibrar Jev por resultado de alvo e não pelo score da baseline.
6. Corrigir agendamento persistente de Evolution e separar alertas precoces de perda dos critérios rigorosos de promoção. Isso permite iniciar investigação antes de 100 trades, sem promover estratégia com amostra pequena.

Nenhum parâmetro, código de execução, ordem ou serviço de produção foi alterado nesta auditoria. Para concluir o cruzamento individual dos 25 trades e dos planos originais falta acesso autenticado ao Neon utilizado pelo dashboard.

## Referências de código

- Funil e shadow: `apps/engine/app/strategies/runners.py`, `apps/engine/app/providers/jev/scheduler.py`, `apps/engine/app/strategies/baseline_score.py`.
- Custos e geometria: `apps/engine/app/risk/plans.py`, `apps/engine/app/risk/economics.py`, `apps/engine/app/risk/engine.py`, `apps/engine/app/providers/fees.py`.
- Saída/PnL/rótulos: `apps/engine/app/accounts.py`, `apps/engine/app/service.py`.
- Validação: `apps/engine/app/backtest/runner.py`, `apps/engine/app/backtest/__main__.py`, `apps/engine/app/evolution/{service.py,anomaly_detector.py,validation_pipeline.py}`, `docs/CHAMPION_CHALLENGER.md`.
- Histórico anterior: `docs/DEPLOY_MARKET_CROSS_REVIEW_2026-10-12.md`.
