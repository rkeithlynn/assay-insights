# Migration Lambda image (linux/amd64). Built by Pulumi (infra/assay_infra/migrations.py)
# with the repository root as the build context. Base images are pinned by digest.

FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

FROM public.ecr.aws/lambda/python:3.14@sha256:497df5d229e6d5c54279a6a48e7f597ff386bd02a9b3b37e788bb881108fc081 AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY infra/pyproject.toml infra/pyproject.toml
COPY db/pyproject.toml db/pyproject.toml
# Locked, hash-checked, wheels only: nothing is compiled and nothing comes from the host.
RUN uv export --locked --package assay-db --no-dev --no-emit-workspace --no-header \
        --format requirements-txt -o /tmp/requirements.txt \
    && uv pip install --python /var/lang/bin/python3 --target /out \
        --require-hashes --only-binary :all: -r /tmp/requirements.txt
COPY db/assay_db /out/assay_db

# RDS CA bundle for TLS verify-full, fetched from AWS and checked against a pinned checksum.
# If AWS publishes a new bundle the checksum fails the build; update it deliberately.
# ADD from a URL always creates the file with mode 0600 (root only), which Lambda's
# non-root user cannot read, so it is fetched here and copied below with an explicit mode.
FROM scratch AS rds-ca
ADD --checksum=sha256:90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b \
    https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /global-bundle.pem

FROM public.ecr.aws/lambda/python:3.14@sha256:497df5d229e6d5c54279a6a48e7f597ff386bd02a9b3b37e788bb881108fc081
# World-readable, writable by no one. tests/unit/test_migrate_image.py checks this as
# Lambda's non-root user. The directory is created first: COPY --chmod would otherwise
# apply 0444 to the directory it creates, leaving it untraversable.
RUN install -d -m 0755 /opt/rds-ca
COPY --from=rds-ca --chmod=0444 /global-bundle.pem /opt/rds-ca/global-bundle.pem
COPY --from=build /out ${LAMBDA_TASK_ROOT}
ENV DB_CA_BUNDLE=/opt/rds-ca/global-bundle.pem
CMD ["assay_db.handler.handler"]
