"""One tenant's storage, as a Pulumi program (stack `assay-tenant/<tenant_id>`).

BUILD_SPEC.md section 6, rule 1. Creates, for one business tenant:

- a customer-managed KMS key, tagged with the tenant ID, whose policy names every principal
  that may use it (`assay_tss.policies.tenant_key_policy`);
- the S3 bucket `<prefix>-t-<8 hex>`: Block Public Access, SSE-KMS with the tenant key and
  Bucket Keys, versioning, Object Lock in governance mode with 1-day default retention, and
  a bucket policy that refuses every principal but the four it names;
- an S3 Vectors bucket and its one index, encrypted with the tenant key, with a vector
  bucket policy that admits only the tenant role and the provisioning role;
- a GuardDuty Malware Protection plan for the bucket's `inbox/` prefix only (build step 6,
  owner decision 9): uploads are scanned; the pipeline's own writes elsewhere are not;
- CORS for the tenant portal's origin (build step 6): the browser PUTs uploads through
  presigned links, and step 7's PDF viewer GETs originals the same way;
- a Rekognition face collection named after the bucket.

Rekognition does not document customer-managed keys for face vectors, so the collection is
not described as encrypted with the tenant key (section 8a).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from assay_tss import keys, naming, policies
from assay_tss.provision.settings import ProvisionSettings

# Chunk text is stored with each vector but never filtered on. Filterable metadata is
# limited to 2 KB a vector, so the text must be declared non-filterable, and an index's
# metadata configuration cannot change after creation.
NON_FILTERABLE_METADATA_KEYS = ("text",)
VECTOR_DATA_TYPE = "float32"
VECTOR_DISTANCE_METRIC = "cosine"
RETENTION_MODE = "GOVERNANCE"
# S3 blocks SSE-C on every new bucket and reports it in the encryption rule; the rule is
# written the way S3 stores it. Objects are encrypted with the tenant key only.
BLOCKED_ENCRYPTION_TYPES = ("SSE-C",)
RETENTION_DAYS = 1
# Only uploads are scanned (owner decision 9, build step 6).
SCANNED_PREFIXES = (keys.INBOX_PREFIX,)
# Browser access through presigned links only: the signed headers of an upload, and Range
# for the viewer. ETag lets the browser confirm an upload.
CORS_METHODS = ("PUT", "GET")
CORS_HEADERS = ("content-type", "x-amz-checksum-sha256", "range")
CORS_EXPOSE = ("ETag",)
CORS_MAX_AGE_SECONDS = 600

# Stack outputs, read back by the provisioning task.
OUT_BUCKET = "bucket"
OUT_KEY_ARN = "kms_key_arn"
OUT_VECTOR_BUCKET = "vector_bucket"
OUT_VECTOR_INDEX = "vector_index"
OUT_FACE_COLLECTION = "face_collection"
OUTPUTS = (OUT_BUCKET, OUT_KEY_ARN, OUT_VECTOR_BUCKET, OUT_VECTOR_INDEX, OUT_FACE_COLLECTION)


def tenant_program(settings: ProvisionSettings, tenant_id: uuid.UUID) -> Callable[[], None]:
    """The inline program for one tenant. Pulumi is imported only when it runs."""

    def program() -> None:
        import pulumi
        import pulumi_aws as aws

        s = settings
        tid = str(tenant_id)
        slug = naming.tenant_slug(s.name_prefix, tenant_id)
        bucket_name = naming.bucket_name(s.name_prefix, tenant_id)
        vector_bucket_name = naming.vector_bucket_name(s.name_prefix, tenant_id)
        tags = {policies.TENANT_TAG: tid}

        # The provider refuses to act in any account but this one.
        provider_args: dict[str, Any] = {
            "region": s.region,
            "allowed_account_ids": [s.account_id],
            "default_tags": aws.ProviderDefaultTagsArgs(tags=dict(s.default_tags)),
            **s.provider_args,
        }
        provider = aws.Provider("aws", **provider_args)
        opts = pulumi.ResourceOptions(provider=provider)
        # Tenant deletion is out of scope; nothing here may be removed by accident.
        kept = pulumi.ResourceOptions(provider=provider, protect=True)

        key = aws.kms.Key(
            "key",
            description=f"{slug}: tenant data",
            enable_key_rotation=True,
            rotation_period_in_days=365,
            deletion_window_in_days=30,
            policy=policies.to_json(
                policies.tenant_key_policy(
                    region=s.region,
                    account_id=s.account_id,
                    tenant_id=tid,
                    vector_bucket=vector_bucket_name,
                    principals=s.principals,
                )
            ),
            tags=tags,
            opts=kept,
        )
        aws.kms.Alias(
            "key-alias",
            name=naming.key_alias(s.name_prefix, tenant_id),
            target_key_id=key.key_id,
            opts=opts,
        )

        bucket = aws.s3.Bucket(
            "bucket",
            bucket=bucket_name,
            object_lock_enabled=True,
            force_destroy=False,
            tags=tags,
            opts=kept,
        )
        ownership = aws.s3.BucketOwnershipControls(
            "bucket-ownership",
            bucket=bucket.id,
            rule=aws.s3.BucketOwnershipControlsRuleArgs(object_ownership="BucketOwnerEnforced"),
            opts=opts,
        )
        public_access = aws.s3.BucketPublicAccessBlock(
            "bucket-public-access",
            bucket=bucket.id,
            block_public_acls=True,
            block_public_policy=True,
            ignore_public_acls=True,
            restrict_public_buckets=True,
            opts=opts,
        )
        versioning = aws.s3.BucketVersioning(
            "bucket-versioning",
            bucket=bucket.id,
            versioning_configuration=aws.s3.BucketVersioningVersioningConfigurationArgs(
                status="Enabled"
            ),
            opts=opts,
        )
        encryption = aws.s3.BucketServerSideEncryptionConfiguration(
            "bucket-encryption",
            bucket=bucket.id,
            rules=[
                aws.s3.BucketServerSideEncryptionConfigurationRuleArgs(
                    apply_server_side_encryption_by_default=aws.s3.BucketServerSideEncryptionConfigurationRuleApplyServerSideEncryptionByDefaultArgs(
                        sse_algorithm="aws:kms", kms_master_key_id=key.arn
                    ),
                    bucket_key_enabled=True,
                    blocked_encryption_types=list(BLOCKED_ENCRYPTION_TYPES),
                )
            ],
            # After the other settings, so the bucket is no longer brand new when the
            # provider reads the rule back.
            opts=pulumi.ResourceOptions(
                provider=provider, depends_on=[ownership, public_access, versioning]
            ),
        )
        object_lock = aws.s3.BucketObjectLockConfiguration(
            "bucket-object-lock",
            bucket=bucket.id,
            rule=aws.s3.BucketObjectLockConfigurationRuleArgs(
                default_retention=aws.s3.BucketObjectLockConfigurationRuleDefaultRetentionArgs(
                    mode=RETENTION_MODE, days=RETENTION_DAYS
                )
            ),
            opts=pulumi.ResourceOptions(provider=provider, depends_on=[versioning]),
        )
        bucket_policy = aws.s3.BucketPolicy(
            "bucket-policy",
            bucket=bucket.id,
            policy=policies.to_json(
                policies.tenant_bucket_policy(
                    bucket=bucket_name, tenant_id=tid, principals=s.principals
                )
            ),
            # One bucket setting at a time: S3 rejects concurrent changes to a new bucket.
            opts=pulumi.ResourceOptions(
                provider=provider,
                depends_on=[ownership, public_access, versioning, encryption, object_lock],
            ),
        )

        vector_bucket = aws.s3.VectorsVectorBucket(
            "vector-bucket",
            vector_bucket_name=vector_bucket_name,
            encryption_configurations=[
                aws.s3.VectorsVectorBucketEncryptionConfigurationArgs(
                    sse_type="aws:kms", kms_key_arn=key.arn
                )
            ],
            force_destroy=False,
            tags=tags,
            opts=kept,
        )
        aws.s3.VectorsVectorBucketPolicy(
            "vector-bucket-policy",
            vector_bucket_arn=vector_bucket.vector_bucket_arn,
            policy=policies.to_json(
                policies.tenant_vector_bucket_policy(
                    region=s.region,
                    account_id=s.account_id,
                    vector_bucket=vector_bucket_name,
                    tenant_id=tid,
                    principals=s.principals,
                )
            ),
            opts=opts,
        )
        index = aws.s3.VectorsIndex(
            "vector-index",
            index_name=naming.VECTOR_INDEX,
            vector_bucket_name=vector_bucket.vector_bucket_name,
            data_type=VECTOR_DATA_TYPE,
            dimension=s.vector_dimensions,
            distance_metric=VECTOR_DISTANCE_METRIC,
            metadata_configuration=aws.s3.VectorsIndexMetadataConfigurationArgs(
                non_filterable_metadata_keys=list(NON_FILTERABLE_METADATA_KEYS)
            ),
            tags=tags,
            # S3 Vectors decrypts with the tenant key, on this role's behalf, to create the
            # index, so the key and its policy must be in place first.
            opts=pulumi.ResourceOptions(provider=provider, protect=True, depends_on=[key]),
        )

        collection = aws.rekognition.Collection(
            "face-collection",
            collection_id=naming.face_collection_id(s.name_prefix, tenant_id),
            tags=tags,
            opts=kept,
        )

        # GuardDuty checks the bucket with the scan role when the plan is created, so the
        # bucket's encryption and policy must already allow that role.
        if s.upload_origins:
            aws.s3.BucketCorsConfiguration(
                "bucket-cors",
                bucket=bucket.id,
                cors_rules=[
                    aws.s3.BucketCorsConfigurationCorsRuleArgs(
                        allowed_methods=list(CORS_METHODS),
                        allowed_origins=list(s.upload_origins),
                        allowed_headers=list(CORS_HEADERS),
                        expose_headers=list(CORS_EXPOSE),
                        max_age_seconds=CORS_MAX_AGE_SECONDS,
                    )
                ],
                opts=pulumi.ResourceOptions(provider=provider, depends_on=[bucket_policy]),
            )

        aws.guardduty.MalwareProtectionPlan(
            "malware-protection",
            role=s.principals.scan_role_arn,
            protected_resource=aws.guardduty.MalwareProtectionPlanProtectedResourceArgs(
                s3_bucket=aws.guardduty.MalwareProtectionPlanProtectedResourceS3BucketArgs(
                    bucket_name=bucket.bucket, object_prefixes=list(SCANNED_PREFIXES)
                )
            ),
            actions=[
                aws.guardduty.MalwareProtectionPlanActionArgs(
                    taggings=[
                        aws.guardduty.MalwareProtectionPlanActionTaggingArgs(status="ENABLED")
                    ]
                )
            ],
            tags=tags,
            opts=pulumi.ResourceOptions(provider=provider, depends_on=[bucket_policy, encryption]),
        )

        pulumi.export(OUT_BUCKET, bucket.bucket)
        pulumi.export(OUT_KEY_ARN, key.arn)
        pulumi.export(OUT_VECTOR_BUCKET, vector_bucket.vector_bucket_name)
        pulumi.export(OUT_VECTOR_INDEX, index.index_name)
        pulumi.export(OUT_FACE_COLLECTION, collection.collection_id)

    return program
