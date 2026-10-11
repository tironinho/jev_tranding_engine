# Revisão da estratégia live — 11 de outubro de 2026

## Escopo e fontes

Esta revisão cruza os fechamentos registrados nos logs do Render entre 7 e 11 de outubro, eventos de deploy, falhas de execução, configuração atual e o banco PostgreSQL informado para acesso externo.

O PostgreSQL informado está acessível, mas não contém tabelas de usuário. Ao mesmo tempo, o engine registra `book restored from postgres` em produção. Portanto, o `DATABASE_URL` do serviço aponta para outro banco, ou o endereço informado não é o banco usado pelo engine. Sem essa correspondência não foi possível relacionar cada resposta do JEV, seus scores e o plano escolhido ao resultado final. Os logs permitem medir os fechamentos e incidentes, mas não substituem o histórico de decisões persistido.

## Resultado observado

Foram encontrados 101 fechamentos:

| Estratégia | Operações | Vitórias | Derrotas | Taxa de acerto | PnL líquido registrado |
|---|---:|---:|---:|---:|---:|
| `baseline` | 39 | 5 | 31 | 12,8% | -693,9470 USDT |
| `baseline_jev` | 56 | 16 | 39 | 28,6% | +80,6406 USDT |
| `baseline_openai_jev` | 6 | 0 | 6 | 0,0% | -65,0034 USDT |

Esses valores agregados misturam paper e live, tamanhos de conta diferentes e muitas versões. Eles mostram que o JEV filtrou entradas ruins da baseline, mas não servem para estimar retorno financeiro live.

A amostra identificável como `baseline_jev` live começa em 8 de outubro às 14:54 UTC. Ela contém 25 operações, 6 vitórias, 19 derrotas e PnL líquido de -0,2012 USDT. A taxa de acerto foi 24,0%; o intervalo de Wilson de 95% vai de 11,5% a 43,4%, evidenciando a incerteza da amostra pequena.

| Saída live | Operações | PnL líquido | Média por operação |
|---|---:|---:|---:|
| `TARGET` | 4 | +0,8036 | +0,2009 |
| `STOP` | 12 | -0,8017 | -0,0668 |
| `TIME` | 8 | -0,2041 | -0,0255 |
| `PROTECTION_FAILED` | 1 | +0,0010 | +0,0010 |

Nos 16 resultados que chegaram a alvo ou stop, houve quatro alvos: exatamente 25%. Um alvo de 3R precisa de 25% de acerto antes de custos. Taxas, slippage, juros e erros de previsão elevam o ponto de equilíbrio; por isso, esse subconjunto não demonstrou vantagem líquida. As oito saídas por tempo retiraram mais 0,2041 USDT e já foram desativadas na configuração atual.

| Ativo live | Operações | Vitórias | Derrotas | PnL líquido |
|---|---:|---:|---:|---:|
| BNBUSDT | 5 | 2 | 3 | +0,1091 |
| ETHUSDT | 3 | 1 | 2 | +0,0474 |
| BTCUSDT | 5 | 1 | 4 | +0,0354 |
| XRPUSDT | 6 | 1 | 5 | -0,1497 |
| SOLUSDT | 6 | 1 | 5 | -0,2434 |

SOL e XRP concentram a perda, mas seis operações por ativo são insuficientes para criar uma regra permanente. Excluí-los agora ajustaria a estratégia ao ruído dessa amostra.

## JEV e execução

Os logs contêm 285 decisões `JEV_UNAVAILABLE`: 89 em 8 de outubro, 110 em 9 de outubro, 84 em 10 de outubro e 2 em 11 de outubro. O sistema falhou de forma fechada nessas ocorrências (`NO_TRADE`), então elas não geraram diretamente as perdas. Elas reduzem a cobertura, criam viés de seleção no histórico e impedem calibrar o modelo enquanto a causa não estiver categorizada.

Também foram encontrados 37 bloqueios live, além de falhas antigas na colocação ou substituição de stops. Em 11 de outubro, uma entrada SOL não conseguiu criar a proteção por saldo insuficiente; o fail-safe encerrou a posição como `PROTECTION_FAILED`. O código atual já calcula a reserva para o stop, tenta recuperação controlada e encerra a posição se a proteção falhar.

Houve 49 deploys em aproximadamente 61 horas. O último fechamento analisado ocorreu às 03:52 UTC, e a versão atual (`cf5e781`) começou a ser implantada às 04:13 UTC. Logo, não há nenhum trade fechado que valide a configuração atual. Comparar todo o período como se fosse uma única estratégia produziria uma calibração inválida.

## Decisão de estratégia

Foram preservados os controles atuais que atacam os problemas observados:

- alvo mínimo de 3R;
- filtro de expectativa líquida após custos e haircut;
- saída por tempo desativada;
- longs live desativados, ainda observáveis em paper;
- falha do JEV mantendo `NO_TRADE`;
- stop obrigatório e fechamento de emergência quando a proteção falha.

Não foram alterados pesos da baseline, piso de continuação de 0,55 nem filtros por símbolo. O histórico persistido necessário para medir a calibração do JEV não está no banco informado, e a versão atual ainda não possui resultados. Alterar esses parâmetros agora seria sobreajuste.

Foi acrescentado um circuito de perda: três perdas live consecutivas na mesma data UTC pausam novas entradas live até o dia seguinte. O paper e a coleta de dados continuam ativos. Os dados históricos contêm sequências diárias de seis e quatro perdas. Não é possível atribuir uma economia retrospectiva, pois posições posteriores podem já estar abertas quando a terceira perda fecha.

## Correções operacionais aplicadas

- A reconciliação feita antes de uma ordem live agora recebe somente posições live. Uma posição paper não pode mais bloquear ou distorcer a conta Binance.
- Toda decisão acionável passa a registrar lado, confiança, continuação do JEV, piso exigido, reversão, falso rompimento, expectativa líquida, plano e versão da regra.
- Indisponibilidades do JEV passam a ser classificadas como timeout, HTTP, conexão, configuração ou resposta inválida, sem gravar payloads sensíveis.
- O logger `httpx` deixa de registrar URLs completas em nível INFO, evitando copiar assinaturas temporárias da Binance para os logs.

## Próxima janela de validação

A configuração deve permanecer estável durante a próxima amostra. A revisão seguinte deve separar por hash/commit e exigir pelo menos 30 a 50 fechamentos da mesma versão antes de mexer nos limiares. Devem ser comparados:

- expectativa líquida e taxa de alvo por lado, ativo e classe da baseline;
- calibração do `jev_continuation` em faixas de probabilidade;
- diferença entre probabilidade prevista e frequência real de alvo antes do stop;
- custo real total por operação e diferença entre R planejado e R realizado;
- indisponibilidade e latência do JEV;
- incidência de stop não protegido, reconciliação e bloqueios por sequência de perdas.

Antes dessa etapa, o `DATABASE_URL` usado pelo serviço deve ser reconciliado com o banco acessado externamente. Sem isso, o histórico detalhado fica preso no banco atual de produção e não pode ser auditado pelo mesmo caminho usado nesta análise.
