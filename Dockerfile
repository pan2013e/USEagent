ARG BASE_IMAGE=ubuntu:24.04

# ---- builder ----
FROM ${BASE_IMAGE} AS builder
LABEL stage=builder

ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Singapore

RUN rm -f /etc/apt/apt.conf.d/docker-clean
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata curl git openssh-client python3 python3-venv lsb-release && \
    true

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"
ENV UV_LINK_MODE=copy

RUN mkdir -p /root/.ssh && \
    printf "Host github.com\nHostname ssh.github.com\nPort 443\nUser git\n" > /root/.ssh/config && \
    chmod 600 /root/.ssh/config

ARG USEBENCH_ENABLED=true
ENV USEBENCH_ENABLED=${USEBENCH_ENABLED}
RUN if [ "$USEBENCH_ENABLED" = "true" ]; then \
      ssh-keyscan -p 443 ssh.github.com >> /root/.ssh/known_hosts; \
    fi

# First: copy dependency metadata and install runtime deps before source changes.
WORKDIR /src
COPY pyproject.toml uv.lock* /src/
RUN touch README.md
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
  --mount=type=ssh \
  if [ "$USEBENCH_ENABLED" = "true" ]; then \
    UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --locked --python /usr/bin/python3 --extra usebench --no-install-project --no-dev; \
  else \
    UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --locked --python /usr/bin/python3 --no-install-project --no-dev; \
  fi
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Now: Copy in Project source and build wheel
COPY . /src/
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked uv build
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked uv pip install --python /opt/venv/bin/python --no-deps /src/dist/*.whl

# always create the directory, run migration only if enabled
RUN mkdir -p /artifact/data && \
    if [ "$USEBENCH_ENABLED" = "true" ]; then \
      /opt/venv/bin/usebench-migration /artifact/data; \
    fi

# ---- runtime ----
FROM ${BASE_IMAGE}
LABEL maintainer.Yuntong="Yuntong Zhang <ang.unong@gmail.com>"
LABEL maintainer.Leonhard="Leonhard Applis <leonhard.applis@protonmail.com>"

ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Singapore

RUN rm -f /etc/apt/apt.conf.d/docker-clean
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata curl wget git openssh-client python3 python3-venv python3-dev build-essential lsb-release make tree ripgrep sudo && \
    true

RUN wget -O /etc/apt/sources.list.d/gitlab-ci-local.sources https://gitlab-ci-local-ppa.firecow.dk/gitlab-ci-local.sources
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update -y && apt-get install -y --no-install-recommends gitlab-ci-local

# bring only the ready venv and migrated data
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /artifact/data /app/data

ARG USEBENCH_ENABLED=true
ENV USEBENCH_ENABLED=${USEBENCH_ENABLED}
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# These are throw-away containers; allow system package installs
ENV PIP_BREAK_SYSTEM_PACKAGES=1
ARG COMMIT_SHA=""
RUN [ -n "$COMMIT_SHA" ] && mkdir -p /output && printf "%s\n" "$COMMIT_SHA" > /commit.sha || true

RUN useradd -m -u 0 -o -g 0 app
USER app

RUN git config --global init.defaultBranch main

# Most Tasks will work (not start!) in /tmp/working_dir, we can help some tools by setting this as default cwd
RUN mkdir /tmp/working_dir
WORKDIR /tmp/working_dir
