"""What the provisioning role cannot do with a tenant's data, tried for real.

Part of the isolation tests (`tests/isolation`), but it has to run as the provisioning role,
so it is a command of the provisioning task: `PROVISION_COMMAND=probe`, started by
`scripts/run_isolation.py` for the two test tenants.

The role may use a tenant key through S3 Vectors, which creating a vector index requires
(an accepted exception; BUILD_SPEC.md section 6). This shows it still reaches no tenant
data: every attempt to read or write an object or a vector, or to use the key directly,
must be refused. A few settings calls that the role is allowed must succeed, so a run in
which everything fails (no network, wrong names) cannot pass.

Nothing is written: each write attempted here must be refused.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from assay_tss import keys, naming
from assay_tss.provision.settings import ProvisionSettings

if TYPE_CHECKING:
    from mypy_boto3_kms import KMSClient
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3vectors import S3VectorsClient
    from mypy_boto3_s3vectors.type_defs import VectorDataTypeDef

log = logging.getLogger(__name__)

ALLOWED = "allowed"
DENIED = "denied"
DENIED_CODES = frozenset({"AccessDenied", "AccessDeniedException", "403", "Forbidden"})
# Fail fast: an unreachable endpoint must not look like a refusal.
CLIENT_CONFIG = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2})


@dataclass(frozen=True)
class Check:
    tenant_id: str
    name: str
    expected: str
    outcome: str

    @property
    def ok(self) -> bool:
        return self.outcome == self.expected


def attempt(call: Callable[[], object]) -> str:
    """`allowed`, `denied`, or `error:<code>` for anything that is neither."""
    try:
        call()
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        return DENIED if code in DENIED_CODES else f"error:{code}"
    except BotoCoreError as exc:
        return f"error:{type(exc).__name__}"
    return ALLOWED


def parse_tenants(value: str) -> list[uuid.UUID]:
    tenants = [uuid.UUID(part.strip()) for part in value.split(",") if part.strip()]
    if not tenants:
        raise ValueError("PROBE_TENANTS must list at least one tenant ID")
    return tenants


class Probe:
    def __init__(
        self,
        settings: ProvisionSettings,
        *,
        s3: S3Client | None = None,
        vectors: S3VectorsClient | None = None,
        kms: KMSClient | None = None,
    ) -> None:
        self._settings = settings
        region = settings.region
        self._s3: S3Client = s3 or boto3.client("s3", region_name=region, config=CLIENT_CONFIG)
        self._vectors: S3VectorsClient = vectors or boto3.client(
            "s3vectors", region_name=region, config=CLIENT_CONFIG
        )
        self._kms: KMSClient = kms or boto3.client("kms", region_name=region, config=CLIENT_CONFIG)

    def checks(self, tenant_id: uuid.UUID) -> list[tuple[str, str, Callable[[], object]]]:
        """(what, expected outcome, the call) for one tenant."""
        s = self._settings
        bucket = naming.bucket_name(s.name_prefix, tenant_id)
        vector_bucket = naming.vector_bucket_name(s.name_prefix, tenant_id)
        index = naming.VECTOR_INDEX
        key = naming.key_alias(s.name_prefix, tenant_id)
        s3, vectors, kms = self._s3, self._vectors, self._kms
        zeros: VectorDataTypeDef = {"float32": [0.0] * s.vector_dimensions}
        return [
            # Settings the role manages. These must work, or the refusals below prove nothing.
            (
                "read the bucket's settings",
                ALLOWED,
                lambda: s3.get_bucket_versioning(Bucket=bucket),
            ),
            ("describe the key", ALLOWED, lambda: kms.describe_key(KeyId=key)),
            (
                "describe the vector index",
                ALLOWED,
                lambda: vectors.get_index(vectorBucketName=vector_bucket, indexName=index),
            ),
            # Objects.
            (
                "read an object",
                DENIED,
                lambda: s3.get_object(Bucket=bucket, Key=keys.original_key(uuid.uuid4())),
            ),
            (
                "read an object's metadata",
                DENIED,
                lambda: s3.head_object(Bucket=bucket, Key=keys.text_key(uuid.uuid4())),
            ),
            (
                "list object versions",
                DENIED,
                lambda: s3.list_object_versions(Bucket=bucket, MaxKeys=1),
            ),
            (
                "write an object",
                DENIED,
                lambda: s3.put_object(
                    Bucket=bucket,
                    Key=keys.inbox_key(uuid.uuid4(), uuid.uuid4()),
                    Body=b"isolation probe",
                ),
            ),
            # Vectors.
            (
                "list vectors",
                DENIED,
                lambda: vectors.list_vectors(
                    vectorBucketName=vector_bucket, indexName=index, maxResults=1, returnData=True
                ),
            ),
            (
                "read vectors",
                DENIED,
                lambda: vectors.get_vectors(
                    vectorBucketName=vector_bucket, indexName=index, keys=["isolation-probe"]
                ),
            ),
            (
                "query vectors",
                DENIED,
                lambda: vectors.query_vectors(
                    vectorBucketName=vector_bucket, indexName=index, topK=1, queryVector=zeros
                ),
            ),
            (
                "write vectors",
                DENIED,
                lambda: vectors.put_vectors(
                    vectorBucketName=vector_bucket,
                    indexName=index,
                    vectors=[{"key": "isolation-probe", "data": zeros}],
                ),
            ),
            # The key, used directly rather than by S3 Vectors on the role's behalf.
            (
                "generate a data key directly",
                DENIED,
                lambda: kms.generate_data_key(KeyId=key, KeySpec="AES_256"),
            ),
            (
                "encrypt directly",
                DENIED,
                lambda: kms.encrypt(KeyId=key, Plaintext=b"isolation probe"),
            ),
        ]

    def run(self, tenants: list[uuid.UUID]) -> list[Check]:
        results = []
        for tenant_id in tenants:
            for name, expected, call in self.checks(tenant_id):
                check = Check(str(tenant_id), name, expected, attempt(call))
                results.append(check)
                log.info(
                    "probe",
                    extra={
                        "check": check.name,
                        "tenant_id": check.tenant_id,
                        "expected": check.expected,
                        "outcome": check.outcome,
                        "ok": check.ok,
                    },
                )
        return results
