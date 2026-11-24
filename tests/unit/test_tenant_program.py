"""The per-tenant Pulumi program (stack `assay-tenant/<tenant_id>`), using Pulumi mocks.

BUILD_SPEC.md section 6, rule 1: what every tenant's bucket, key, vector bucket, index, face
collection and malware protection plan are created with. The image test
(`test_tenancy_images.py`) previews the same program against the real provider's schema;
this one reads the values.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pulumi
import pytest

from assay_tss import naming, policies
from assay_tss.policies import Principals
from assay_tss.provision import program
from assay_tss.provision.settings import ProvisionSettings
from tests.unit.conftest import REGISTERED

REGION = "us-east-1"
ACCOUNT = "000000000000"
PREFIX = "assay-dev"
TENANT = uuid.UUID("7f3a91c2-0000-4000-8000-000000000001")
ROLE = f"arn:aws:iam::{ACCOUNT}:role"
PRINCIPALS = Principals(
    tenant_role_arn=f"{ROLE}/assay-dev-tenant",
    break_glass_role_arn=f"{ROLE}/assay-dev-break-glass",
    provisioning_role_arn=f"{ROLE}/provisioning",
    scan_role_arn=f"{ROLE}/scan",
)
SETTINGS = ProvisionSettings(
    region=REGION,
    account_id=ACCOUNT,
    name_prefix=PREFIX,
    principals=PRINCIPALS,
    state_bucket="state",
    state_key_arn=f"arn:aws:kms:{REGION}:{ACCOUNT}:key/state",
    invite_function="invite",
    vector_dimensions=1024,
    default_tags={"app": "assay-insights", "env": "dev"},
    work_dir=Path("unused"),
    upload_origins=("https://portal.example.test",),
)
BUCKET = naming.bucket_name(PREFIX, TENANT)
VECTOR_BUCKET = naming.vector_bucket_name(PREFIX, TENANT)
KEY_ARN = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/key-key-id"  # as the mocks name it
TAGS = {"tenant_id": str(TENANT)}


@pulumi.runtime.test
def _run_program() -> None:
    program.tenant_program(SETTINGS, TENANT)()


@pytest.fixture(scope="module")
def resources() -> dict[str, tuple[str, dict[str, Any]]]:
    """Every resource the program registers: logical name -> (type, inputs)."""
    start = len(REGISTERED)
    _run_program()
    created = REGISTERED[start:]
    names = [name for _, name, _ in created]
    assert len(names) == len(set(names))
    return {name: (type_token, inputs) for type_token, name, inputs in created}


def inputs(resources: dict[str, tuple[str, dict[str, Any]]], name: str) -> dict[str, Any]:
    return resources[name][1]


def test_program_creates_exactly_the_tenants_resources(
    resources: dict[str, tuple[str, dict[str, Any]]],
) -> None:
    assert {name: type_token for name, (type_token, _) in resources.items()} == {
        "aws": "pulumi:providers:aws",
        "key": "aws:kms/key:Key",
        "key-alias": "aws:kms/alias:Alias",
        "bucket": "aws:s3/bucket:Bucket",
        "bucket-ownership": "aws:s3/bucketOwnershipControls:BucketOwnershipControls",
        "bucket-public-access": "aws:s3/bucketPublicAccessBlock:BucketPublicAccessBlock",
        "bucket-versioning": "aws:s3/bucketVersioning:BucketVersioning",
        "bucket-encryption": (
            "aws:s3/bucketServerSideEncryptionConfiguration:BucketServerSideEncryptionConfiguration"
        ),
        "bucket-object-lock": "aws:s3/bucketObjectLockConfiguration:BucketObjectLockConfiguration",
        "bucket-policy": "aws:s3/bucketPolicy:BucketPolicy",
        "bucket-cors": "aws:s3/bucketCorsConfiguration:BucketCorsConfiguration",
        "vector-bucket": "aws:s3/vectorsVectorBucket:VectorsVectorBucket",
        "vector-bucket-policy": "aws:s3/vectorsVectorBucketPolicy:VectorsVectorBucketPolicy",
        "vector-index": "aws:s3/vectorsIndex:VectorsIndex",
        "face-collection": "aws:rekognition/collection:Collection",
        "malware-protection": "aws:guardduty/malwareProtectionPlan:MalwareProtectionPlan",
    }


def test_provider_acts_only_in_this_account(
    resources: dict[str, tuple[str, dict[str, Any]]],
) -> None:
    provider = inputs(resources, "aws")
    assert provider["region"] == REGION
    assert json.loads(provider["allowedAccountIds"]) == [ACCOUNT]
    assert json.loads(provider["defaultTags"]) == {"tags": {"app": "assay-insights", "env": "dev"}}


def test_bucket_has_every_setting_the_spec_requires(
    resources: dict[str, tuple[str, dict[str, Any]]],
) -> None:
    bucket = inputs(resources, "bucket")
    # Named after the tenant ID, never the customer.
    assert bucket["bucket"] == BUCKET == "assay-dev-t-7f3a91c2"
    assert bucket["objectLockEnabled"] is True
    assert bucket["forceDestroy"] is False
    assert bucket["tags"] == TAGS

    block = inputs(resources, "bucket-public-access")
    assert all(
        block[k]
        for k in (
            "blockPublicAcls",
            "blockPublicPolicy",
            "ignorePublicAcls",
            "restrictPublicBuckets",
        )
    )
    ownership = inputs(resources, "bucket-ownership")
    assert ownership["rule"] == {"objectOwnership": "BucketOwnerEnforced"}
    versioning = inputs(resources, "bucket-versioning")
    assert versioning["versioningConfiguration"] == {"status": "Enabled"}
    lock = inputs(resources, "bucket-object-lock")
    assert lock["rule"] == {"defaultRetention": {"mode": "GOVERNANCE", "days": 1}}


def test_bucket_encryption_is_the_tenant_key_written_as_s3_stores_it(
    resources: dict[str, tuple[str, dict[str, Any]]],
) -> None:
    [rule] = inputs(resources, "bucket-encryption")["rules"]
    assert rule == {
        "applyServerSideEncryptionByDefault": {
            "sseAlgorithm": "aws:kms",
            "kmsMasterKeyId": KEY_ARN,
        },
        "bucketKeyEnabled": True,
        # S3 adds this to every new bucket; left out, a later run would try to change it.
        "blockedEncryptionTypes": ["SSE-C"],
    }


def test_bucket_key_and_vector_bucket_policies_are_the_isolation_policies(
    resources: dict[str, tuple[str, dict[str, Any]]],
) -> None:
    tenant = str(TENANT)
    assert json.loads(
        inputs(resources, "bucket-policy")["policy"]
    ) == policies.tenant_bucket_policy(bucket=BUCKET, tenant_id=tenant, principals=PRINCIPALS)
    assert json.loads(inputs(resources, "key")["policy"]) == policies.tenant_key_policy(
        region=REGION,
        account_id=ACCOUNT,
        tenant_id=tenant,
        vector_bucket=VECTOR_BUCKET,
        principals=PRINCIPALS,
    )
    attached = inputs(resources, "vector-bucket-policy")
    assert json.loads(attached["policy"]) == policies.tenant_vector_bucket_policy(
        region=REGION,
        account_id=ACCOUNT,
        vector_bucket=VECTOR_BUCKET,
        tenant_id=tenant,
        principals=PRINCIPALS,
    )


def test_key_is_customer_managed_rotated_and_tagged_for_the_tenant(
    resources: dict[str, tuple[str, dict[str, Any]]],
) -> None:
    key = inputs(resources, "key")
    assert key["enableKeyRotation"] is True
    assert key["rotationPeriodInDays"] == 365
    # The tenant role's policy finds the key by this tag.
    assert key["tags"] == TAGS
    assert inputs(resources, "key-alias")["name"] == "alias/assay-dev-t-7f3a91c2"


def test_vector_bucket_and_index(resources: dict[str, tuple[str, dict[str, Any]]]) -> None:
    vector_bucket = inputs(resources, "vector-bucket")
    assert vector_bucket["vectorBucketName"] == VECTOR_BUCKET
    assert vector_bucket["encryptionConfigurations"] == [
        {"sseType": "aws:kms", "kmsKeyArn": KEY_ARN}
    ]
    assert vector_bucket["forceDestroy"] is False
    index = inputs(resources, "vector-index")
    assert index["indexName"] == "documents"
    assert index["vectorBucketName"] == VECTOR_BUCKET
    # Fixed at creation; the pipeline must write vectors that fit.
    assert (index["dimension"], index["distanceMetric"], index["dataType"]) == (
        1024,
        "cosine",
        "float32",
    )
    assert index["metadataConfiguration"] == {"nonFilterableMetadataKeys": ["text"]}


def test_face_collection_and_malware_protection(
    resources: dict[str, tuple[str, dict[str, Any]]],
) -> None:
    assert inputs(resources, "face-collection")["collectionId"] == BUCKET
    plan = inputs(resources, "malware-protection")
    assert plan["role"] == PRINCIPALS.scan_role_arn
    # Uploads only (owner decision 9, build step 6).
    assert plan["protectedResource"] == {
        "s3Bucket": {"bucketName": BUCKET, "objectPrefixes": ["inbox/"]}
    }
    assert plan["actions"] == [{"taggings": [{"status": "ENABLED"}]}]


def test_bucket_cors_admits_only_the_portal_and_the_signed_headers(
    resources: dict[str, tuple[str, dict[str, Any]]],
) -> None:
    """Build step 6: the browser PUTs uploads (and, from step 7, GETs originals) through
    presigned links. Nothing else: no other origin, method or header."""
    cors = inputs(resources, "bucket-cors")
    assert cors["corsRules"] == [
        {
            "allowedMethods": ["PUT", "GET"],
            "allowedOrigins": ["https://portal.example.test"],
            "allowedHeaders": ["content-type", "x-amz-checksum-sha256", "range"],
            "exposeHeaders": ["ETag"],
            "maxAgeSeconds": 600,
        }
    ]


def test_every_taggable_resource_carries_the_tenant_id(
    resources: dict[str, tuple[str, dict[str, Any]]],
) -> None:
    for name in ("key", "bucket", "vector-bucket", "vector-index", "face-collection"):
        assert inputs(resources, name)["tags"] == TAGS, name
    assert inputs(resources, "malware-protection")["tags"] == TAGS
