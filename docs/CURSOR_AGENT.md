# Agente de código

A Evolution Engine fala com um agente só pela interface `CodingAgentProvider`:

```text
create_experiment_task
get_task_status
get_result
```

Implementações previstas: mock, Cursor, e no futuro Codex ou Claude Code. O núcleo não importa um fornecedor específico.

## Estado atual

`CODING_AGENT_PROVIDER=mock` é o default. O mock grava a tarefa e devolve `repository_modified=false`. Ele não cria branch, não edita arquivo e não abre pull request.

`CursorCodingAgentProvider` recusa a chamada. O SDK oficial existe (`cursor-sdk` / `@cursor/sdk`, `Agent.prompt`, runtime local ou cloud). Ele não está ligado neste processo: uma chamada daqui editaria o checkout da Trading Engine, e a Evolution Engine exige worktree isolada mais `CURSOR_API_KEY`. Até isso existir, o provider fica bloqueado e o mock é o único agente que grava task.

## Quando a integração existir

A implementação fica apenas em `CursorCodingAgentProvider`. O restante da Evolution Engine não muda.

O que o adaptador precisa receber, sem exceção:

- `experiment_id`
- problema e hipótese
- versão pai e challenger
- paths permitidos e protegidos
- critérios de aceite e testes obrigatórios
- branch `experiment/EXP-xxxxxx`

O que ele não recebe: chave de exchange, permissão de saque, execução live, alteração de kill switch ou de limite de risco.

O researcher continua sem shell e sem SQL. Só o coding agent, no branch isolado, mexe em código permitido.

## Branch e PR

`main` não é editado pelo experimento. O pull request carrega experimento, hipótese, parent, challenger e os relatórios que existirem. Sem relatório, o campo fica vazio. Não se preenche com resultado simulado.

O workflow `.github/workflows/experiment-validation.yml` roda testes e o verificador de paths protegidos quando a branch começa com `experiment/`.
