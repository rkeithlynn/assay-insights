# Tenant provisioning task image for ECS Fargate (linux/amd64). Built by Pulumi
# (infra/assay_infra/provisioning.py) with the repository root as the build context.
#
# The task has no route to the internet, so the Pulumi CLI and the AWS provider plugin are
# in the image, each checked against a pinned SHA-256. Their versions must equal the
# `pulumi` and `pulumi-aws` versions in uv.lock (tests/unit/test_provision_image.py checks).
# To upgrade: change the version and the checksum here together with uv.lock.

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
# Locked, hash-checked, wheels only: nothing is compiled and nothing comes from the host.
RUN uv export --locked --package assay-tss --extra provision --no-dev --no-emit-workspace \
        --no-header --format requirements-txt -o /tmp/requirements.txt \
    && uv pip install --python /usr/local/bin/python3 --target /out \
        --require-hashes --only-binary :all: -r /tmp/requirements.txt
COPY db/assay_db /out/assay_db
COPY tss/assay_tss /out/assay_tss

# Pulumi CLI. Checksum from the release's pulumi-<version>-checksums.txt.
FROM scratch AS pulumi-cli-archive
ADD --checksum=sha256:1e38f0fb69a14a326cf3acfaf975770e8de5d0771042f26292f0013d237b8393 \
    https://get.pulumi.com/releases/sdk/pulumi-v3.277.0-linux-x64.tar.gz /pulumi.tar.gz

# AWS provider plugin. The release publishes a SHA-1 only; this SHA-256 was computed from a
# download whose SHA-1 matched the published one.
FROM scratch AS pulumi-aws-archive
ADD --checksum=sha256:0ceb32ca5914b4ba5fa980b3ec398d240c8720d9cb63de9639b7d9bd91c84523 \
    https://github.com/pulumi/pulumi-aws/releases/download/v7.55.0/pulumi-resource-aws-v7.55.0-linux-amd64.tar.gz /aws.tar.gz

FROM public.ecr.aws/docker/library/python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS pulumi
COPY --from=pulumi-cli-archive /pulumi.tar.gz /tmp/pulumi.tar.gz
COPY --from=pulumi-aws-archive /aws.tar.gz /tmp/aws.tar.gz
# Only the engine: an inline Automation API program is its own language host.
RUN install -d -m 0755 /opt/pulumi/bin /opt/pulumi/plugins/resource-aws-v7.55.0 \
    && tar -xzf /tmp/pulumi.tar.gz -C /tmp \
    && install -m 0555 /tmp/pulumi/pulumi /opt/pulumi/bin/pulumi \
    && tar -xzf /tmp/aws.tar.gz -C /opt/pulumi/plugins/resource-aws-v7.55.0 \
    && chmod 0555 /opt/pulumi/plugins/resource-aws-v7.55.0/pulumi-resource-aws

# RDS CA bundle for TLS verify-full, checked against the checksum pinned in db/Dockerfile.
FROM scratch AS rds-ca
ADD --checksum=sha256:90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b \
    https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /global-bundle.pem

FROM public.ecr.aws/docker/library/python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
# The task runs as this unprivileged user with a read-only root filesystem. /work is its
# only writable directory: a task volume is mounted there, and keeps this ownership.
RUN groupadd --system --gid 10001 assay \
    && useradd --system --uid 10001 --gid assay --no-create-home --shell /usr/sbin/nologin assay \
    && install -d -m 0755 /opt/rds-ca /app \
    && install -d -m 0700 -o 10001 -g 10001 /work
COPY --from=rds-ca --chmod=0444 /global-bundle.pem /opt/rds-ca/global-bundle.pem
COPY --from=pulumi /opt/pulumi /opt/pulumi
COPY --from=build --chmod=a+rX,go-w /out /app
ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/pulumi/bin:/usr/local/bin:/usr/bin:/bin \
    DB_CA_BUNDLE=/opt/rds-ca/global-bundle.pem \
    WORK_DIR=/work \
    HOME=/work \
    TMPDIR=/work \
    PULUMI_HOME=/work/pulumi-home \
    PULUMI_PLUGINS_DIR=/opt/pulumi/plugins \
    PULUMI_SKIP_UPDATE_CHECK=true \
    PULUMI_DISABLE_AUTOMATIC_PLUGIN_ACQUISITION=true \
    AWS_STS_REGIONAL_ENDPOINTS=regional
VOLUME ["/work"]
USER 10001:10001
CMD ["python", "-m", "assay_tss.provision"]
