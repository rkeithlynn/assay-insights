# The ingestion pipeline's Lambda image (linux/amd64): the scan result, starter, step and
# sweeper functions. Built by Pulumi (infra/assay_infra/pipeline.py) with the repository root
# as the build context. Each function picks its handler (image_config). Base images are
# pinned by digest.
#
# Locked, hash-checked wheels only (pypdfium2 ships a manylinux x86_64 wheel): nothing is
# compiled and nothing comes from the host. Ships the TSS client, never the service or the
# provisioning code.

FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

FROM public.ecr.aws/lambda/python:3.14@sha256:497df5d229e6d5c54279a6a48e7f597ff386bd02a9b3b37e788bb881108fc081 AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY infra/pyproject.toml infra/pyproject.toml
COPY db/pyproject.toml db/pyproject.toml
COPY api/pyproject.toml api/pyproject.toml
COPY schemas/pyproject.toml schemas/pyproject.toml
COPY tss/pyproject.toml tss/pyproject.toml
COPY workers/pyproject.toml workers/pyproject.toml
RUN uv export --locked --package assay-workers --no-dev --no-emit-workspace \
        --no-header --format requirements-txt -o /tmp/requirements.txt \
    && uv pip install --python /var/lang/bin/python3 --target /out \
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

FROM public.ecr.aws/lambda/python:3.14@sha256:497df5d229e6d5c54279a6a48e7f597ff386bd02a9b3b37e788bb881108fc081
# World-readable, writable by no one: Lambda runs code as a non-root user. The directory
# is created first, because COPY --chmod would give it the file's mode.
RUN install -d -m 0755 /opt/rds-ca
COPY --from=rds-ca --chmod=0444 /global-bundle.pem /opt/rds-ca/global-bundle.pem
COPY --from=build --chmod=a+rX,go-w /out ${LAMBDA_TASK_ROOT}
ENV DB_CA_BUNDLE=/opt/rds-ca/global-bundle.pem
CMD ["assay_workers.scan.handler"]
