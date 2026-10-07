FROM python:3.12-slim

WORKDIR /app

RUN useradd --create-home --uid 10001 engine

COPY apps/engine /app
COPY infra/migrations /infra/migrations
COPY packages/configs /packages/configs

RUN pip install --no-cache-dir .

ENV CONFIG_DIR=/packages/configs
ENV HOST=0.0.0.0
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

USER engine

EXPOSE 8000

CMD ["python", "main.py"]
