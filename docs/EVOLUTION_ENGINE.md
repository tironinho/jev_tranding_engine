# Evolution Engine

A Evolution Engine observa o que a Trading Engine já registrou. Ela não envia ordem, não altera limite de risco e não promove nada para live.

## Impacto

O caminho de mercado, feature, estratégia, risco, paper e execução permanece o mesmo. A camada nova lê trades e decisões, grava versões, anomalias e experimentos, e expõe `/api/evolution/*` mais a página `/evolution`.

O que ela não faz nesta versão:

- não reescreve código;
- não chama API do Cursor;
- não gera hipótese quando não há modelo de pesquisa configurado;
- não promove para live;
- não mostra saúde ou expectativa sem amostra mínima.

Champions iniciais são o código que já existe: `B-001`, `J-001`, `OJ-001`. Métricas ficam `NO DATA` até haver amostra suficiente.

## Módulos

`apps/engine/app/evolution/`

| Módulo | Papel nesta versão |
| --- | --- |
| `observer` | Recorta trades por família e janela. |
| `anomaly_detector` | Só emite anomalia com amostra mínima e intervalo bootstrap abaixo de zero. |
| `researcher` | Chama `OPENAI_RESEARCH_MODEL` com JSON Schema. Sem modelo, sem cliente HTTP ou com resposta inválida, a decisão é `NO_ACTION`. Não inventa hipótese. |
| `hypothesis_generator` | Recusa pedido vago. |
| `experiment_memory` | Fingerprint e similaridade textual. Embeddings ficam para depois. |
| `experiment_manager` | Cria, cancela e rejeita. Branch `experiment/EXP-xxxxxx`, nunca `main`. |
| `agent_dispatcher` | `MockCodingAgentProvider` não mexe no repositório. `CursorCodingAgentProvider` não tem cliente. |
| `validation_pipeline` | Ordem dos gates e bloqueio de path protegido. |
| `promotion_engine` | Shadow e paper só com gates. Live sempre recusado. |
| `metrics_comparator` | Reusa o resumo de performance já existente. |
| `ranking` | Ordena anomalias e descarta duplicata da mesma família, código e símbolo. |
| `repository` | `MemoryEvolutionRepository` nos testes. `PostgresEvolutionRepository` quando o engine sobe com Postgres. |
| `service` | `run_analysis` persiste experimento e versão challenger. `AUTO_BUILD` grava a task do agente. Live continua desligado. |

Walk-forward, Monte Carlo e o ciclo de shadow/paper estão contratados nos gates. Eles não são aprovados com número inventado: sem artefato de validação, a promoção falha com `GATES_INCOMPLETE`.

## Ciclo

Uma vez por dia, se `AUTO_RESEARCH=true`, a análise roda. Sem amostra, o resultado é `NO_ACTION`. `AUTO_BUILD`, `AUTO_SHADOW_PROMOTION` e `AUTO_PAPER_PROMOTION` nascem desligados. O máximo diário de experimentos é 3, e mesmo assim o researcher desta versão não abre experimento sozinho.

## Próximas fases

E2 aprofunda o detector por regime e símbolo. E3 liga o researcher à Responses API com o prompt `evolution_researcher_v1`, separado do interpretador de mercado. E5–E7 preenchem os artefatos de OOS, walk-forward, Monte Carlo, shadow e paper usando o backtest que já existe. E8 só entra quando houver documentação real do agente Cursor.
