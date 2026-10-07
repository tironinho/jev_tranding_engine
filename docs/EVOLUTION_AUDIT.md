# Auditoria da Evolution Engine

Estado conferido no código antes do P0, e o que esse passo passou a fazer de verdade.

## O que já existia e continua

A Trading Engine avalia mercado, baseline, Jev, OpenAI, risco e paper. A Evolution Engine lê trades, não envia ordem e recusa promoção para live. Champions de bootstrap: `B-001`, `J-001`, `OJ-001`. Tabelas Alembic de versão, experimento, anomalia, relatório e task de agente já existiam. O estado usado pela API estava em dict e list.

## Falhas confirmadas

- `EvolutionResearcher.analyze` devolvia `RESEARCH_CALL_NOT_ENABLED_IN_THIS_VERSION` mesmo com modelo configurado. Nenhuma chamada HTTP saía.
- `run_analysis` incrementava `experiments_created` sem criar `Experiment` nem versão challenger.
- Anomalias não eram ordenadas. O detector só emitia `NEGATIVE_EXPECTANCY` e `STRATEGY_DECAY`.
- `ExperimentMemory`, versões e relatórios morriam no restart.
- Shadow de challenger, conta paper isolada, backtest champion contra challenger, OOS, walk-forward e Monte Carlo não executam. Os gates de promoção exigem artefatos que ninguém grava, então a promoção histórica falha com `GATES_INCOMPLETE`.
- `python -m app.evolution.paths` imprimia sucesso quando não havia base de diff.
- `CursorCodingAgentProvider` não chama rede. O SDK oficial é `cursor-sdk` / `@cursor/sdk` (`Agent.prompt`). Ligá-lo dentro deste processo editaria o checkout da Trading Engine. Fica bloqueado até existir worktree isolada e `CURSOR_API_KEY`.

## P0 implementado

- `EvolutionResearchClient` chama a Responses API com JSON Schema `evolution_research`. Sem modelo, sem chave, sem cliente ou com JSON inválido, a decisão é `NO_ACTION`. Não há hipótese inventada.
- `run_analysis` ordena anomalias, valida a hipótese, recusa path protegido e duplicata, cria `Experiment`, aloca `B-002` / `J-002` / `OJ-002` e persiste.
- `experiments_created` conta só experimento persistido.
- `AUTO_BUILD` grava task no `MockCodingAgentProvider`. O mock continua sem escrever no disco.
- `MemoryEvolutionRepository` e `PostgresEvolutionRepository`. Com Postgres saudável no boot, o engine recarrega versões, experimentos, memória, anomalias, relatórios, tasks e controls.
- O check de path protegido falha fechado sem base de diff. O CI busca o histórico inteiro (`fetch-depth: 0`).

## Ainda não implementado

Dataset histórico da Binance, replay pelo mesmo feature engine, backtest de experimento, OOS com trava, walk-forward, Monte Carlo, shadow runtime, conta paper do challenger, troca transacional de champion, rollback, agente Cursor, branch `experiment/*` e pull request. Não há número de performance desses estágios porque eles não rodaram.
