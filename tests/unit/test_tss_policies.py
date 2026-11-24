"""The policies that keep tenants apart (BUILD_SPEC.md section 6, rules 1 and 2).

These read the policy documents the way a reviewer would: what each one can ever allow,
and what it always denies. `tests/isolation` proves the same things against real AWS.
"""

from __future__ import annotations

import fnmatch
import json
import uuid
from typing import Any

import pytest

from assay_tss import keys, policies
from assay_tss.models import IsolationMode, StorageAccess, TenantStorageProfile
from assay_tss.policies import Principals

REGION = "us-east-1"
ACCOUNT = "000000000000"
PREFIX = "assay-dev"
TENANT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
TENANT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
ROLE = f"arn:aws:iam::{ACCOUNT}:role"
PRINCIPALS = Principals(
    tenant_role_arn=f"{ROLE}/assay-dev-tenant",
    break_glass_role_arn=f"{ROLE}/assay-dev-break-glass",
    provisioning_role_arn=f"{ROLE}/provisioning",
    scan_role_arn=f"{ROLE}/scan",
)
CRYPTO = {"kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:ReEncrypt*", "kms:*"}
TAGGING = {"kms:TagResource", "kms:UntagResource"}


def profile(tenant_id: uuid.UUID) -> TenantStorageProfile:
    slug = f"{PREFIX}-t-{tenant_id.hex[:8]}"
    return TenantStorageProfile(
        tenant_id=tenant_id,
        mode=IsolationMode.DEDICATED,
        bucket=slug,
        kms_key_arn=f"arn:aws:kms:{REGION}:{ACCOUNT}:key/{tenant_id}",
        role_arn=PRINCIPALS.tenant_role_arn,
        vector_bucket=f"{slug}-vectors",
        vector_index="documents",
        region=REGION,
    )


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def statements(policy: dict[str, Any], effect: str | None = None) -> list[dict[str, Any]]:
    return [s for s in policy["Statement"] if effect is None or s["Effect"] == effect]


def by_sid(policy: dict[str, Any], sid: str) -> dict[str, Any]:
    found: list[dict[str, Any]] = [s for s in policy["Statement"] if s.get("Sid") == sid]
    [statement] = found
    return statement


def actions(statement: dict[str, Any]) -> set[str]:
    return set(as_list(statement.get("Action", [])))


# --- Trust policy: only the TSS ---------------------------------------------------------------


def trust_policy() -> dict[str, Any]:
    return policies.tenant_role_trust_policy(tss_role_arn=f"{ROLE}/tss", name_prefix=PREFIX)


def trust_allows(action: str, context: dict[str, Any]) -> bool:
    """Whether the trust policy allows one STS action, given that action's request context.

    Handles the two operators the trust policy uses, as IAM does: StringLike needs the key
    to be present and to match; ForAllValues is true when the key is absent.
    """
    for statement in trust_policy()["Statement"]:
        if action not in as_list(statement["Action"]):
            continue
        holds = True
        for operator, tests in statement["Condition"].items():
            for key, expected in tests.items():
                if operator == "StringLike":
                    holds &= key in context and fnmatch.fnmatchcase(str(context[key]), expected)
                elif operator == "ForAllValues:StringEquals":
                    holds &= set(as_list(context.get(key, []))) <= set(expected)
                else:
                    raise AssertionError(f"operator {operator} is not handled by this test")
        if holds:
            return True
    return False


# What STS puts in each action's request context for one AssumeRole call with tags and a
# source identity. The tags are not in the context for sts:SetSourceIdentity.
SESSION_TAGS = {
    "aws:RequestTag/tenant_id": str(TENANT_A),
    "aws:RequestTag/bucket": "assay-dev-t-aaaaaaaa",
    "aws:RequestTag/vector_bucket": "assay-dev-t-aaaaaaaa-vectors",
    "aws:TagKeys": ["tenant_id", "bucket", "vector_bucket"],
}
SOURCE_IDENTITY = {"sts:SourceIdentity": "api.batch-42"}


def test_only_the_tss_role_may_assume_the_tenant_role() -> None:
    trust = trust_policy()
    assert {s["Effect"] for s in trust["Statement"]} == {"Allow"}
    assert all(s["Principal"] == {"AWS": f"{ROLE}/tss"} for s in trust["Statement"])
    # One statement per action, so each carries only the conditions that apply to it.
    assert [s["Action"] for s in trust["Statement"]] == [
        "sts:AssumeRole",
        "sts:TagSession",
        "sts:SetSourceIdentity",
    ]


def test_trust_policy_requires_the_tags_and_a_source_identity() -> None:
    assume = by_sid(trust_policy(), "OnlyTheTenantStorageService")
    required = assume["Condition"]["StringLike"]
    assert required == {
        "aws:RequestTag/tenant_id": "?*",
        "aws:RequestTag/bucket": "assay-dev-t-*",
        "aws:RequestTag/vector_bucket": "assay-dev-t-*-vectors",
        "sts:SourceIdentity": "?*",
    }
    # No other tag may ride along.
    assert assume["Condition"]["ForAllValues:StringEquals"]["aws:TagKeys"] == [
        "tenant_id",
        "bucket",
        "vector_bucket",
    ]


def test_each_sts_action_is_allowed_in_its_own_request_context() -> None:
    """Regression: every TSS resolve failed (2026-11-23).

    One statement put the tag conditions on all three actions, and STS refused
    `sts:SetSourceIdentity`: the session tags are not in that action's request context.
    Each action must be allowed with only the keys its own context carries.
    """
    assert trust_allows("sts:AssumeRole", {**SESSION_TAGS, **SOURCE_IDENTITY})
    assert trust_allows("sts:TagSession", SESSION_TAGS)
    assert trust_allows("sts:SetSourceIdentity", SOURCE_IDENTITY)
    tagging = by_sid(trust_policy(), "WithTheThreeSessionTags")
    assert "sts:SourceIdentity" not in json.dumps(tagging)
    identity = by_sid(trust_policy(), "WithASourceIdentity")
    assert "aws:RequestTag" not in json.dumps(identity)
    assert "aws:TagKeys" not in json.dumps(identity)


@pytest.mark.parametrize(
    ("what", "action", "context"),
    [
        ("no session without a source identity", "sts:AssumeRole", SESSION_TAGS),
        ("no session without tags", "sts:AssumeRole", SOURCE_IDENTITY),
        (
            "no session without the tenant tag",
            "sts:AssumeRole",
            {
                **{k: v for k, v in SESSION_TAGS.items() if k != "aws:RequestTag/tenant_id"},
                **SOURCE_IDENTITY,
            },
        ),
        (
            "no session for a bucket that is not a tenant bucket",
            "sts:AssumeRole",
            {
                **SESSION_TAGS,
                **SOURCE_IDENTITY,
                "aws:RequestTag/bucket": "assay-dev-provision-state",
            },
        ),
        (
            "no extra tag key",
            "sts:TagSession",
            {**SESSION_TAGS, "aws:TagKeys": ["tenant_id", "bucket", "vector_bucket", "admin"]},
        ),
        ("no tagging without the three tags", "sts:TagSession", {"aws:TagKeys": ["tenant_id"]}),
        ("no empty source identity", "sts:SetSourceIdentity", {"sts:SourceIdentity": ""}),
        ("no source identity action without one", "sts:SetSourceIdentity", {}),
        ("no other action", "sts:AssumeRoleWithWebIdentity", {**SESSION_TAGS, **SOURCE_IDENTITY}),
    ],
)
def test_trust_policy_refuses(what: str, action: str, context: dict[str, Any]) -> None:
    assert not trust_allows(action, context), what


# --- The one template -------------------------------------------------------------------------


def test_role_policy_resources_all_come_from_the_session_tags() -> None:
    policy = policies.tenant_role_policy(region=REGION, account_id=ACCOUNT)
    for statement in statements(policy, "Allow"):
        resources = as_list(statement["Resource"])
        assert "*" not in resources
        from_tag = all("${aws:PrincipalTag/" in r for r in resources)
        tag_condition = statement.get("Condition", {}).get("StringEquals", {})
        key_by_tag = tag_condition == {"aws:ResourceTag/tenant_id": "${aws:PrincipalTag/tenant_id}"}
        assert from_tag or key_by_tag, statement["Sid"]


def test_role_policy_names_no_tenant() -> None:
    text = json.dumps(policies.tenant_role_policy(region=REGION, account_id=ACCOUNT))
    assert "-t-" not in text
    assert text.count("${aws:PrincipalTag/") >= 5


def test_role_policy_never_allows_tags_sessions_or_object_lock_bypass() -> None:
    policy = policies.tenant_role_policy(region=REGION, account_id=ACCOUNT)
    allowed = set().union(*(actions(s) for s in statements(policy, "Allow")))
    assert not allowed & set(policies.TENANT_DENIED_ACTIONS)
    assert not any(a.endswith("*") for a in allowed)
    [deny] = statements(policy, "Deny")
    assert deny["Resource"] == "*" and "Condition" not in deny
    assert actions(deny) == set(policies.TENANT_DENIED_ACTIONS)
    for action in (
        "kms:TagResource",
        "kms:UntagResource",
        "s3:PutBucketTagging",
        "s3vectors:TagResource",
        "rekognition:TagResource",
        "sts:AssumeRole",
        "s3:BypassGovernanceRetention",
        "s3:DeleteObjectVersion",
    ):
        assert action in actions(deny)


# --- Session policy: the exact bucket, key, index and collection ------------------------------


@pytest.mark.parametrize("access", list(StorageAccess))
def test_session_policy_names_only_this_tenants_resources(access: StorageAccess) -> None:
    a, b = profile(TENANT_A), profile(TENANT_B)
    policy = policies.session_policy(access, a, account_id=ACCOUNT)
    text = json.dumps(policy)
    assert b.bucket not in text and str(TENANT_B) not in text
    for statement in policy["Statement"]:
        assert statement["Effect"] == "Allow"
        for resource in as_list(statement["Resource"]):
            assert resource != "*"
            assert a.bucket in resource or resource == a.kms_key_arn
            # The only wildcard is "every object in this bucket" or under one fixed prefix.
            trimmed = resource.removesuffix("/*")
            for prefix in (keys.INBOX_PREFIX, *policies.READ_PREFIXES):
                trimmed = trimmed.removesuffix(f"/{prefix}*")
            assert "*" not in trimmed


@pytest.mark.parametrize("access", list(StorageAccess))
def test_session_policy_allows_nothing_the_role_template_does_not(access: StorageAccess) -> None:
    template = policies.tenant_role_policy(region=REGION, account_id=ACCOUNT)
    template_allows = set().union(*(actions(s) for s in statements(template, "Allow")))
    session = policies.session_policy(access, profile(TENANT_A), account_id=ACCOUNT)
    session_allows = set().union(*(actions(s) for s in session["Statement"]))
    assert session_allows <= template_allows
    assert not session_allows & set(policies.TENANT_DENIED_ACTIONS)


def test_upload_access_is_put_into_the_inbox_and_nothing_else() -> None:
    a = profile(TENANT_A)
    policy = policies.session_policy(StorageAccess.UPLOAD, a, account_id=ACCOUNT)
    assert policy["Statement"] == [
        {
            "Effect": "Allow",
            "Action": ["s3:PutObject"],
            "Resource": f"arn:aws:s3:::{a.bucket}/inbox/*",
        },
        {"Effect": "Allow", "Action": ["kms:GenerateDataKey"], "Resource": a.kms_key_arn},
    ]


def test_read_access_cannot_write_or_reach_faces() -> None:
    policy = policies.session_policy(StorageAccess.READ, profile(TENANT_A), account_id=ACCOUNT)
    allowed = set().union(*(actions(s) for s in policy["Statement"]))
    assert not allowed & {*policies.OBJECT_WRITE, *policies.VECTOR_WRITE, "kms:GenerateDataKey"}
    assert not any(a.startswith("rekognition:") for a in allowed)


def test_read_access_reads_only_originals_derived_and_chat() -> None:
    """Condition (c), build step 6: unscanned (inbox/) and infected (quarantine/) objects, in any
    version, are readable by pipeline sessions only."""
    a = profile(TENANT_A)
    policy = policies.session_policy(StorageAccess.READ, a, account_id=ACCOUNT)
    object_resources = {
        r
        for s in policy["Statement"]
        if set(actions(s)) & set(policies.OBJECT_READ)
        for r in as_list(s["Resource"])
    }
    bucket = f"arn:aws:s3:::{a.bucket}"
    assert object_resources == {f"{bucket}/originals/*", f"{bucket}/derived/*", f"{bucket}/chat/*"}
    for resource in object_resources:
        assert not resource.startswith((f"{bucket}/inbox", f"{bucket}/quarantine", f"{bucket}/*"))


@pytest.mark.parametrize("access", list(StorageAccess))
def test_no_session_may_list_the_bucket(access: StorageAccess) -> None:
    """The pipeline and the API work from database rows and scan events, never a listing."""
    policy = policies.session_policy(access, profile(TENANT_A), account_id=ACCOUNT)
    allowed = set().union(*(actions(s) for s in policy["Statement"]))
    # ListMultipartUploadParts lists one upload's parts, not the bucket.
    assert not {a for a in allowed if a.startswith("s3:ListBucket")}
    resources = {r for s in policy["Statement"] for r in as_list(s["Resource"])}
    assert f"arn:aws:s3:::{profile(TENANT_A).bucket}" not in resources


def test_only_pipeline_access_reads_inbox_and_quarantine() -> None:
    a = profile(TENANT_A)
    bucket = f"arn:aws:s3:::{a.bucket}"
    for access in StorageAccess:
        policy = policies.session_policy(access, a, account_id=ACCOUNT)
        reads = {
            r
            for s in policy["Statement"]
            if set(actions(s)) & {"s3:GetObject", "s3:GetObjectVersion"}
            for r in as_list(s["Resource"])
        }
        if access is StorageAccess.PIPELINE:
            assert reads == {f"{bucket}/*"}
        else:
            assert f"{bucket}/*" not in reads
            assert not any(r.startswith((f"{bucket}/inbox", f"{bucket}/quarantine")) for r in reads)


def test_pipeline_access_reaches_the_exact_index_and_collection() -> None:
    a = profile(TENANT_A)
    policy = policies.session_policy(StorageAccess.PIPELINE, a, account_id=ACCOUNT)
    resources = {r for s in policy["Statement"] for r in as_list(s["Resource"])}
    assert a.vector_index_arn(ACCOUNT) in resources
    assert a.face_collection_arn(ACCOUNT) in resources
    assert a.vector_index_arn(ACCOUNT).endswith(f"bucket/{a.vector_bucket}/index/documents")


@pytest.mark.parametrize("access", list(StorageAccess))
def test_session_policy_fits_the_sts_limit(access: StorageAccess) -> None:
    text = policies.session_policy_json(access, profile(TENANT_A), account_id=ACCOUNT)
    assert len(text) <= policies.SESSION_POLICY_MAX_CHARS
    assert json.loads(text)["Version"] == "2012-10-17"


def test_session_tags_are_the_three_the_trust_policy_expects() -> None:
    a = profile(TENANT_A)
    assert policies.session_tags(a) == [
        {"Key": "tenant_id", "Value": str(TENANT_A)},
        {"Key": "bucket", "Value": a.bucket},
        {"Key": "vector_bucket", "Value": a.vector_bucket},
    ]
    assert tuple(t["Key"] for t in policies.session_tags(a)) == policies.SESSION_TAG_KEYS


# --- Bucket policy ----------------------------------------------------------------------------


def bucket_policy() -> dict[str, Any]:
    return policies.tenant_bucket_policy(
        bucket=profile(TENANT_A).bucket, tenant_id=str(TENANT_A), principals=PRINCIPALS
    )


def test_bucket_policy_only_denies() -> None:
    policy = bucket_policy()
    assert {s["Effect"] for s in policy["Statement"]} == {"Deny"}
    assert all(s["Principal"] == "*" for s in policy["Statement"])


def test_bucket_policy_refuses_every_principal_but_the_four() -> None:
    statement = by_sid(bucket_policy(), "DenyEveryOtherPrincipal")
    assert statement["Action"] == "s3:*"
    assert set(statement["Condition"]["ArnNotEquals"]["aws:PrincipalArn"]) == {
        PRINCIPALS.tenant_role_arn,
        PRINCIPALS.break_glass_role_arn,
        PRINCIPALS.provisioning_role_arn,
        PRINCIPALS.scan_role_arn,
    }
    bucket = profile(TENANT_A).bucket
    assert statement["Resource"] == [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]


def test_bucket_policy_refuses_the_tenant_role_tagged_for_another_tenant() -> None:
    statement = by_sid(bucket_policy(), "DenyOtherTenants")
    assert statement["Action"] == "s3:*"
    assert statement["Condition"] == {
        "ArnEquals": {"aws:PrincipalArn": PRINCIPALS.tenant_role_arn},
        "StringNotEquals": {"aws:PrincipalTag/tenant_id": str(TENANT_A)},
    }


def test_bucket_policy_keeps_the_provisioning_role_away_from_objects() -> None:
    statement = by_sid(bucket_policy(), "ProvisioningRoleNeverTouchesObjects")
    assert statement["Condition"] == {
        "ArnEquals": {"aws:PrincipalArn": PRINCIPALS.provisioning_role_arn}
    }
    denied = actions(statement)
    assert {"s3:GetObject*", "s3:PutObject*", "s3:DeleteObject*", "s3:ListBucketVersions"} <= denied
    # Bucket settings stay possible, or the tenant's stack could never be updated.
    assert not any(a.startswith(("s3:GetBucket", "s3:PutBucket")) for a in denied)


def test_bucket_policy_limits_the_scan_role_to_scanning() -> None:
    policy = bucket_policy()
    statement = by_sid(policy, "ScanRoleScansOnly")
    assert set(statement["NotAction"]) == set(policies.SCAN_BUCKET_ACTIONS)
    assert not {"s3:DeleteObject", "s3:PutBucketPolicy"} & set(statement["NotAction"])
    writes = by_sid(policy, "ScanRoleWritesOnlyItsTestObject")
    bucket = profile(TENANT_A).bucket
    assert writes["Action"] == "s3:PutObject"
    assert writes["NotResource"] == f"arn:aws:s3:::{bucket}/{keys.SCAN_VALIDATION_KEY}"


def test_bucket_policy_limits_the_tenant_role_to_objects() -> None:
    statement = by_sid(bucket_policy(), "TenantRoleObjectsOnly")
    assert set(statement["NotAction"]) == set(policies.TENANT_BUCKET_ACTIONS)
    assert not any(
        "Bucket" in a and a != "s3:ListBucket" and a != "s3:GetBucketLocation"
        for a in statement["NotAction"]
    )


def test_only_the_provisioning_role_may_change_bucket_tags() -> None:
    statement = by_sid(bucket_policy(), "OnlyProvisioningChangesTags")
    assert statement["Action"] == "s3:PutBucketTagging"
    assert statement["Condition"] == {
        "ArnNotEquals": {"aws:PrincipalArn": PRINCIPALS.provisioning_role_arn}
    }


def test_bucket_policy_requires_tls() -> None:
    statement = by_sid(bucket_policy(), "DenyInsecureTransport")
    assert statement["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}


# --- Key policy -------------------------------------------------------------------------------


def key_policy() -> dict[str, Any]:
    return policies.tenant_key_policy(
        region=REGION,
        account_id=ACCOUNT,
        tenant_id=str(TENANT_A),
        vector_bucket=profile(TENANT_A).vector_bucket,
        principals=PRINCIPALS,
    )


def test_key_policy_never_hands_the_key_to_iam_for_use() -> None:
    """Without this, any IAM policy in the account could grant decrypt on a tenant key."""
    policy = key_policy()
    root = by_sid(policy, "AccountReadsMetadataOnly")
    assert root["Principal"] == {"AWS": f"arn:aws:iam::{ACCOUNT}:root"}
    assert actions(root) == set(policies.KEY_METADATA_ACTIONS)
    assert not actions(root) & (CRYPTO | TAGGING | {"kms:PutKeyPolicy", "kms:CreateGrant"})
    for statement in statements(policy, "Allow"):
        assert "kms:*" not in actions(statement)
        assert "kms:CreateGrant" not in actions(statement)


def test_provisioning_role_administers_but_never_decrypts() -> None:
    policy = key_policy()
    admin = by_sid(policy, "ProvisioningAdministers")
    assert admin["Principal"] == {"AWS": PRINCIPALS.provisioning_role_arn}
    assert not actions(admin) & CRYPTO
    assert actions(admin) >= TAGGING
    provisioning = [
        s
        for s in statements(policy, "Allow")
        if s["Principal"] == {"AWS": PRINCIPALS.provisioning_role_arn}
    ]
    granted = set().union(*(actions(s) for s in provisioning))
    assert not granted & {"kms:Encrypt", "kms:ReEncrypt*", "kms:CreateGrant", "kms:*"}
    # Regression (2026-11-23): S3 Vectors needs GenerateDataKey of whoever creates a vector
    # bucket and Decrypt of whoever creates a vector index. Both are granted only for calls
    # S3 Vectors makes on the role's behalf: never directly, and never through S3.
    use = by_sid(policy, "ProvisioningCreatesVectorBucketAndIndex")
    assert actions(use) == {"kms:GenerateDataKey", "kms:Decrypt"}
    assert use["Condition"] == {
        "StringEquals": {"kms:ViaService": f"s3vectors.{REGION}.amazonaws.com"}
    }
    unconditional = [
        s["Sid"] for s in provisioning if actions(s) & CRYPTO and s["Sid"] != use["Sid"]
    ]
    assert unconditional == []


def test_tenant_role_uses_the_key_only_for_its_own_tenant_and_only_through_storage() -> None:
    policy = key_policy()
    use = by_sid(policy, "TenantUsesThroughStorage")
    assert use["Principal"] == {"AWS": PRINCIPALS.tenant_role_arn}
    assert actions(use) == {"kms:Decrypt", "kms:GenerateDataKey"}
    assert use["Condition"]["StringEquals"] == {
        "aws:PrincipalTag/tenant_id": str(TENANT_A),
        "kms:ViaService": [f"s3.{REGION}.amazonaws.com", f"s3vectors.{REGION}.amazonaws.com"],
    }
    describe = by_sid(policy, "TenantDescribes")
    assert describe["Condition"] == {"StringEquals": {"aws:PrincipalTag/tenant_id": str(TENANT_A)}}
    for statement in (use, describe):
        assert not actions(statement) & TAGGING


def test_only_the_provisioning_role_may_tag_or_untag_a_tenant_key() -> None:
    policy = key_policy()
    deny = by_sid(policy, "OnlyProvisioningChangesTags")
    assert deny["Effect"] == "Deny"
    assert deny["Principal"] == {"AWS": "*"}
    assert actions(deny) == TAGGING
    assert deny["Condition"] == {
        "ArnNotEquals": {"aws:PrincipalArn": PRINCIPALS.provisioning_role_arn}
    }
    taggers = [s["Sid"] for s in statements(policy, "Allow") if actions(s) & TAGGING]
    assert taggers == ["ProvisioningAdministers"]


def test_scan_role_and_break_glass_get_only_what_they_need() -> None:
    policy = key_policy()
    scan = by_sid(policy, "MalwareScanReadsObjects")
    assert actions(scan) == {"kms:Decrypt", "kms:GenerateDataKey"}
    assert scan["Condition"] == {"StringEquals": {"kms:ViaService": f"s3.{REGION}.amazonaws.com"}}
    break_glass = by_sid(policy, "BreakGlass")
    assert break_glass["Principal"] == {"AWS": PRINCIPALS.break_glass_role_arn}
    assert "kms:Decrypt" in actions(break_glass)
    assert not actions(break_glass) & (TAGGING | {"kms:ScheduleKeyDeletion", "kms:CreateGrant"})


def test_s3_vectors_indexing_is_limited_to_this_tenants_vector_bucket() -> None:
    statement = by_sid(key_policy(), "S3VectorsIndexing")
    assert statement["Principal"] == {"Service": "indexing.s3vectors.amazonaws.com"}
    assert statement["Action"] == "kms:Decrypt"
    arn = f"arn:aws:s3vectors:{REGION}:{ACCOUNT}:bucket/{profile(TENANT_A).vector_bucket}"
    assert statement["Condition"]["ArnLike"] == {"aws:SourceArn": [arn, f"{arn}/*"]}
    assert statement["Condition"]["StringEquals"] == {"aws:SourceAccount": ACCOUNT}


def vector_bucket_policy() -> dict[str, Any]:
    return policies.tenant_vector_bucket_policy(
        region=REGION,
        account_id=ACCOUNT,
        vector_bucket=profile(TENANT_A).vector_bucket,
        tenant_id=str(TENANT_A),
        principals=PRINCIPALS,
    )


def test_vector_bucket_policy_admits_only_the_tenant_role_and_the_provisioning_role() -> None:
    policy = vector_bucket_policy()
    assert {s["Effect"] for s in policy["Statement"]} == {"Deny"}
    arn = f"arn:aws:s3vectors:{REGION}:{ACCOUNT}:bucket/{profile(TENANT_A).vector_bucket}"
    assert all(s["Resource"] == [arn, f"{arn}/*"] for s in policy["Statement"])
    everyone_else = by_sid(policy, "DenyEveryOtherPrincipal")
    assert everyone_else["Principal"] == "*" and everyone_else["Action"] == "s3vectors:*"
    assert everyone_else["Condition"] == {
        "ArnNotEquals": {
            "aws:PrincipalArn": [PRINCIPALS.tenant_role_arn, PRINCIPALS.provisioning_role_arn]
        },
        # S3 Vectors' own background indexing is not a principal to refuse.
        "Bool": {"aws:PrincipalIsAWSService": "false"},
    }
    # Not even the break-glass role or the scan role.
    admitted = everyone_else["Condition"]["ArnNotEquals"]["aws:PrincipalArn"]
    assert PRINCIPALS.break_glass_role_arn not in admitted
    assert PRINCIPALS.scan_role_arn not in admitted


def test_vector_bucket_policy_refuses_other_tenants_and_keeps_provisioning_from_vectors() -> None:
    policy = vector_bucket_policy()
    other_tenants = by_sid(policy, "DenyOtherTenants")
    assert other_tenants["Action"] == "s3vectors:*"
    assert other_tenants["Condition"] == {
        "ArnEquals": {"aws:PrincipalArn": PRINCIPALS.tenant_role_arn},
        "StringNotEquals": {"aws:PrincipalTag/tenant_id": str(TENANT_A)},
    }
    provisioning = by_sid(policy, "ProvisioningRoleNeverTouchesVectors")
    assert provisioning["Condition"] == {
        "ArnEquals": {"aws:PrincipalArn": PRINCIPALS.provisioning_role_arn}
    }
    assert set(provisioning["Action"]) == {
        "s3vectors:GetVectors",
        "s3vectors:QueryVectors",
        "s3vectors:ListVectors",
        "s3vectors:PutVectors",
        "s3vectors:DeleteVectors",
    }
    # It still manages the bucket, the index and this policy.
    assert not {"s3vectors:CreateIndex", "s3vectors:PutVectorBucketPolicy"} & set(
        provisioning["Action"]
    )


def test_two_tenants_policies_share_no_resource() -> None:
    a = policies.session_policy(StorageAccess.PIPELINE, profile(TENANT_A), account_id=ACCOUNT)
    b = policies.session_policy(StorageAccess.PIPELINE, profile(TENANT_B), account_id=ACCOUNT)
    resources_a = {r for s in a["Statement"] for r in as_list(s["Resource"])}
    resources_b = {r for s in b["Statement"] for r in as_list(s["Resource"])}
    assert not resources_a & resources_b
