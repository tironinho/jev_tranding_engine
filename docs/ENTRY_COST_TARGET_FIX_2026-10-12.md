# Entradas confirmadas bloqueadas por R:R líquido

## Evidência em produção

A instância `srv-db36slcs728c73bhs3l0-zxfzb`, no commit `9fe02da`, registrou três recusas live de BNBUSDT por `NET_RR_TOO_LOW` às 03:29:14, 03:30:14 e 03:31:13 UTC de 12/10/2026. A consulta filtrada de 03:26:00 até 03:39:11 UTC terminou com `hasMore=false`. O primeiro sinal foi confirmado pelo Jev com continuação de 30%, sob a regra histórica de rompimento com tamanho reduzido.

Os logs anteriores não contêm entrada, stop, alvo ou custos dessas recusas. Sem autenticação no Neon/inspector, não afirmamos um R:R ou uma taxa efetiva específica para essas três decisões. O código mostra o conflito: alvo igual à amplitude de 60 minutos, stop sujeito ao piso de 0,25%/ATR/ruído recente e exigência de pelo menos 1,5R líquido.

## Correção

`cost_aware_target_floor: true` habilita um plano explícito no perfil `live_first_ten_2026_10_09`. A baseline e a classificação continuam na referência dos dez trades; a geometria e a pergunta de continuação agora incluem esta correção, identificada por `net_rr_floor_v1`. Portanto, a política passa a ser uma alteração da referência histórica, não uma restauração literal.

Antes da chamada ao Jev, a engine calcula o alvo base histórico e o alvo mínimo para 1,5R líquido. Mantém o mais distante e preserva o stop. O cálculo inclui comissão na entrada e na saída no preço do alvo, slippage de estresse e juros estimados de margem. Juros desconhecidos usam o cenário configurado, não são apresentados como juros realizados.

Para distância favorável `d`, direção `s` (+1 LONG, -1 SHORT), entrada `E`, stop `S`, taxas `fe`/`fx`, slippage `L` e financiamento recebido `F` por unidade:

```
risco_liquido = |E-S| + E*fe + S*fx + L - F
d_minimo = (1,5*risco_liquido + E*(fe+fx) + L - F) / (1-s*fx)
alvo = E + s*max(distancia_do_alvo_base, d_minimo)
```

Esse alvo maior não torna o movimento mais provável. O Jev recebe distâncias relativas ao preço, ATR e faixa de 60 minutos, horizonte de 60 minutos, R:R e ponto de equilíbrio. A pergunta exige estimar alvo antes do stop e avisa que ultrapassar a faixa observada exige extensão do mercado. A probabilidade continua não calibrada.

A regra de continuação usa o maior entre o piso do lado (40% SHORT, 55% LONG) e a probabilidade de equilíbrio do plano líquido. Isso substitui a aproximação por 2,5R somente quando o novo plano está presente. Rompimento abaixo do piso ainda reduz tamanho conforme a política solicitada dos dez trades. O gate posterior de EV de 0,10R continua desativado; a EV estressada é registrada como diagnóstico. **Esse perfil pode autorizar um rompimento com EV estimada negativa. Reduzir tamanho não torna essa EV positiva nem comprova lucro.**

A execução conserva exatamente o stop e o alvo avaliados. Recalcula custos com a taxa da conta, quantidade e livro; rejeita se o R:R real estimado cair abaixo do mínimo, se houver mudança excessiva na entrada ou se os controles de saldo/proteção/risco recusarem. Não estende o alvo novamente após a confirmação. Planos inviáveis ou sem geometria não viram uma avaliação apenas direcional. Alvos anteriores mais distantes e posições existentes não são reescritos.

Os logs de recusa passam a informar identificadores da decisão/snapshot, preços, quantidade, R:R bruto/líquido, risco/retorno líquidos, taxas, slippage e financiamento. `live_risk_accept` registra aprovação de risco, sem afirmar que a ordem foi enviada ou preenchida. A tabela de planos recebe o plano avaliado, sua probabilidade, EV diagnóstica e resultado do Jev.

## Verificação e publicação

**284 testes Python passaram**, incluindo abertura paper LONG/SHORT em faixa estreita com conta de 20 USDT, comissão dependente do alvo, juros, contrato enviado ao Jev, alvo imutável após avaliação, recusa com taxas/impacto maiores, saldo insuficiente para proteção, circuito de perdas, geometria indisponível e regressões dos perfis históricos. Não foram enviadas ordens reais. Esses testes validam comportamento, não rentabilidade.

A publicação desta correção foi solicitada pelo usuário. Até a confirmação de `cost_target_floor=True` no `jev_policy` da nova instância, considere a configuração anterior do commit `9fe02da` como referência de produção. Depois do deploy, a confirmação depende de novas decisões e dos registros de risco/ordem/fill; um sinal passado não será reenviado. Os registros locais de publicação e logs ficam em `exports/monitoring/`.
