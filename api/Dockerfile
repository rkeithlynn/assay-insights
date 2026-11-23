# API image for ECS Fargate (linux/amd64). Built by Pulumi (infra/assay_infra/api.py) with the
# repository root as the build context. Base images are pinned by digest.

FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

# 3.14.10-slim-trixie
FROM public.ecr.aws/docker/library/python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY infra/pyproject.toml infra/pyproject.toml
COPY db/pyproject.toml db/pyproject.toml
COPY api/pyproject.toml api/pyproject.toml
COPY tss/pyproject.toml tss/pyproject.toml
# Locked, hash-checked, wheels only: nothing is compiled and nothing comes from the host.
RUN uv export --locked --package assay-api --no-dev --no-emit-workspace --no-header \
        --format requirements-txt -o /tmp/requirements.txt \
    && uv pip install --python /usr/local/bin/python3 --target /out \
        --require-hashes --only-binary :all: -r /tmp/requirements.txt
COPY db/assay_db /out/assay_db
COPY api/assay_api /out/assay_api

# RDS CA bundle for TLS verify-full, checked against the checksum pinned in db/Dockerfile.
FROM scratch AS rds-ca
ADD --checksum=sha256:90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b \
    https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /global-bundle.pem

FROM public.ecr.aws/docker/library/python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
# The task runs as this unprivileged user with a read-only root filesystem.
RUN groupadd --system --gid 10001 assay \
    && useradd --system --uid 10001 --gid assay --no-create-home --shell /usr/sbin/nologin assay \
    && install -d -m 0755 /opt/rds-ca /app
COPY --from=rds-ca --chmod=0444 /global-bundle.pem /opt/rds-ca/global-bundle.pem
COPY --from=build --chmod=a+rX,go-w /out /app
ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_CA_BUNDLE=/opt/rds-ca/global-bundle.pem
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "assay_api"]
