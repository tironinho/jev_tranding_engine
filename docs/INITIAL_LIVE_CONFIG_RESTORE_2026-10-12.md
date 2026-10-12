# Dez primeiros trades live e restauração da configuração inicial

**Referência corrigida após esclarecimento do usuário:** este documento registra a restauração da primeira entrada (`d5b2f6d`). O usuário escolheu depois a configuração vigente ao completar os dez trades (`a15bc12`), que difere em classificação, corte SHORT, rompimentos e alvo. Ver `FIRST_TEN_CONFIG_CORRECTION_2026-10-12.md` para a política ativa corrigida.

## Identificação do período

Live foi ativado em 08/10/2026 às 11:54:22 de Brasília (14:54:22 UTC), no deploy `9288af7`. A primeira versão que conseguiu executar ordens com o mínimo de 5 USDT foi `d5b2f6d`, live às 12:19:38 de Brasília. Logs registram chamadas de ordem pelo gateway de Singapura às 12:19:54/55 e o primeiro fechamento ETH às 12:32.

Referência adotada para restaurar a política de entrada inicial: **`d5b2f6d`**. “Dez primeiros” significa dez fechamentos cronológicos `baseline_jev` posteriores à ativação live. Sem as posições originais do Neon, não foi possível ordenar todos os dez por horário de entrada ou vincular cada um ao commit de entrada.

## Os dez resultados registrados

Horários em Brasília. Valores líquidos são os números arredondados registrados nos logs antigos, sem conciliação nova de comissões e juros da Binance.

| # | Fechamento | Símbolo | Saída | Líquido registrado, USDT |
| --- | --- | --- | --- | ---: |
| 1 | 08/10 12:32:32 | ETHUSDT | TARGET | +0,1343 |
| 2 | 08/10 14:00:52 | BNBUSDT | TARGET | +0,2813 |
| 3 | 08/10 16:38:51 | SOLUSDT | TIME | -0,0097 |
| 4 | 09/10 01:06:27 | BTCUSDT | TARGET | +0,2616 |
| 5 | 09/10 01:37:35 | SOLUSDT | STOP | -0,0539 |
| 6 | 09/10 01:44:49 | XRPUSDT | TIME | -0,0384 |
| 7 | 09/10 02:48:13 | XRPUSDT | STOP | -0,0550 |
| 8 | 09/10 10:32:31 | SOLUSDT | STOP | -0,0472 |
| 9 | 09/10 10:41:16 | XRPUSDT | TARGET | +0,1264 |
| 10 | 09/10 10:52:19 | BTCUSDT | STOP | -0,0830 |

Resultado: **+0,5164 USDT**, quatro ganhos, seis perdas, 40% de acerto e profit factor aproximado de **2,80**. Ganhos: +0,8036; perdas: -0,2872. Os dois fechamentos por tempo somaram -0,0481.

O resultado positivo veio do tamanho dos quatro ganhos frente às seis perdas; não foi necessário ganhar a maioria das operações. É uma amostra de dez fechamentos, em uma sessão de mercado e com vários deploys. Não comprova que restaurar os parâmetros reproduzirá o lucro.

## Mudanças durante a amostra

Os dez fechamentos não pertencem a uma configuração única. Depois da entrada original, `2b09c77` acrescentou classificação por alinhamento dos scores; `b7aa950`, incorporado nesse período, introduziu confirmação SHORT a partir de 40%; `5fc0e2e` passou a tratar rompimentos abaixo do corte com redução de tamanho; `e708555` alterou dimensionamento/alcance do alvo. Outras versões corrigiram saldo, taxas em ativo base, persistência e encerramento.

A restauração usa o início executável, sem escolher retrospectivamente uma versão vencedora entre essas mudanças. O corte Jev inicial era 55%, não 40%. A interpretação de retornar à política do início live foi comunicada durante o trabalho.

## Política restaurada no código

| Componente | Configuração restaurada |
| --- | --- |
| Baseline | `baseline_weights_v1`, score absoluto mínimo 0,45 |
| Scores | Fórmulas do commit `d5b2f6d`, sem os gates posteriores de classificação/reclaim/contexto 15m |
| Jev | Pedido compacto original e três perguntas; uma avaliação por sinal válido |
| Baseline recusada | Encerra NO_TRADE; não envia candidato sombra ao Jev |
| Corte Jev | Mínimo 55%, também respeitando a fórmula histórica de taxa/stop se ela exigir mais |
| Reversão / falso rompimento | Vetos acima de 65% / 80%, como no início |
| Alvo | `min(2,5 × distância do stop, amplitude de 60m)` |
| R:R líquido | Entrada exige pelo menos 1,5 após custos de execução |
| Planos dinâmicos / gate de EV 0,10R | Desativados neste perfil; preservados para o perfil posterior |
| Direções live | LONG e SHORT disponíveis na política |
| Stop móvel em 1R/2R | Desativado neste perfil, como no início |
| Saída temporal | Após 60m, fecha perdedora/empatada; vencedora segue até alvo ou stop, mantendo o cálculo atual após custos |
| Baseline sem Jev | Default shadow, como no início; overrides existentes continuam sendo respeitados |

O pedido original dizia “alvo antes do stop” sem enviar a geometria do plano. Isso foi restaurado por solicitação de retorno à política inicial, com versão explícita `jev_initial_live_2026_10_08`. Sua resposta continua não calibrada e não deve ser confundida com a probabilidade validada do plano exato de 60 minutos do perfil posterior.

## Correções atuais preservadas

Não foi realizado um checkout ou revert completo do repositório. Foram mantidos gateway, reconciliação, lotes e comissões de execução, persistência, proteção obrigatória, reserva para stop SHORT, tentativas de recuperação, limite de margem, risco agregado, circuito de três perdas live, frescor de book/preço e logs. O orçamento atual de 30s e o scheduler também permanecem; retornar ao orçamento antigo de 12s reintroduziria expiração desnecessária.

A infraestrutura e as fontes atuais de contexto externo continuam, com a filtragem de qualidade. Portanto, restauramos a política de decisão/saída identificada, não o payload histórico inteiro byte a byte nem os defeitos de execução antigos.

Nenhuma credencial, saldo, histórico, posição ou override no Neon foi apagado. Modos persistidos e flags de ambiente existentes continuam sendo respeitados; os parâmetros da política vêm dos arquivos versionados. Overrides antigos de `min_net_rr` no runtime não substituem o corte restaurado de 1,5.

## Validação

**268 testes Python aprovados**, incluindo sete casos dedicados ao perfil inicial:

- leitura da configuração versionada e alvo limitado a 2,5R/faixa;
- scoring inicial sem adquirir os filtros posteriores de 15m;
- Jev vetando 54%, aceitando 55% e não sendo chamado abaixo do score;
- contrato exato do pedido compacto e versão do prompt, com HTTP simulado;
- operação completa em paper, sem abrir a baseline shadow, e veto econômico a alvo pequeno;
- saída temporal e conservação de vencedoras;
- restauração do runtime sem sobrescrever o corte versionado.

Os testes existentes do perfil classificado usam configuração explícita nos fixtures, para continuar verificando aquela política sem depender do perfil ativo no YAML. Não foram enviados pedidos ao Jev real nem ordens reais nesses testes. Isso valida comportamento, não rentabilidade.

Evidência local ignorada pelo Git: `exports/initial-live-review-2026-10-12/evidence.json`, com logs dos dez fechamentos, deploys e limites da reconstrução. O banco efetivo de produção continua sendo Neon, sem acesso autenticado nesta auditoria; o banco Render fornecido anteriormente tem zero tabelas públicas.

Após a validação local, a publicação deve enviar a restauração à `main`, branch acompanhada pelo Render. O deploy e a política efetiva devem ser confirmados pelos logs de `jev_policy`, pelo commit publicado e pelos endpoints de saúde; um push sozinho não comprova que a configuração já está carregada.
