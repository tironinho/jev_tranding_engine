# Migrações

As revisões Alembic ficam neste diretório. Os modelos SQLAlchemy em `apps/engine/app/db/models.py` são a definição do schema. A revisão inicial cria as tabelas a partir desses modelos.

Na raiz do engine, com `DATABASE_URL` apontando para o Postgres:

```bash
cd apps/engine
alembic upgrade head
```

Trocar de banco de produção é trocar `DATABASE_URL` e rodar o mesmo comando.
