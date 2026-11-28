# Isolation test runner Lambda image (linux/amd64). Built by Pulumi
# (infra/assay_infra/isolation.py) with the repository root as the build context, and only
# when stack config `isolationTests` is true. It runs tests/isolation with pytest inside
# the VPC, where the TSS and the database can be reached.

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
# Locked, hash-checked, wheels only: nothing is compiled and nothing comes from the host.
RUN uv export --locked --package assay-tss --extra isolation-tests --no-dev \
        --no-emit-workspace --no-header --format requirements-txt -o /tmp/requirements.txt \
    && uv pip install --python /var/lang/bin/python3 --target /deps \
        --require-hashes --only-binary :all: -r /tmp/requirements.txt
COPY db/assay_db /code/assay_db
COPY tss/assay_tss /code/assay_tss
RUN rm -rf /code/assay_tss/provision
COPY tests/__init__.py /code/tests/__init__.py
COPY tests/isolation /code/tests/isolation

# RDS CA bundle for TLS verify-full, checked against the checksum pinned in db/Dockerfile.
FROM scratch AS rds-ca
ADD --checksum=sha256:90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b \
    https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /global-bundle.pem

FROM public.ecr.aws/lambda/python:3.14@sha256:497df5d229e6d5c54279a6a48e7f597ff386bd02a9b3b37e788bb881108fc081
RUN install -d -m 0755 /opt/rds-ca
COPY --from=rds-ca --chmod=0444 /global-bundle.pem /opt/rds-ca/global-bundle.pem
# Two layers, in this order (owner, 2026-11-25). The dependencies change only with uv.lock;
# the code changes with every edit. Copied as one layer, any edit pushed every dependency to
# ECR again (26 to 53 MB an image). The files and their modes are the same as before:
# tests/unit/test_image_layers.py holds the installed packages to what they were.
COPY --from=build /deps ${LAMBDA_TASK_ROOT}
COPY --from=build /code ${LAMBDA_TASK_ROOT}
ENV DB_CA_BUNDLE=/opt/rds-ca/global-bundle.pem \
    PYTHONDONTWRITEBYTECODE=1
CMD ["tests.isolation.runner.handler"]
