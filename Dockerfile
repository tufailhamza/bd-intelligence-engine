FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

ARG branch

COPY requirements.txt ./


RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

RUN mkdir -p -m 0600 /root/.ssh

RUN --mount=type=secret,id=known_hosts,dst=/root/.ssh/known_hosts \
    --mount=type=secret,id=ssh_key,dst=/root/.ssh/id_rsa \
    ssh-keyscan -H -t rsa github.com \
    && pip install --no-cache-dir git+ssh://git@github.com/dnnaeinc/ai-Qlu2-library@${branch}#egg=qutils

COPY app ./app
COPY main.py .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "uvloop"]
