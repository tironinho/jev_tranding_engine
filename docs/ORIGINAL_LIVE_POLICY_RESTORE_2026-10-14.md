# Restauração da política histórica a15bc12

Em 14/10/2026, o usuário reiterou o pedido de voltar à política do início do live. A referência adotada é `a15bc12`, já escolhida quando ele esclareceu que queria a configuração ao completar os dez primeiros fechamentos, conforme [FIRST_TEN_CONFIG_CORRECTION_2026-10-12.md](FIRST_TEN_CONFIG_CORRECTION_2026-10-12.md).

A restauração anterior deixou de representar essa política depois que foram ativados extensão do alvo por custos, planos de 60/240 minutos, bloqueio por amplitude antes do modelo, gate de expectativa e pesquisa abaixo do corte. O deploy `e7a7dd6` corrigiu execução e dados, mas manteve esses desvios da política histórica. Este ajuste restaura a decisão; conserva as correções de execução.

## O que volta à referência

| Etapa | Política ativa após esta alteração |
| --- | --- |
| Baseline | `baseline_first_ten_v1`, fórmulas e classificação históricas, corte absoluto 0,45 |
| Pedido JEV | Um pedido por sinal válido; três perguntas históricas e estado compacto |
| Identificação do pedido | `jev_live_first_ten_2026_10_09_a15bc12_v1` |
| Estado principal | Símbolo, mercado, lado, confiança, scores, classe, labels, fluxo executado de 1m, retorno/amplitude de 60m e flags de rompimento |
| Sinal abaixo do corte | Encerra como `BASELINE_NO_TRADE`, sem pesquisa shadow adicional |
| Plano antes do JEV | Sem plano explícito, extensão por custos ou filtro de amplitude de 60/240m |
| Continuação sem rompimento | LONG 0,55; SHORT 0,40; sujeitos ao piso histórico de taxa/stop |
| Rompimento classificado | Abaixo do piso, reduz tamanho por `p/piso`; reversão/falso rompimento continuam vetando |
| Alvo de execução | Amplitude absoluta de 60m, sem limitar a 2,5R e sem alongar para cobrir custos |
| Múltiplo 2,5 | Continua na fórmula histórica do piso de continuação |
| Economia de execução | R:R líquido mínimo 1,5, calculado após a avaliação do JEV |
| Tempo de novas posições | Após 60m fecha perdedora/empatada após custos; vencedora segue para alvo/stop |
| Stop móvel | Desativado, como no perfil histórico |
| Comparação paper adicional | Desativada na configuração ativa; implementação preservada para uso explícito |

O comportamento de volume da v1 também é restaurado literalmente: atividade abaixo da média pode produzir voto contrário ao fluxo. A v2, que neutraliza esse voto, continua disponível, mas não integra a política solicitada. Não foram introduzidas novas otimizações.

As três perguntas centrais são iguais às de `a15bc12`. A normalização atual continua retirando inteligência externa ausente ou sem evidência; instruções de on-chain são acrescentadas somente quando há dados utilizáveis. O perfil histórico não envia o resumo Binance/microestrutura nem o bloco posterior `market_context`. As respostas do provedor são copiadas sem transformação.

O carregador de risco agora lê explicitamente `jev_expectancy_gate` do YAML; antes esse campo só assumia o default da dataclass. No perfil restaurado o gate está desativado e nenhum plano de expectativa é produzido. Nos testes dos perfis posteriores ele permanece explicitamente ativado.

## O que fica preservado

- Stop calculado pela quantidade líquida do fill, sem depender de uma leitura de saldo imediatamente após a compra; repetição restrita à rejeição definitiva por saldo.
- Saída de emergência e bloqueio por exposição sem proteção confirmada.
- Taxas efetivas da conta/gateway, com fallback de 0,1% por lado; não são restauradas estimativas antigas abaixo das comissões observadas.
- Reconciliação Binance, persistência, idempotência, lotes, frescor do book e limites de risco.
- Circuito de três perdas live no dia UTC, sem apagar trades, perdas ou saldos.
- Posições anteriores mantêm os horizontes/prazos explícitos gravados, inclusive 240 minutos; a nova configuração não reescreve esses contratos.
- Correções dos labels temporais e da cobertura das simulações paper.

Configurações antigas salvas no runtime não podem reativar os flags posteriores de decisão. `/engine/status` passa a expor `decision_policy` com as versões e os flags efetivamente carregados; o log `jev_policy` inclui os mesmos parâmetros relevantes. Isso permite verificar a restauração depois do deploy.

## Evidência e limites

Foram reprocessadas offline as **95 leituras** salvas do build `e7a7dd6`, entre **02:03:09 e 02:21:04 de 14/10/2026, Brasília**.

| Verificação | Resultado |
| --- | --- |
| Baseline moderna observada | 33 SHORTs válidos; 32 barrados antes do modelo pela amplitude |
| Chamadas reais observadas no recorte | 3, incluindo pesquisa shadow |
| Baseline histórica restaurada | 32 SHORTs válidos, 63 sem sinal |
| Encaminhamento restaurado | **32 de 32 sinais válidos geram exatamente um pedido JEV** |
| Scoring/classificação versus execução direta do código Git a15bc12 | **95 de 95 resultados iguais**, incluindo scores, ação, confiança, razões e classe |
| Três perguntas centrais versus literais do Git a15bc12 | Iguais |

O replay usou resposta sintética fixa para verificar encaminhamento. Não fez chamadas reais ao modelo nem ordens e não constitui backtest de rentabilidade. O contexto externo não acompanha essas linhas de auditoria; o replay não inclui um gate externo. O gate histórico de qualidade e os controles normais de dados continuam possíveis em execução real.

**Mais avaliações não implicam mais entradas:** nesse recorte, 31 dos 32 sinais históricos ainda têm R:R líquido abaixo de 1,5 com taxa de 0,1% por lado, mesmo desconsiderando slippage e juros. A restauração faz o sinal chegar ao modelo; a regra econômica histórica continua podendo recusar sua execução. O circuito das três perdas também permanece aplicado. Na última leitura de produção ele estava aberto no dia UTC de 14/10, cuja virada ocorre às 21h de Brasília.

Os trades vencedores não pertencem todos à mesma versão. Os detalhes recuperados mostram BNB e BTC com `jev_veto_only_v1`, XRP com `jev_break_size_v1` e ETH com a variante posterior de alvo estendido por custos (`48dd55f`). A escolha de `a15bc12` segue a referência solicitada para o décimo fechamento; não atribui todos esses resultados a uma configuração única e não promete repeti-los.

## Validação

**387 testes Python passaram.** Os casos novos exercitam os defaults reais do repositório, o contrato HTTP histórico, o encaminhamento apesar de amplitude pequena seguido da recusa econômica, ausência de pesquisa abaixo do corte, proteção contra overrides antigos e preservação do prazo de posições anteriores. Os testes dos perfis posteriores usam fixtures explícitas e continuam cobrindo suas regras.

Os artefatos locais, ignorados pelo Git, estão em `exports/original-policy-restore-2026-10-14/`: `verify_restore.py`, `verification.json`, `pytest.txt` e os detalhes recuperados de trades vencedores. A política só muda na instância de produção após publicação e deploy desta alteração. Não houve mudança de plano/custo de infraestrutura.
