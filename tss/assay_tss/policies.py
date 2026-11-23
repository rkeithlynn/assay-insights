"""Every IAM, bucket and key policy that keeps tenants apart (BUILD_SPEC.md section 6).

One module, so the pieces can be read and tested together:

- `tenant_role_trust_policy`: only the TSS Lambda's role may assume the tenant role, and
  only with the three session tags and a source identity.
- `tenant_role_policy`: the one template, built from `${aws:PrincipalTag/...}`. A session
  reaches the bucket, vector bucket and face collection named by its tags, and the KMS key
  whose `tenant_id` tag equals its own.
- `session_policy`: the inline policy passed to AssumeRole. It names the exact bucket, key,
  index and collection, and narrows the actions to what the caller asked for.
- `tenant_bucket_policy` and `tenant_key_policy`: the resource side. They deny or withhold
  access from every principal except the four named in `Principals`, each for its own job.
- `tenant_vector_bucket_policy`: the same for the vector bucket, admitting only the tenant
  role and the provisioning role.

Tags are part of the lock: the role policy matches a key by its `tenant_id` tag. So only the
provisioning role may tag or untag a tenant key (or bucket), and the tenant role is denied
every tagging action. Bucket, vector bucket and collection are matched by name, which
cannot change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from assay_tss import keys
from assay_tss.models import StorageAccess, TenantStorageProfile
from assay_tss.naming import VECTOR_BUCKET_SUFFIX, tenant_pattern

Policy = dict[str, Any]

VERSION = "2012-10-17"

# Session tags the TSS passes to AssumeRole, and the tag on every tenant resource.
TENANT_TAG = "tenant_id"
BUCKET_TAG = "bucket"
VECTOR_BUCKET_TAG = "vector_bucket"
SESSION_TAG_KEYS = (TENANT_TAG, BUCKET_TAG, VECTOR_BUCKET_TAG)

# AWS limit on the plain text of an inline session policy.
SESSION_POLICY_MAX_CHARS = 2048

# --- Actions --------------------------------------------------------------------------

OBJECT_READ = ("s3:GetObject", "s3:GetObjectVersion", "s3:GetObjectTagging")
# DeleteObject only adds a delete marker: the role never gets DeleteObjectVersion.
OBJECT_WRITE = (
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:AbortMultipartUpload",
    "s3:ListMultipartUploadParts",
)
BUCKET_LIST = ("s3:ListBucket", "s3:GetBucketLocation")
KEY_USE = ("kms:Decrypt", "kms:GenerateDataKey")
KEY_DESCRIBE = ("kms:DescribeKey",)
# QueryVectors needs GetVectors as well when it filters or returns metadata.
VECTOR_READ = (
    "s3vectors:GetIndex",
    "s3vectors:GetVectors",
    "s3vectors:QueryVectors",
    "s3vectors:ListVectors",
)
VECTOR_WRITE = ("s3vectors:PutVectors", "s3vectors:DeleteVectors")
# The vectors themselves. The provisioning role is denied all of it.
VECTOR_DATA_ACTIONS = (
    "s3vectors:GetVectors",
    "s3vectors:QueryVectors",
    "s3vectors:ListVectors",
    "s3vectors:PutVectors",
    "s3vectors:DeleteVectors",
)
FACE_COLLECTION = (
    "rekognition:DescribeCollection",
    "rekognition:IndexFaces",
    "rekognition:ListFaces",
    "rekognition:SearchFaces",
    "rekognition:SearchFacesByImage",
    "rekognition:DeleteFaces",
)

# Everything the tenant role may do in its bucket. The bucket policy denies it the rest.
TENANT_BUCKET_ACTIONS = (*OBJECT_READ, *OBJECT_WRITE, *BUCKET_LIST)

# Never allowed to a tenant session, whatever else changes: changing the tags and names the
# policies match on, starting another session, and getting around Object Lock.
TENANT_DENIED_ACTIONS = (
    "kms:TagResource",
    "kms:UntagResource",
    "s3:PutBucketTagging",
    "s3vectors:TagResource",
    "s3vectors:UntagResource",
    "rekognition:TagResource",
    "rekognition:UntagResource",
    "sts:AssumeRole",
    "sts:TagSession",
    "sts:SetSourceIdentity",
    "s3:BypassGovernanceRetention",
    "s3:DeleteObjectVersion",
    "s3:PutObjectRetention",
    "s3:PutObjectLegalHold",
)

# Object data. The provisioning role is denied all of it: it manages bucket settings only.
# (It keeps s3:ListBucket, which HeadBucket needs, so it can see key names but no content.)
OBJECT_DATA_ACTIONS = (
    "s3:GetObject*",
    "s3:PutObject*",
    "s3:DeleteObject*",
    "s3:RestoreObject",
    "s3:AbortMultipartUpload",
    "s3:ListMultipartUploadParts",
    "s3:ListBucketVersions",
    "s3:ListBucketMultipartUploads",
    "s3:BypassGovernanceRetention",
    "s3:ReplicateObject",
    "s3:ReplicateDelete",
    "s3:ReplicateTags",
    "s3:InitiateReplication",
)

# What GuardDuty Malware Protection for S3 does with the scan role (AWS's documented role
# policy): read and tag objects, check the bucket, turn on EventBridge notifications and
# write one test object.
SCAN_BUCKET_ACTIONS = (
    "s3:GetObject",
    "s3:GetObjectVersion",
    "s3:GetObjectTagging",
    "s3:GetObjectVersionTagging",
    "s3:PutObjectTagging",
    "s3:PutObjectVersionTagging",
    "s3:PutObject",
    "s3:ListBucket",
    "s3:GetBucketNotification",
    "s3:PutBucketNotification",
)

# Key administration by the provisioning role. No cryptographic operation is in this list.
KEY_ADMIN_ACTIONS = (
    "kms:DescribeKey",
    "kms:GetKeyPolicy",
    "kms:PutKeyPolicy",
    "kms:GetKeyRotationStatus",
    "kms:EnableKeyRotation",
    "kms:ListResourceTags",
    "kms:TagResource",
    "kms:UntagResource",
    "kms:UpdateKeyDescription",
    "kms:EnableKey",
    "kms:DisableKey",
    "kms:ScheduleKeyDeletion",
    "kms:CancelKeyDeletion",
    "kms:CreateAlias",
    "kms:UpdateAlias",
    "kms:DeleteAlias",
)
# Readable through IAM by the account (AWS Config, an auditor): metadata, never data.
KEY_METADATA_ACTIONS = (
    "kms:DescribeKey",
    "kms:GetKeyPolicy",
    "kms:GetKeyRotationStatus",
    "kms:ListResourceTags",
)
BREAK_GLASS_KEY_ACTIONS = (
    "kms:Decrypt",
    "kms:DescribeKey",
    "kms:GetKeyPolicy",
    "kms:PutKeyPolicy",
    "kms:GetKeyRotationStatus",
    "kms:ListResourceTags",
)
KEY_TAG_ACTIONS = ("kms:TagResource", "kms:UntagResource")

S3_VECTORS_INDEXING_SERVICE = "indexing.s3vectors.amazonaws.com"
S3_VECTORS_CONTEXT_KEYS = ("aws:s3vectors:arn", "aws:s3vectors:resource-id")


@dataclass(frozen=True)
class Principals:
    """The four roles a tenant's bucket and key policies know. Everyone else is refused."""

    tenant_role_arn: str  # data, through the TSS, one tenant per session
    break_glass_role_arn: str  # emergency read and policy repair; every use raises an alarm
    provisioning_role_arn: str  # settings, tags and policies; never object data or decrypt
    scan_role_arn: str  # GuardDuty Malware Protection: read and tag objects


def to_json(policy: Policy) -> str:
    return json.dumps(policy, separators=(",", ":"))


def _s3_service(region: str) -> str:
    return f"s3.{region}.amazonaws.com"


def _s3vectors_service(region: str) -> str:
    return f"s3vectors.{region}.amazonaws.com"


# --- The tenant role --------------------------------------------------------------------


def tenant_role_trust_policy(*, tss_role_arn: str, name_prefix: str) -> Policy:
    """Only the TSS Lambda's role, and only with all three tags and a source identity."""
    pattern = tenant_pattern(name_prefix)
    return {
        "Version": VERSION,
        "Statement": [
            {
                "Sid": "OnlyTheTenantStorageService",
                "Effect": "Allow",
                "Principal": {"AWS": tss_role_arn},
                "Action": ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"],
                "Condition": {
                    "StringLike": {
                        f"aws:RequestTag/{TENANT_TAG}": "?*",
                        f"aws:RequestTag/{BUCKET_TAG}": pattern,
                        f"aws:RequestTag/{VECTOR_BUCKET_TAG}": pattern + VECTOR_BUCKET_SUFFIX,
                        "sts:SourceIdentity": "?*",
                    },
                    "ForAllValues:StringEquals": {"aws:TagKeys": list(SESSION_TAG_KEYS)},
                },
            }
        ],
    }


def tenant_role_policy(*, region: str, account_id: str) -> Policy:
    """The one template. Every resource comes from the session's tags."""
    bucket = "arn:aws:s3:::${aws:PrincipalTag/" + BUCKET_TAG + "}"
    vectors = (
        f"arn:aws:s3vectors:{region}:{account_id}:bucket/"
        "${aws:PrincipalTag/" + VECTOR_BUCKET_TAG + "}/index/*"
    )
    collection = (
        f"arn:aws:rekognition:{region}:{account_id}:collection/"
        "${aws:PrincipalTag/" + BUCKET_TAG + "}"
    )
    return {
        "Version": VERSION,
        "Statement": [
            {
                "Sid": "TenantObjects",
                "Effect": "Allow",
                "Action": [*OBJECT_READ, *OBJECT_WRITE],
                "Resource": f"{bucket}/*",
            },
            {
                "Sid": "TenantBucket",
                "Effect": "Allow",
                "Action": list(BUCKET_LIST),
                "Resource": bucket,
            },
            {
                # A key ARN cannot be built from a tag, so the key is matched by its own tag.
                "Sid": "TenantKey",
                "Effect": "Allow",
                "Action": [*KEY_USE, *KEY_DESCRIBE],
                "Resource": f"arn:aws:kms:{region}:{account_id}:key/*",
                "Condition": {
                    "StringEquals": {
                        f"aws:ResourceTag/{TENANT_TAG}": "${aws:PrincipalTag/" + TENANT_TAG + "}"
                    }
                },
            },
            {
                "Sid": "TenantVectors",
                "Effect": "Allow",
                "Action": [*VECTOR_READ, *VECTOR_WRITE],
                "Resource": vectors,
            },
            {
                "Sid": "TenantFaces",
                "Effect": "Allow",
                "Action": list(FACE_COLLECTION),
                "Resource": collection,
            },
            {
                "Sid": "NeverTagsSessionsOrObjectLock",
                "Effect": "Deny",
                "Action": list(TENANT_DENIED_ACTIONS),
                "Resource": "*",
            },
        ],
    }


# --- The session ------------------------------------------------------------------------


def session_tags(profile: TenantStorageProfile) -> list[dict[str, str]]:
    return [
        {"Key": TENANT_TAG, "Value": str(profile.tenant_id)},
        {"Key": BUCKET_TAG, "Value": profile.bucket},
        {"Key": VECTOR_BUCKET_TAG, "Value": profile.vector_bucket},
    ]


def session_policy(
    access: StorageAccess, profile: TenantStorageProfile, *, account_id: str
) -> Policy:
    """Exact ARNs only: this tenant's bucket, key, index and collection, nothing wider."""
    bucket = f"arn:aws:s3:::{profile.bucket}"
    key = profile.kms_key_arn
    index = profile.vector_index_arn(account_id)
    collection = profile.face_collection_arn(account_id)

    def allow(actions: tuple[str, ...], resource: str) -> dict[str, Any]:
        return {"Effect": "Allow", "Action": list(actions), "Resource": resource}

    statements: list[dict[str, Any]]
    match access:
        case StorageAccess.UPLOAD:
            statements = [
                allow(("s3:PutObject",), f"{bucket}/{keys.INBOX_PREFIX}*"),
                allow(("kms:GenerateDataKey",), key),
            ]
        case StorageAccess.READ:
            statements = [
                allow(OBJECT_READ, f"{bucket}/*"),
                allow(BUCKET_LIST, bucket),
                allow(("kms:Decrypt", *KEY_DESCRIBE), key),
                allow(VECTOR_READ, index),
            ]
        case StorageAccess.PIPELINE:
            statements = [
                allow((*OBJECT_READ, *OBJECT_WRITE), f"{bucket}/*"),
                allow(BUCKET_LIST, bucket),
                allow((*KEY_USE, *KEY_DESCRIBE), key),
                allow((*VECTOR_READ, *VECTOR_WRITE), index),
                allow(FACE_COLLECTION, collection),
            ]
    return {"Version": VERSION, "Statement": statements}


def session_policy_json(
    access: StorageAccess, profile: TenantStorageProfile, *, account_id: str
) -> str:
    text = to_json(session_policy(access, profile, account_id=account_id))
    if len(text) > SESSION_POLICY_MAX_CHARS:
        raise ValueError(f"session policy is {len(text)} characters; the limit is 2048")
    return text


# --- A tenant's bucket ------------------------------------------------------------------


def tenant_bucket_policy(*, bucket: str, tenant_id: str, principals: Principals) -> Policy:
    """Deny everything to everyone, except each of the four roles for its own job.

    A bucket policy cannot grant what an identity policy withholds here: every statement is
    a Deny. The roles' own policies do the allowing.
    """
    bucket_arn = f"arn:aws:s3:::{bucket}"
    both = [bucket_arn, f"{bucket_arn}/*"]
    p = principals

    def is_role(arn: str) -> dict[str, Any]:
        return {"ArnEquals": {"aws:PrincipalArn": arn}}

    return {
        "Version": VERSION,
        "Statement": [
            {
                "Sid": "DenyInsecureTransport",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": both,
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            },
            {
                "Sid": "DenyEveryOtherPrincipal",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": both,
                "Condition": {
                    "ArnNotEquals": {
                        "aws:PrincipalArn": [
                            p.tenant_role_arn,
                            p.break_glass_role_arn,
                            p.provisioning_role_arn,
                            p.scan_role_arn,
                        ]
                    }
                },
            },
            {
                # The tenant role is shared by every tenant; the session tag says which one.
                "Sid": "DenyOtherTenants",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": both,
                "Condition": {
                    **is_role(p.tenant_role_arn),
                    "StringNotEquals": {f"aws:PrincipalTag/{TENANT_TAG}": tenant_id},
                },
            },
            {
                "Sid": "TenantRoleObjectsOnly",
                "Effect": "Deny",
                "Principal": "*",
                "NotAction": list(TENANT_BUCKET_ACTIONS),
                "Resource": both,
                "Condition": is_role(p.tenant_role_arn),
            },
            {
                "Sid": "ProvisioningRoleNeverTouchesObjects",
                "Effect": "Deny",
                "Principal": "*",
                "Action": list(OBJECT_DATA_ACTIONS),
                "Resource": both,
                "Condition": is_role(p.provisioning_role_arn),
            },
            {
                "Sid": "ScanRoleScansOnly",
                "Effect": "Deny",
                "Principal": "*",
                "NotAction": list(SCAN_BUCKET_ACTIONS),
                "Resource": both,
                "Condition": is_role(p.scan_role_arn),
            },
            {
                "Sid": "ScanRoleWritesOnlyItsTestObject",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:PutObject",
                "NotResource": f"{bucket_arn}/{keys.SCAN_VALIDATION_KEY}",
                "Condition": is_role(p.scan_role_arn),
            },
            {
                "Sid": "OnlyProvisioningChangesTags",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:PutBucketTagging",
                "Resource": bucket_arn,
                "Condition": {"ArnNotEquals": {"aws:PrincipalArn": p.provisioning_role_arn}},
            },
        ],
    }


# --- A tenant's vector bucket -----------------------------------------------------------


def tenant_vector_bucket_policy(
    *,
    region: str,
    account_id: str,
    vector_bucket: str,
    tenant_id: str,
    principals: Principals,
) -> Policy:
    """Admit only the tenant role (tagged for this tenant) and the provisioning role.

    Every statement is a Deny, as in the bucket policy. The break-glass role is not
    admitted: vectors are derived from the documents and can be rebuilt. AWS services
    acting on their own behalf (S3 Vectors' background indexing) are left alone.
    """
    bucket_arn = f"arn:aws:s3vectors:{region}:{account_id}:bucket/{vector_bucket}"
    # The bucket itself, and every index in it.
    both = [bucket_arn, f"{bucket_arn}/*"]
    p = principals
    return {
        "Version": VERSION,
        "Statement": [
            {
                "Sid": "DenyEveryOtherPrincipal",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3vectors:*",
                "Resource": both,
                "Condition": {
                    "ArnNotEquals": {
                        "aws:PrincipalArn": [p.tenant_role_arn, p.provisioning_role_arn]
                    },
                    "Bool": {"aws:PrincipalIsAWSService": "false"},
                },
            },
            {
                "Sid": "DenyOtherTenants",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3vectors:*",
                "Resource": both,
                "Condition": {
                    "ArnEquals": {"aws:PrincipalArn": p.tenant_role_arn},
                    "StringNotEquals": {f"aws:PrincipalTag/{TENANT_TAG}": tenant_id},
                },
            },
            {
                "Sid": "ProvisioningRoleNeverTouchesVectors",
                "Effect": "Deny",
                "Principal": "*",
                "Action": list(VECTOR_DATA_ACTIONS),
                "Resource": both,
                "Condition": {"ArnEquals": {"aws:PrincipalArn": p.provisioning_role_arn}},
            },
        ],
    }


# --- A tenant's key ---------------------------------------------------------------------


def tenant_key_policy(
    *,
    region: str,
    account_id: str,
    tenant_id: str,
    vector_bucket: str,
    principals: Principals,
) -> Policy:
    """No statement hands the key to the account's IAM policies for use.

    The usual `kms:*` for the account root is absent, so no IAM policy anywhere can grant
    decrypt on a tenant key: only the principals named here can use it.
    """
    p = principals
    s3 = _s3_service(region)
    vectors = _s3vectors_service(region)
    vector_bucket_arn = f"arn:aws:s3vectors:{region}:{account_id}:bucket/{vector_bucket}"
    tenant_tag = {f"aws:PrincipalTag/{TENANT_TAG}": tenant_id}
    return {
        "Version": VERSION,
        "Statement": [
            {
                "Sid": "AccountReadsMetadataOnly",
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
                "Action": list(KEY_METADATA_ACTIONS),
                "Resource": "*",
            },
            {
                "Sid": "ProvisioningAdministers",
                "Effect": "Allow",
                "Principal": {"AWS": p.provisioning_role_arn},
                "Action": list(KEY_ADMIN_ACTIONS),
                "Resource": "*",
            },
            {
                # S3 Vectors requires GenerateDataKey of whoever creates a vector bucket with
                # this key. It is no use for reading data: the role never gets Decrypt.
                "Sid": "ProvisioningCreatesTheVectorBucket",
                "Effect": "Allow",
                "Principal": {"AWS": p.provisioning_role_arn},
                "Action": "kms:GenerateDataKey",
                "Resource": "*",
                "Condition": {"StringEquals": {"kms:ViaService": vectors}},
            },
            {
                "Sid": "TenantUsesThroughStorage",
                "Effect": "Allow",
                "Principal": {"AWS": p.tenant_role_arn},
                "Action": list(KEY_USE),
                "Resource": "*",
                "Condition": {"StringEquals": {**tenant_tag, "kms:ViaService": [s3, vectors]}},
            },
            {
                "Sid": "TenantDescribes",
                "Effect": "Allow",
                "Principal": {"AWS": p.tenant_role_arn},
                "Action": list(KEY_DESCRIBE),
                "Resource": "*",
                "Condition": {"StringEquals": tenant_tag},
            },
            {
                "Sid": "MalwareScanReadsObjects",
                "Effect": "Allow",
                "Principal": {"AWS": p.scan_role_arn},
                "Action": list(KEY_USE),
                "Resource": "*",
                "Condition": {"StringEquals": {"kms:ViaService": s3}},
            },
            {
                "Sid": "BreakGlass",
                "Effect": "Allow",
                "Principal": {"AWS": p.break_glass_role_arn},
                "Action": list(BREAK_GLASS_KEY_ACTIONS),
                "Resource": "*",
            },
            {
                # S3 Vectors maintains the index in the background (AWS's documented statement,
                # narrowed to this tenant's vector bucket).
                "Sid": "S3VectorsIndexing",
                "Effect": "Allow",
                "Principal": {"Service": S3_VECTORS_INDEXING_SERVICE},
                "Action": "kms:Decrypt",
                "Resource": "*",
                "Condition": {
                    "ArnLike": {"aws:SourceArn": [vector_bucket_arn, f"{vector_bucket_arn}/*"]},
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ForAnyValue:StringEquals": {
                        "kms:EncryptionContextKeys": list(S3_VECTORS_CONTEXT_KEYS)
                    },
                },
            },
            {
                # The tenant role's policy matches this key by its tenant_id tag.
                "Sid": "OnlyProvisioningChangesTags",
                "Effect": "Deny",
                "Principal": {"AWS": "*"},
                "Action": list(KEY_TAG_ACTIONS),
                "Resource": "*",
                "Condition": {"ArnNotEquals": {"aws:PrincipalArn": p.provisioning_role_arn}},
            },
        ],
    }
