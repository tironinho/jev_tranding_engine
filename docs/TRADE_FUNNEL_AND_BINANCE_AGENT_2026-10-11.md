# Funil de decisões e integração Binance — 11 de outubro de 2026

## Diagnóstico do período atual

A janela posterior ao deploy das 17:24 UTC contém 140 avaliações por estratégia:

| Etapa | Resultado |
|---|---:|
| Baseline `NO_TRADE` | 129 |
| Baseline acionável | 11 |
| Sinais acionáveis LONG | 11 |
| Sinais acionáveis SHORT | 0 |
| Aprovações do JEV | 0 |
| Vetos `JEV_LOW_CONTINUATION` | 11 |
| Falhas `JEV_UNAVAILABLE` | 0 |
| Ordens live enviadas | 0 |

Dos 129 bloqueios da baseline, 126 ficaram abaixo do score mínimo absoluto de 0,55. Os demais motivos se sobrepõem: 59 decisões divergentes, 35 com fluxo contra a direção e 28 com baixa liquidez. Em modo live, candidatos abaixo do threshold não chegam ao JEV. A baseline é, portanto, o primeiro gargalo.

Os 11 sinais que passaram pela baseline eram LONG, com confiança de 0,55 a 0,686. O JEV respondeu normalmente, mas recusou todos por probabilidade de continuação abaixo de 0,55. Não houve veto por reversão, falso rompimento, qualidade de inteligência ou expectativa líquida. Mesmo que algum desses sinais tivesse sido confirmado, o risco live recusaria a entrada porque `live_longs_enabled` permanece falso após a amostra live negativa.

O resultado é coerente com a configuração: o sistema só pode abrir um SHORT confirmado enquanto os longs estiverem suspensos. Não há evidência de falha de execução nessa janela.

## Limitações encontradas no JEV

O prompt atual pede a probabilidade de o alvo ser atingido antes do stop no horizonte do plano. Para um alvo de pelo menos 3R, o ponto de equilíbrio bruto está perto de 25%, e a expectativa mínima configurada, com haircut, exige aproximadamente um terço de probabilidade. O veto fixo de 55% é muito mais conservador e pode estar misturando duas grandezas diferentes: continuação direcional e probabilidade de atingir um alvo distante antes do stop.

Esse threshold não foi reduzido nesta alteração. A probabilidade do JEV ainda não está calibrada contra resultados observados, e diminuir o piso diretamente poderia liberar risco sem evidência suficiente.

Também havia perda de contexto na requisição. O JEV recebia tendência, retorno e fluxo de um minuto, mas não recebia diretamente RSI, ATR, volume relativo, spread e desequilíbrio do book. Quando a qualidade agregada de provedores externos ficava abaixo do piso, todo o bloco de inteligência era removido, inclusive microestrutura válida da Binance. Isso deixava o modelo com pouca informação para estimar se um alvo de 3R era alcançável em 60 minutos.

## Correção aplicada

O estado enviado ao JEV agora inclui apenas valores normalizados, sem preços absolutos:

- retornos de 1, 5, 15 e 60 minutos;
- RSI, ROC, ATR normalizado e volatilidade realizada;
- volume relativo e z-score de volume;
- fluxo agressor de 1 e 5 minutos;
- desequilíbrio do book em 5, 10 e 20 níveis;
- spread, posição na faixa e distância do VWAP;
- distância percentual do stop e do alvo;
- distância do stop e do alvo em ATR;
- distância do alvo em múltiplos da faixa de 60 minutos.

Os vetos agora registram probabilidade de continuação, piso exigido, reversão, falso rompimento, expectativa líquida, probabilidade sob estresse e plano avaliado. Isso permite calibrar o JEV com dados observados em vez de ajustar o threshold por impressão.

Também foi adicionado um analista Binance determinístico em modo `READ_ONLY_SHADOW`. Ele consolida os dados públicos já coletados pela engine em tendência, momentum, fluxo, liquidez, volatilidade, alertas de risco, viés direcional e qualidade. O resultado é persistido nos metadados de cada decisão, aparece nos logs inclusive quando a baseline encerra antes do JEV e segue no contexto do JEV quando as fontes externas estão incompletas. O componente declara `can_execute=false`: ele fornece contexto e nunca envia nem autoriza ordens.

## Como usar Binance AI Agent Skills

O pacote oficial da Binance é uma coleção de skills que orienta agentes a usar o `binance-cli`. Ele oferece dados públicos, endpoints Spot, Futures e Margin, consulta de conta e execução. Ele não é um novo modelo de previsão e não deve substituir o executor determinístico da engine.

A integração indicada para esta engine tem três camadas:

1. **Pesquisa e diagnóstico:** instalar as skills no ambiente de desenvolvimento para consultas ad hoc, comparação de klines, book, trades, comissões e histórico de ordens. Essa camada não participa do loop de ordens.
2. **Contexto read-only:** incorporar somente endpoints oficiais úteis como fontes estruturadas, com timestamp, TTL, qualidade e persistência. A engine já consome klines, book, trades, open interest, funding e juros de margem; a mudança atual melhora o uso dessas informações pelo JEV.
3. **Execução:** manter `BinanceExecutionProvider`, Risk Engine, idempotência, stop obrigatório e circuit breakers como único caminho de ordem. O `binance-cli` não deve ser chamado como subprocesso dentro do hot path.

Para pesquisa local, a instalação documentada pela Binance é:

```bash
npx skills add https://github.com/binance/binance-skills-hub
```

A skill oficial foi instalada no ambiente local do Codex. Ela ficará disponível em uma nova sessão do Codex. O `binance-cli` atual não oferece binário nativo para Windows, portanto não foi incorporado ao processo da engine. A aplicação continua usando diretamente as APIs oficiais já integradas, que também são adequadas ao contêiner Linux do Render e preservam o caminho de risco e execução existente.

O ambiente requer Node.js 22 ou superior. Dados públicos não precisam de chave. Consultas de conta devem usar uma chave read-only. Se a skill for usada para testes de execução, deve começar no Testnet. Em mainnet, a chave deve ter restrição de IP, permissão mínima, saques desabilitados e saldo limitado em subconta.

## Próxima decisão de estratégia

Primeiro é necessário observar os novos logs de veto e rotular cada candidato com `target_before_stop`. Depois de pelo menos 30 a 50 candidatos sob o mesmo prompt e a mesma regra, devem ser produzidas curvas de calibração por faixa de probabilidade. Somente então o piso de 0,55 pode ser substituído por um piso econômico calibrado.

Longs devem continuar bloqueados no live até que os candidatos avaliados com o contexto novo demonstrem expectativa líquida positiva. A coleta continua em decisões persistidas e pode ser auditada sem enviar ordens.
