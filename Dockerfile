FROM python:3.10-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/root/.opam/default/bin:${PATH}"

WORKDIR /scythe

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        clang \
        clang-14 \
        curl \
        docker.io \
        git \
        jq \
        libclang-rt-14-dev \
        m4 \
        opam \
        openjdk-17-jdk-headless \
        unzip \
        xz-utils \
        zip \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --upgrade pip setuptools wheel

COPY . /scythe

RUN python -m pip install --no-cache-dir solc-select slither-analyzer \
    && python -m pip install --no-cache-dir -e /scythe \
    && bash /scythe/scripts/docker-setup.sh --language all \
    && docker --version

COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /workspace

COPY scripts/docker-entrypoint.sh /usr/local/bin/scythe-entrypoint.sh
RUN chmod +x /usr/local/bin/scythe-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/scythe-entrypoint.sh"]
CMD ["benchmark"]