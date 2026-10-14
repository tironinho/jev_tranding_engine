# JEV confirmou, mas a ordem não abriu

Conferência de produção em 14/10/2026, a partir de 03:09 de Brasília, no build `c1492b0`.

## Motivo registrado

| Decisão | Hora de Brasília | Continuação JEV | Resultado do risco |
| --- | --- | --- | --- |
| BTC `af04d908-b259-4b59-89d1-41a9c9211eb5` | 03:06:44 | 57% | `MAX_CONSECUTIVE_LIVE_LOSSES` |
| ETH `702097c1-c377-44c5-b9ef-4b0f30280e4e` | 03:06:45 | 58% | `MAX_CONSECUTIVE_LIVE_LOSSES` |
| BNB `03e5281c-50b0-4f80-8a94-27169863dd7f` | 03:06:49 | 61% | `MAX_CONSECUTIVE_LIVE_LOSSES` |

As três decisões têm risco recusado e zero ordens. Seus pedidos efetivos contêm os sete grupos de evidência e a descrição da operação. O novo contrato chegou ao JEV.

O status reportava sequência de três perdas live, limite três e circuito aberto. As perdas vieram de `PROTECTION_FAILED`: SOL às 00:31:35 (-0,03338493 USDT), SOL às 00:34:35 (-0,024780995 USDT) e BTC às 00:36:33 (-0,03748108835 USDT). Somam -0,09564701335 USDT.

A regra existente conta perdas financeiras consecutivas no dia UTC, independentemente da causa do fechamento. Ela não é um cooldown por horas desde a última ordem. A virada desse dia é **14/10/2026 às 21h de Brasília**, equivalente a 15/10 às 00h UTC. A consulta de posições estava vazia; a conciliação não bloqueava entrada e não mostrava dívida.

## Outros limites continuam após a pausa

A avaliação de risco interrompeu esses casos no circuito; `economics` ficou nulo. Não houve aprovação econômica posterior. Calculando separadamente os níveis e taxas gravados no pedido, sem slippage ou juros, os R:R líquidos são aproximadamente BTC 0,342, ETH 0,490 e BNB 0,430, abaixo do mínimo 1,5. São estimativas dos planos, não resultados de execução ou previsões de lucro. O fim da pausa não garante que essas entradas passariam.

## Erro de apresentação corrigido localmente

O painel ainda lia `baseline_scores`, `range_60m` e flags do formato antigo. O novo contrato fornece `evidence` e `trade`, por isso aparecia “sem dado” apesar do envio completo.

- Os sete componentes agora são mostrados com suas unidades nativas, sem multiplicar percentuais novamente por cem ou limitar RSI/volume ao intervalo de scores.
- Rompimento é lido de `evidence.structure.event`; `BREAKDOWN` não aparece como “sem rompimento”.
- O plano mostra distâncias de alvo/stop, equilíbrio estimado por comissões e a regra temporal real, sem anunciar planos alternativos inexistentes.
- O score interno da baseline aparece separado do estado enviado ao provedor. O backend expõe `baseline_score` sem alterar o pedido gravado.
- O status informa `live_loss_reset_at`. O painel JEV mostra a pausa, a virada em Brasília e que as outras verificações continuam após ela. O cabeçalho mostra `LIVE PAUSED` em vez de entradas abertas durante o circuito.
- Não foram alterados limite de perdas, histórico, saldo, regras de alvo/stop ou autorização de ordens reais.

Validação: **406 testes Python passaram**, build de produção do frontend passou com tipos/lint, e a renderização dos componentes foi conferida com a decisão real de BTC: sete grupos, RSI 32,10, alvo 0,3536%, stop 0,2500%, equilíbrio 0,1998%, continuação 57% e horário de reset correto. Os dados e a prévia local estão em `exports/confirmed-no-entry-2026-10-14/`. A correção de apresentação só chegará à produção após push/deploy.
