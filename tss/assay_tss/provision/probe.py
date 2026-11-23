"""What the provisioning role cannot do with a tenant's data, tried for real.

Part of the isolation tests (`tests/isolation`), but it has to run as the provisioning role,
so it is a command of the provisioning task: `PROVISION_COMMAND=probe`, started by
`scripts/run_isolation.py` for the two test tenants.

The role may use a tenant key through S3 Vectors, which creating a vector index requires
(an accepted exception; BUILD_SPEC.md section 6). This shows it still reaches no tenant
data: every attempt to read or write an object or a vector, or to use the key directly,
must be refused. A few settings calls that the role is allowed must succeed, so a run in
which everything fails (no network, wrong names) cannot pass.

Reading is tried on an object that exists. S3 answers "no such key" (404) for a key that
is not there to any caller that may list the bucket, whatever its right to read objects,
and this role may list (the AWS provider's HeadBucket needs `s3:ListBucket`). So asking
for a missing key proves nothing either way: only a refusal for an object that exists
shows the role cannot read. The probe lists one key and reads that; the isolation tests
(which run first) leave one fixture object in each test tenant's bucket. A 404 still
fails the check, as does a bucket with nothing in it.

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


class NothingToRead(Exception):
    """The bucket holds no object, so a refused read cannot be shown."""


def attempt(call: Callable[[], object]) -> str:
    """`allowed`, `denied`, or `error:<code>` for anything that is neither."""
    try:
        call()
    except NothingToRead:
        return "error:NoObjectToRead"
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
        # One existing key per bucket, found by listing.
        self._existing: dict[str, str | None] = {}

    def _existing_key(self, bucket: str) -> str:
        """The key of an object in the bucket. The role may list keys; it must not read."""
        if bucket not in self._existing:
            found: str | None = None
            try:
                page = self._s3.list_objects_v2(Bucket=bucket, MaxKeys=1)
                contents = page.get("Contents") or []
                found = contents[0]["Key"] if contents else None
            except ClientError, BotoCoreError:
                # A failed listing must not pass for a refused read. "list object keys"
                # reports the listing itself.
                found = None
            self._existing[bucket] = found
        key = self._existing[bucket]
        if key is None:
            raise NothingToRead(bucket)
        return key

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
            # Objects. Listing keys is the one thing the role can do here (accepted: keys
            # hold IDs only); reading is tried on an object that exists.
            (
                "list object keys",
                ALLOWED,
                lambda: s3.list_objects_v2(Bucket=bucket, MaxKeys=1),
            ),
            (
                "read an object that exists",
                DENIED,
                lambda: s3.get_object(Bucket=bucket, Key=self._existing_key(bucket)),
            ),
            (
                "read the metadata of an object that exists",
                DENIED,
                lambda: s3.head_object(Bucket=bucket, Key=self._existing_key(bucket)),
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
