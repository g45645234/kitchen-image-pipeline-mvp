FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on

WORKDIR /src

ARG INSTALL_DEV=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN if [ "$INSTALL_DEV" = "true" ]; then pip install ".[dev]"; else pip install .; fi

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
