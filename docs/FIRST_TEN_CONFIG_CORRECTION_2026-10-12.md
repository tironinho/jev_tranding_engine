# Configuração vigente ao completar os dez trades live

Correção posterior preparada para as recusas por R:R líquido: [ENTRY_COST_TARGET_FIX_2026-10-12.md](ENTRY_COST_TARGET_FIX_2026-10-12.md). Esse ajuste muda a geometria e passa a enviar um plano explícito ao Jev; o restante deste documento descreve a restauração histórica publicada em `9fe02da`.

O usuário esclareceu que deseja a configuração **ao completar os dez trades**, e não a da primeira entrada. A referência correta é `a15bc12`, live desde 09/10/2026 às 01:35:27 de Brasília. O décimo fechamento registrado ocorreu às 10:52:19 do mesmo dia. A amostra de dez fechamentos soma +0,5164 USDT (quatro ganhos e seis perdas); os registros e limites da reconstrução estão no relatório anterior.

## Por que a primeira restauração não abriu trades

`640d76e` restaurou a política anterior `d5b2f6d`, com continuação mínima de 55% para ambos os lados e alvo limitado a 2,5R. Essa referência não correspondia ao estado da engine no décimo fechamento.

Entre 00:04:30 e 00:13:24 de 12/10/2026, Brasília, os logs da instância `l8vxs` registraram 50 avaliações baseline: 41 sem sinal e nove SHORTs. Os nove chegaram ao Jev, receberam probabilidades entre 37% e 48% e foram todos vetados por `JEV_LOW_CONTINUATION`. Nenhuma ação chegou ao executor live; não houve erro de aplicação, veto por saldo ou expiração nesse recorte.

O problema observado foi a continuação exigida pela política escolhida, não falha de deploy. Os vetos não validam nem invalidam lucro de uma entrada alternativa.

## Política restaurada de a15bc12

| Item | Regra |
| --- | --- |
| Scoring/classificação | Funções históricas de `a15bc12`, identificadas como `baseline_first_ten_v1` |
| Baseline | Corte absoluto 0,45; classificação de alinhamento original, sem os ajustes posteriores de 5m/15m/reclaim |
| Sem rompimento | LONG mínimo 55%; SHORT mínimo 40% |
| Piso ajustado por custos | `max(corte do lado, (1 + taxa ida/volta / stop_pct) / 3,5)` |
| Rompimento na classe | Continuação abaixo do piso reduz tamanho por `p / piso`; reversão e falso rompimento continuam vetando |
| Alvo | Amplitude absoluta dos últimos 60 minutos, inclusive quando supera 2,5R |
| 2,5R | Continua na fórmula histórica do piso, sem limitar o alvo |
| Economia de execução | R:R líquido mínimo 1,5 |
| Jev | Uma avaliação por candidato válido, estado compacto com classe e perguntas históricas orientadas ao candidato |
| Contexto externo incompleto | Retirado do pedido abaixo da qualidade mínima, como na versão histórica |
| LONG/SHORT | Ambos disponíveis; limites atuais de execução/proteção preservados |
| Após 60m | Fecha perdedora/empatada após custos; mantém vencedora para alvo/stop, sem stop móvel posterior |

40% não é um passe automático: taxas podem elevar o piso. Exemplo: stop 0,25%, taxa ida/volta 0,15% e múltiplo 2,5 elevam o corte SHORT para aproximadamente 45,71%. Um rompimento classificado pode passar abaixo disso com tamanho reduzido, mas continua sujeito ao filtro econômico e aos demais controles. Se o tamanho for zero, a execução recusa; não transforma zero em uma ordem de tamanho cheio.

As perguntas históricas não informam a geometria de um plano exato nem horizonte explícito. As respostas continuam não calibradas. Alterar a política não garante uma ordem ou rentabilidade.

## Escopo e verificação

Foram preservados os perfis anteriores e as correções atuais de reconciliação, lotes, comissões, proteção, persistência, frescor, reserva do stop, risco agregado e circuito de perdas. A infraestrutura e as fontes atuais de contexto não são revertidas. O perfil dos dez trades não envia o resumo Binance/microestrutura acrescentado posteriormente ao Jev como informação extra.

As funções de scoring/classificação foram copiadas da referência Git e comparadas pela árvore sintática; as três perguntas Jev também foram comparadas literalmente com `a15bc12`. **274 testes Python passaram.** Os testes dedicados verificam corte SHORT ajustado por taxas, tamanho menor no rompimento, veto por reversão/falso rompimento, alvo sem teto de 2,5R, contrato do pedido e operação paper completa. Nenhuma ordem real é enviada pelos testes.

O banco Neon e os overrides históricos do dashboard continuam sem acesso autenticado. A identificação usa deploys, código versionado e fechamentos dos logs, e não afirma que os dez trades tenham sido abertos sob uma única versão. A configuração carregada deve ser confirmada em produção por `jev_policy`, incluindo `request_profile=live_first_ten_2026_10_09`, `short_floor=0.4`, `rr=2.5` e `min_net_rr=1.5`.
