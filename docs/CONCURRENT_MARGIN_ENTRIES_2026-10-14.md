# Posições simultâneas — 14/10/2026

O operador solicitou remover a limitação de abertura em conjunto e confirmou que deseja posições em ativos diferentes e várias no mesmo ativo. A produção atual opera na margem cruzada Binance.

## Diagnóstico

Na consulta das 11:45 de Brasília, o limite numérico era dez posições, e `allow_pyramiding` estava desligado. O motor já permitia posições simultâneas em ativos diferentes: SOL LONG e XRP LONG tiveram horários sobrepostos no histórico recente. Porém, uma posição gerenciada no símbolo recusava uma entrada adicional com `EXISTING_POSITION`.

O XRP confirmado às 11:39 foi recusado por esse motivo. Os exemplos mais recentes de BTC, ETH, SOL e BNB foram recusados por `ORDER_BELOW_MIN_NOTIONAL`, não por quantidade de posições. Retirar o teto e permitir posições adicionais não elimina uma falta de saldo/margem ou o mínimo da corretora.

## Mudança

- `allow_pyramiding: true`: novos sinais válidos podem acrescentar posições na mesma direção no mesmo ativo, cada uma identificada pela decisão e posição, com fill e stop próprios.
- `max_open_positions: 0`: desativa o teto por quantidade. Um valor positivo continua funcionando como teto se for configurado posteriormente. O caso de uma vaga extra não muda o significado de zero e não aparece como vaga especial quando não há teto.
- O valor padrão e o exemplo de ambiente passam a zero. A API aceita zero, rejeita números negativos e informa `allow_pyramiding` na consulta dos limites.
- O painel explica o significado de zero e mostra que novas entradas adicionais continuam sujeitas a controles financeiros.
- A ativação em produção grava `max_open_positions=0` pela API existente depois que o novo código estiver ativo. Esse valor tem checkpoint e auditoria e não depende de apagar overrides antigos de risco. O carregamento preserva zero corretamente.

As travas de execução por conta continuam coordenando reserva de saldo, gravação de intents e proteção. Posições podem permanecer abertas simultaneamente; isso não exige enviar POSTs concorrentes com o mesmo saldo. Reprocessar uma ordem com resultado desconhecido continua bloqueado até conciliação.

## Propriedade das posições e saídas

O contexto de risco passa a identificar os sentidos das posições gerenciadas do símbolo. Na margem atual, entradas adicionais no mesmo sentido são permitidas. Uma entrada oposta usando `AUTO_BORROW_REPAY` compensaria a exposição existente em vez de criar uma posição independente com saldo para seu próprio stop; essa operação continua recusada como `OPPOSITE_POSITION_NETTING`. Isso não implementa hedge independente.

O adaptador legado de futuros usa stop `closePosition=true`, que pode encerrar toda a exposição do símbolo. Ele não recebe autorização para empilhar entradas live como se cada stop fosse independente; a recusa explícita é `FUTURES_CLOSE_ALL_POSITION`. A produção de margem, que usa stops com quantidade própria, recebe a mudança solicitada.

A saída SHORT com valor abaixo do mínimo não pode aumentar sua compra e pagar principal de outra posição do mesmo ativo. O caminho de ajuste mínimo bloqueia essa compra quando há outra posição SHORT gerenciada. No caso sem outra posição, a quantidade requerida utiliza a quantidade pertencente à saída, em vez da dívida total da conta. O relatório de excedente também utiliza essa quantidade. Nenhuma ordem ou fechamento financeiro é inventado para limpar um resíduo.

Saldo, margem mínima, exposição por ativo e total, risco agregado dos stops, risco por operação, perda diária/drawdown, frescor, conciliação, proteção obrigatória e circuito de dez perdas consecutivas continuam ativos. Não foram ampliados alavancagem, probabilidades ou alvos nesta mudança.

## Validação

Os casos novos verificam teto zero com cem posições no contexto, compatibilidade com teto positivo, escopo das travas de netting, proteção contra fechamento geral em futuros e impedimento de pagar principal de outra posição. Os testes de integração simulam duas entradas LONG e duas SHORT de margem no mesmo ativo: IDs e stops distintos, restauração das duas posições e fechamento da primeira preservando integralmente a segunda e seu stop. Nenhuma ordem real foi criada para testar o patch.

O build de produção do frontend passou com tipos e lint. A suíte completa do motor e a conferência de produção são registradas nos artefatos de execução em `exports/concurrent-entries-2026-10-14/`.

Referências primárias: [efeitos de empréstimo e pagamento nas ordens de margem](https://www.binance.com/en/support/faq/detail/f9fc51cda1984bf08b95e0d96c4570bc) e [semântica de closePosition na Binance](https://developers.binance.com/en/docs/catalog/advanced-trading-derivatives-trading-portfolio-margin/api/rest-api/trade).
