# Correções da auditoria de 14/10/2026

O deploy anterior executou três compras e encerrou todas em `PROTECTION_FAILED` após uma leitura de saldo que não confirmou quantidade protegível. O prejuízo fechado foi 0,095647 USDT. Os fills também mostraram comissão de 0,10% por lado, contra a estimativa de 0,075%. A janela auditada teve uma interrupção de coleta de 100 minutos.

## Proteção e saída de emergência

- O stop inicial usa imediatamente a quantidade confirmada pelo fill, líquida da comissão em moeda base e arredondada ao lote. A Binance valida a disponibilidade ao receber a ordem. Uma fotografia de conta atrasada não pode impedir localmente esse primeiro envio.
- Uma rejeição explícita por saldo insuficiente (`-3041`, ou `-2010` com mensagem de saldo insuficiente) permite até três leituras frescas de saldo, dentro de um orçamento total de dois segundos para essas leituras. A segunda tentativa de stop só acontece após confirmar o lote inteiro, com o mesmo ID determinístico.
- Timeout, erro de rede, resultado desconhecido e outras rejeições não autorizam repetir o stop. O caminho existente reconcilia o ID antes de qualquer fechamento, preservando proteção contra ordens duplicadas.
- O saldo aceita strings numéricas finitas e não negativas. Erro, campo inválido e quantidade zero são estados distintos. Os logs de confirmação registram moeda, decisão, quantidade, tentativa, status, detalhe, saldo livre e horário observado; não registram tokens nem o extrato completo.
- Um resíduo pequeno não pode fazer uma posição inteira parecer protegida. A confirmação exige a quantidade integral pertencente à posição.
- A resolução do intent de entrada acontece depois da proteção ou do tratamento de emergência. Foi retirada uma gravação redundante anterior à saída. O intent e o checkpoint durável de entrada continuam preservados.
- O orçamento de dois segundos vale para confirmar saldo após uma rejeição; o POST do stop e a saída têm seus próprios timeouts. Não é uma garantia de proteção/fechamento total em dois segundos.

## Custos reais

- Fallback do repositório: maker e taker de 0,001 por lado. No provedor de margem, um fallback menor também recebe esse piso, sem presumir desconto BNB não observado.
- O motor usa o provedor Binance quando há gateway, mesmo sem chave Binance local.
- Novo GET autenticado `/api/binance/fees?symbol=SOLUSDT`, exclusivo do papel `account`, consulta a taxa da conta. O motor transmite apenas o token compartilhado ao gateway; a assinatura Binance permanece no serviço da conta.
- Fontes de auditoria: `config`, `binance` ou `binance_gateway`. Fallback/erro do gateway não é armazenado como observação verificada.
- As taxas verificadas expiram após cinco minutos. Valores inválidos, não finitos, negativos, booleanos ou pertencentes a outra moeda são rejeitados.
- Mantendo os alvos, stops, quantidades e demais custos dos três registros reais, a taxa corrigida reduz seus R:R líquidos para 1,479593; 1,301235; e 1,239638. Todos ficam abaixo do piso 1,5. Isso é uma comparação dos planos gravados, não uma previsão de lucro do novo motor.

## Pedido ao JEV

- On-chain `UNKNOWN`, suporte não finito, evidência abaixo de 0,5 ou qualidade explícita abaixo do piso não são enviados ao modelo. As referências a esses campos também saem de `data_quality`.
- Um on-chain `NEUTRAL` com evidência e qualidade suficientes continua válido. Zero não é confundido com ausência de dado.
- Instruções extras só acompanham blocos que sobreviveram ao filtro. O prompt dos planos de amplitude passa a terminar em `_market_range_v5`.
- No replay local das 23 chamadas capturadas, os 19 blocos on-chain antigos foram removidos por falta de evidência/qualidade. As probabilidades gravadas, usadas como respostas simuladas no replay, permaneceram intactas. Não se chamou o modelo real nem se fabricou aumento de confiança.

## Retornos futuros e métricas

- Tanto o labeler ao vivo quanto a reconstrução no PostgreSQL aceitam no máximo 60 segundos de atraso além de cada horizonte. O fallback durável tem cadência de um minuto.
- Cada retorno novo registra horário observado, tempo efetivo, atraso e status. Atraso excessivo resulta em valor `null` e `OBSERVATION_GAP`, nunca em um retorno numérico de horizonte incorreto.
- A API não conta um conjunto de retornos todos nulos como amostra rotulada. O payload informa `labels_timing_status`; registros antigos sem trilha de horário ficam `LEGACY_UNVERIFIED`.
- Os 75 retornos de 15m incorretos encontrados na auditoria eram reconstruídos sob demanda. O novo algoritmo os invalida ao consultar, sem reescrever o histórico financeiro ou exigir migração SQL.
- `/api/jev/performance` acrescenta `execution_quality`, separando falhas de proteção de planos encerrados por alvo/stop/tempo. Todas as perdas permanecem no PnL e no circuito de risco.
- O laboratório paper acompanha a continuidade das observações. Uma lacuna acima de 90 segundos exclui o fechamento da taxa de acerto verificada, mantendo integralmente o PnL financeiro. O rastreamento sobrevive ao checkpoint; registros antigos sem cobertura documentada não entram na amostra verificada.
- O painel paper mostra `SEM COBERTURA` e `ACERTO OBSERVADO` para tornar essa distinção visível.

## Disponibilidade: mudança de infraestrutura pendente de autorização

Os serviços existentes continuam Free. Código dentro do processo não garante que o Render deixe de suspendê-lo por inatividade. Não foi criado keep-alive para apresentar uma falsa garantia de disponibilidade.

Mudança concreta proposta no workspace já confirmado, `Paulo Regis's workspace`:

| Serviço | Plano atual | Plano proposto | Custo-base proposto |
| --- | --- | --- | --- |
| [Motor](https://dashboard.render.com/web/srv-db36slcs728c73bhs3l0) | Free | 0,5 CPU / 512 MB, Starter | US$ 7/mês |
| [Gateway da conta](https://dashboard.render.com/web/srv-db3q0nrtqb8s73evr27g) | Free | 0,5 CPU / 512 MB, Starter | US$ 7/mês |
| Total de compute | US$ 0 | Duas instâncias sem suspensão por inatividade | US$ 14/mês |

Valores-base verificados em 14/10/2026 na [página oficial de preços](https://render.com/pricing), antes de consumo adicional/impostos. A mudança cria cobrança recorrente e depende de autorização específica. O banco Neon existente não precisa mudar.

Limitação documentada: [Free web services](https://render.com/docs/free#spinning-down-on-idle). Um plano pago remove a suspensão por inatividade, mas não elimina reinicializações ou indisponibilidade de provedores.

## Publicação e conferência

Motor e gateway seguem `main` com auto-deploy por commit. Não disparar deploy manual adicional depois de um push que já acione o auto-deploy.

As versões são compatíveis durante uma atualização: se o gateway antigo ainda não oferecer `/api/binance/fees`, o motor novo usa o fallback conservador; o frontend trata os campos novos como opcionais. Em publicação manual, atualizar o gateway antes do motor. O frontend também precisa receber o componente paper atualizado.

Após publicar:

1. Conferir o commit nos serviços e o prompt `_market_range_v5` nos novos requests efetivos.
2. Consultar o endpoint de taxas autenticado no gateway e conferir a origem registrada nos próximos riscos.
3. Confirmar que retornos que atravessam o hiato ficam nulos, com o horário efetivo auditável.
4. Conferir a separação de `execution_quality` e a cobertura do paper.
5. No próximo trade naturalmente autorizado pelo risco, verificar stop `NEW` para o lote líquido integral e latência registrada. Não criar uma ordem real só para testar o patch.

Os limites de risco, o piso de R:R, as probabilidades exigidas e o circuito de perdas foram preservados. A validação é local e simulada; confirmar proteção real e continuidade exige o deploy e observação em produção.

## Validação local concluída

- Suite completa do motor: **381 testes passaram**. Inclui fill com saldo subsequente zero, comissão em moeda base, saldo em string, rejeição explícita de saldo, resposta ambígua sem reenvio, limite total da confirmação, reconciliação de stop e falhas de leitura/taxas.
- Frontend: `npm run build` passou, incluindo compilação, lint e validação de tipos.
- Replay dos dados capturados: os 75 retornos incorretos de 15m ficaram nulos; os 19 blocos on-chain sem qualidade suficiente saíram das 23 chamadas; os três planos gravados ficaram abaixo do piso de R:R com 0,10% por lado. O replay não chamou produção nem enviou ordens reais.
- `git diff --check` passou com a configuração CRLF do repositório.
