FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY . /app

ENV PYTHONUNBUFFERED=1
RUN uv sync

EXPOSE 8080
CMD ["uv", "run", "server.py"]
