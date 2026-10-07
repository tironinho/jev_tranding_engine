# Docker

O Dockerfile do engine é `engine.Dockerfile`. O contexto de build é a raiz do repositório.

Postgres apenas:

```bash
docker compose up -d postgres
```

Engine junto (perfil `full`), depois de criar o `.env`:

```bash
docker compose --profile full up --build engine
```

O frontend não entra nesta composição. Ele é um app Next.js pensado para `npm run dev` e para a Vercel.
