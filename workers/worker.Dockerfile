# The pipeline's worker task image for ECS Fargate (linux/amd64): pipeline steps that need a
# PDF opened (build step 6, second half). Built by Pulumi (infra/assay_infra/pipeline.py)
# with the repository root as the build context. Base images are pinned by digest.
#
# Two users (assay_workers.sandbox):
# - the task's main process runs as root with every capability dropped except SETUID, SETGID
#   and KILL (the task definition), on a read-only root filesystem. It holds the credentials
#   and the database connection and never opens a PDF. It needs those three capabilities to
#   start each sandbox child as another user and to stop it;
# - `sandbox` (uid and gid 10002, assay_workers.sandbox.runner.SANDBOX_UID) is that other
#   user. It owns nothing, belongs to no other group and can write nowhere.
#
# Locked, hash-checked wheels only (pypdfium2 ships a manylinux x86_64 wheel): nothing is
# compiled and nothing comes from the host. Ships the TSS client, never the service or the
# provisioning code.

FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

# 3.14.10-slim-trixie
FROM public.ecr.aws/docker/library/python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY infra/pyproject.toml infra/pyproject.toml
COPY db/pyproject.toml db/pyproject.toml
COPY api/pyproject.toml api/pyproject.toml
COPY schemas/pyproject.toml schemas/pyproject.toml
COPY tss/pyproject.toml tss/pyproject.toml
COPY workers/pyproject.toml workers/pyproject.toml
# The `worker` extra is PDFium: this image has it, the Lambda image does not.
RUN uv export --locked --package assay-workers --extra worker --no-dev --no-emit-workspace \
        --no-header --format requirements-txt -o /tmp/requirements.txt \
    && uv pip install --python /usr/local/bin/python3 --target /out \
        --require-hashes --only-binary :all: -r /tmp/requirements.txt
COPY db/assay_db /out/assay_db
COPY schemas/assay_schemas /out/assay_schemas
COPY workers/assay_workers /out/assay_workers
RUN install -d -m 0755 /out/assay_tss
COPY tss/assay_tss/__init__.py tss/assay_tss/client.py tss/assay_tss/keys.py \
     tss/assay_tss/models.py tss/assay_tss/naming.py /out/assay_tss/

# RDS CA bundle for TLS verify-full, checked against the checksum pinned in db/Dockerfile.
FROM scratch AS rds-ca
ADD --checksum=sha256:90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b \
    https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /global-bundle.pem

FROM public.ecr.aws/docker/library/python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
# The sandbox child's user. /work is the task's only writable directory (a task volume is
# mounted over it); the sandbox user can read what the parent puts there and write nothing.
RUN groupadd --system --gid 10002 sandbox \
    && useradd --system --uid 10002 --gid sandbox --no-create-home --shell /usr/sbin/nologin sandbox \
    && install -d -m 0755 /opt/rds-ca /work
COPY --from=rds-ca --chmod=0444 /global-bundle.pem /opt/rds-ca/global-bundle.pem
# Into the interpreter's own site-packages, not a PYTHONPATH directory: the sandbox child
# runs in isolated mode with an empty environment, which ignores PYTHONPATH.
COPY --from=build --chmod=a+rX,go-w /out /usr/local/lib/python3.14/site-packages
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_CA_BUNDLE=/opt/rds-ca/global-bundle.pem \
    WORK_DIR=/work
# Root, and meant to be: see the header. The task definition drops every capability but three.
USER 0:0
CMD ["python", "-m", "assay_workers.worker"]
