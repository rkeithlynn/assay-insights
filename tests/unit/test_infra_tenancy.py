"""The TSS, provisioning and isolation-runner components, using Pulumi mocks (no AWS calls)."""

from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Any

import pulumi
import pytest

from assay_infra import alerts, isolation, provisioning, tss
from assay_infra.alerts import Alerts
from assay_infra.auth import Auth
from assay_infra.auth_events import AuthEvents
from assay_infra.cognito_admin import CognitoAdminApi
from assay_infra.config import (
    AlertsConfig,
    TenancyConfig,
    validate_alerts,
    validate_tenancy,
)
from assay_infra.database import Database
from assay_infra.isolation import IsolationRunner, runner_role
from assay_infra.network import Network
from assay_infra.provisioning import Provisioning
from assay_infra.tss import TenantStorage, TssCaller
from assay_tss import policies
from tests.unit.conftest import ACCOUNT_ID, REGION, registered
from tests.unit.test_infra_auth_api import AUTH, DNS, FAKE_JWKS
from tests.unit.test_infra_data import DB, NET

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFIX = "assay-dev"
BREAK_GLASS_PRINCIPAL = f"arn:aws:iam::{ACCOUNT_ID}:role/aws-reserved/sso.amazonaws.com/Admin"
TAGS = {"app": "assay-insights", "env": "dev"}
ROLE = f"arn:aws:iam::{ACCOUNT_ID}:role"
ALARM_EMAIL = "alarms@example.test"
DEFAULT_MODELS = ("us.example.ingest-v1:0", "us.example.query-v1")
# The callers the platform stack adds to the API's Cognito admin function (assay_infra/__main__).
API_COGNITO_CALLERS = [
    f"arn:aws:iam::{ACCOUNT_ID}:role/api-task",
    f"arn:aws:iam::{ACCOUNT_ID}:role/seed",
]
PLATFORM_KEYS = [
    f"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/database",
    f"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/state",
]


def _resolve(value: str, request: dict[str, Any]) -> str | None:
    """A condition value, with a `${key}` policy variable replaced from the request."""
    if value.startswith("${") and value.endswith("}"):
        found = request.get(value[2:-1])
        return None if found is None else str(found)
    return value


def conditions_hold(condition: dict[str, dict[str, Any]], request: dict[str, Any]) -> bool:
    """Evaluate the condition operators the provisioning role's policy uses, as IAM does."""
    for operator, tests in condition.items():
        for key, expected in tests.items():
            present = key in request
            if operator == "Null":
                holds = (not present) == (expected == "true")
            elif operator == "ForAllValues:StringEquals":
                holds = set(as_list(request.get(key, []))) <= set(as_list(expected))
            elif operator == "ForAnyValue:StringEquals":
                holds = bool(set(as_list(request.get(key, []))) & set(as_list(expected)))
            elif operator == "StringEquals":
                wanted = {_resolve(v, request) for v in as_list(expected)}
                holds = present and str(request[key]) in wanted
            elif operator == "StringNotEquals":
                # A negated operator matches when the key, or the variable, has no value.
                wanted = {_resolve(v, request) for v in as_list(expected)}
                holds = not present or str(request[key]) not in wanted
            else:
                raise AssertionError(f"operator {operator} is not handled by this test")
            if not holds:
                return False
    return True


def decide(policy: dict[str, Any], action: str, resource: str, request: dict[str, Any]) -> str:
    """IAM's decision for one request against one identity policy."""
    decision = "implicitDeny"
    for statement in policy["Statement"]:
        actions = as_list(statement["Action"])
        if not any(fnmatch.fnmatchcase(action, pattern) for pattern in actions):
            continue
        resources = as_list(statement["Resource"])
        if not any(fnmatch.fnmatchcase(resource, pattern) for pattern in resources):
            continue
        if not conditions_hold(statement.get("Condition", {}), request):
            continue
        if statement["Effect"] == "Deny":
            return "explicitDeny"
        decision = "allowed"
    return decision


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def by_sid(policy: str | dict[str, Any], sid: str) -> dict[str, Any]:
    document = json.loads(policy) if isinstance(policy, str) else policy
    found: list[dict[str, Any]] = [s for s in document["Statement"] if s.get("Sid") == sid]
    [statement] = found
    return statement


def _one(type_token: str, name: str) -> dict[str, Any]:
    matches = [inputs for n, inputs in registered(type_token) if n == name]
    assert len(matches) == 1, f"{type_token} {name}: {len(matches)} registered"
    return matches[0]


class Stack:
    def __init__(
        self, prefix: str, *, with_runner: bool = True, tss_reserved: int | None = None
    ) -> None:
        self.prefix = prefix
        # Resources named after the platform prefix (the tenant role, the break-glass role,
        # the scan role, the Cognito admin Lambda) must be unique to each test's stack.
        self.name_prefix = f"assay-{prefix}"
        self.network = Network(prefix, cfg=NET, region=REGION, account_id=ACCOUNT_ID)
        self.database = Database(
            f"{prefix}-db", cfg=DB, network=self.network, account_id=ACCOUNT_ID
        )
        self.alerts = Alerts(
            f"{prefix}-alarms", email=ALARM_EMAIL, region=REGION, account_id=ACCOUNT_ID
        )
        self.auth_events = AuthEvents(
            f"{prefix}-auth-events",
            repo_root=REPO_ROOT,
            region=REGION,
            account_id=ACCOUNT_ID,
            network=self.network,
            alarm_topic_arn=self.alerts.topic.arn,
        )
        self.auth = Auth(
            f"{prefix}-auth",
            cfg=AUTH,
            dns=DNS,
            region=REGION,
            account_id=ACCOUNT_ID,
            repo_root=REPO_ROOT,
            auth_event_lambda_arn=self.auth_events.trigger_function.arn,
            jwks_fetcher=lambda region, pool_id: FAKE_JWKS,
        )
        callers: dict[str, TssCaller] = {}
        if with_runner:
            callers[isolation.SERVICE_NAME] = TssCaller(
                role=runner_role(f"{prefix}-isolation", ACCOUNT_ID),
                access=("pipeline", "read", "upload"),
            )
        self.storage = TenantStorage(
            f"{prefix}-tss",
            repo_root=REPO_ROOT,
            region=REGION,
            account_id=ACCOUNT_ID,
            name_prefix=self.name_prefix,
            network=self.network,
            database=self.database,
            database_name=DB.database_name,
            break_glass_principal_arn=BREAK_GLASS_PRINCIPAL,
            alarm_topic_arn=self.alerts.topic.arn,
            callers=callers,
            reserved_concurrency=tss_reserved,
        )
        self.provisioning = Provisioning(
            f"{prefix}-provision",
            repo_root=REPO_ROOT,
            region=REGION,
            account_id=ACCOUNT_ID,
            name_prefix=self.name_prefix,
            network=self.network,
            database=self.database,
            database_name=DB.database_name,
            auth=self.auth,
            storage=self.storage,
            vector_dimensions=1024,
            default_tags=TAGS,
            default_models=DEFAULT_MODELS,
            upload_origins=["https://example.test"],
            platform_key_arns=[self.database.key.arn, self.alerts.key.arn],
            alarm_topic_arn=self.alerts.topic.arn,
        )
        self.cognito_admin_api = CognitoAdminApi(
            f"{self.name_prefix}-cognito-admin-api",
            image_uri=self.provisioning.invite_image.image_uri,
            auth=self.auth,
            account_id=ACCOUNT_ID,
        )
        self.lambda_endpoint_policy = self.provisioning.lambda_endpoint_access(
            [
                (
                    "ApiAndSeedManageTenantUsers",
                    self.cognito_admin_api.function.arn,
                    API_COGNITO_CALLERS,
                )
            ]
        )
        self.runner: IsolationRunner | None = None
        if with_runner:
            self.runner = IsolationRunner(
                f"{prefix}-isolation",
                role=callers[isolation.SERVICE_NAME].role,
                repo_root=REPO_ROOT,
                region=REGION,
                account_id=ACCOUNT_ID,
                name_prefix=self.name_prefix,
                network=self.network,
                database=self.database,
                database_name=DB.database_name,
                tss_endpoint_url=self.storage.endpoint_url,
                tenant_role_arn=self.storage.tenant_role.arn,
                invite_function_arn=self.provisioning.invite_function.arn,
                api_cognito_function_arn=self.cognito_admin_api.function.arn,
            )

    def ready(self) -> pulumi.Output[list[Any]]:
        """Resolves once everything the checks read is registered with the mocks."""
        outputs: list[pulumi.Output[Any]] = [
            self.storage.stage.id,
            self.storage.function.id,
            self.storage.endpoint_url,
            self.storage.break_glass.alarm.id,
            self.provisioning.state_machine.id,
            self.provisioning.state_machine.definition,
            self.provisioning.task_definition.container_definitions,
            self.provisioning.task_role_policy.policy,
            self.provisioning.failure_alarm.id,
            self.provisioning.invite_function.id,
            self.cognito_admin_api.function.id,
            self.cognito_admin_api.role_policy.policy,
            self.lambda_endpoint_policy.policy,
        ]
        if self.runner is not None:
            outputs.append(self.runner.function.id)
        return pulumi.Output.all(*outputs)

    def topic_arn(self) -> str:
        """The alarm topic's ARN, as the mocks build it."""
        return f"arn:aws:mock:{REGION}:{ACCOUNT_ID}:{self.prefix}-alarms-topic"

    def role_policies(self) -> dict[str, dict[str, Any]]:
        """Every inline role policy in this stack: resource name -> document."""
        return {
            name: json.loads(inputs["policy"])
            for name, inputs in registered("aws:iam/rolePolicy:RolePolicy")
            if name.startswith((self.prefix, self.name_prefix))
        }


# --- Config -----------------------------------------------------------------------------------


def tenancy(**changes: Any) -> TenancyConfig:
    values: dict[str, Any] = {
        "break_glass_principal_arn": BREAK_GLASS_PRINCIPAL,
        "vector_dimensions": 1024,
        "isolation_tests": False,
        "default_tags": TAGS,
    }
    return TenancyConfig(**(values | changes))


def test_tenancy_config_accepts_the_dev_values() -> None:
    assert validate_tenancy(tenancy()).vector_dimensions == 1024


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"break_glass_principal_arn": "arn:aws:iam::123:user/someone"}, "IAM role ARN"),
        ({"break_glass_principal_arn": "*"}, "IAM role ARN"),
        ({"vector_dimensions": 1536}, "vectorDimensions"),
        ({"default_tags": {}}, "defaultTags"),
    ],
)
def test_tenancy_config_rejects(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_tenancy(tenancy(**changes))


# --- The provisioning role: scoped, no object access, no decrypt on tenant keys ---------------


def provisioning_policy() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        provisioning.provisioning_role_policy(
            region=REGION,
            account_id=ACCOUNT_ID,
            name_prefix=PREFIX,
            tag_keys=["app", "env", "tenant_id"],
            platform_keys=PLATFORM_KEYS,
            state_bucket="arn:aws:s3:::state-bucket",
            state_key=f"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/state",
            scan_role=f"{ROLE}/scan",
            invite_function=f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:invite",
            db_user=f"arn:aws:rds-db:{REGION}:{ACCOUNT_ID}:dbuser:cluster/platform_writer",
        )
    )
    return document


# The only statements without a resource to scope to, and why each is safe.
UNSCOPED = {
    "CreateTenantKeys": "only a key created with a tenant_id tag",
    "ListForTheProvider": "list actions only",
    "CreateMalwareProtectionPlans": "a plan has no ARN before it exists",
}
# AWS KMS requires the permission to tag a key at creation "in an IAM policy that isn't
# restricted to particular KMS keys". An explicit deny keeps it off the platform's keys.
ANY_KEY = f"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/*"


def test_provisioning_role_is_scoped_to_tenant_resources_and_the_state_bucket() -> None:
    policy = provisioning_policy()
    for statement in policy["Statement"]:
        if statement["Effect"] != "Allow":
            continue
        resources = as_list(statement["Resource"])
        if "*" in resources:
            assert statement["Sid"] in UNSCOPED, statement["Sid"]
            continue
        if resources == [ANY_KEY]:
            assert statement["Sid"] == "TagKeysWhenCreatingThem"
            continue
        for resource in resources:
            scoped = (
                "assay-dev-t-*" in resource
                or "state" in resource
                or resource.endswith(("role/scan", "function:invite", "/platform_writer"))
                or ":malware-protection-plan/" in resource
            )
            assert scoped, f"{statement['Sid']}: {resource}"
    listing = by_sid(policy, "ListForTheProvider")
    assert all(a.split(":")[1].startswith("List") for a in as_list(listing["Action"]))


def test_provisioning_role_has_no_object_access_to_tenant_buckets() -> None:
    policy = provisioning_policy()
    bucket = "arn:aws:s3:::assay-dev-t-*"
    settings = by_sid(policy, "TenantBucketSettings")
    assert settings["Resource"] == bucket  # the bucket itself, never bucket/*
    for action in as_list(settings["Action"]):
        assert "Object" not in action.removeprefix("s3:GetBucketObjectLock")
        assert "Object" not in action or action.startswith("s3:PutBucket")
    allowed_on_objects = [
        s["Sid"]
        for s in policy["Statement"]
        if s["Effect"] == "Allow"
        and any(r.startswith(f"{bucket}/") for r in as_list(s["Resource"]))
    ]
    assert allowed_on_objects == []
    deny = by_sid(policy, "NeverTenantObjects")
    assert deny["Effect"] == "Deny"
    assert deny["Resource"] == [bucket, f"{bucket}/*"]
    assert set(deny["Action"]) == set(policies.OBJECT_DATA_ACTIONS)
    assert {"s3:GetObject*", "s3:PutObject*", "s3:DeleteObject*"} <= set(deny["Action"])


def test_provisioning_role_cannot_decrypt_with_a_tenant_key() -> None:
    policy = provisioning_policy()
    crypto = {"kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"}
    users = [
        s["Sid"]
        for s in policy["Statement"]
        if s["Effect"] == "Allow" and crypto & set(as_list(s["Action"]))
    ]
    assert users == ["StateKey"]
    assert by_sid(policy, "StateKey")["Resource"].endswith("key/state")
    deny = by_sid(policy, "NeverDecryptWithATenantKey")
    assert (deny["Effect"], deny["Action"], deny["Resource"]) == ("Deny", "kms:Decrypt", ANY_KEY)
    # The one exception: S3 Vectors decrypting on the role's behalf (creating an index).
    assert deny["Condition"] == {
        "Null": {"aws:ResourceTag/tenant_id": "false"},
        "StringNotEquals": {"kms:ViaService": f"s3vectors.{REGION}.amazonaws.com"},
    }
    other = by_sid(policy, "NeverReEncryptOrGrantWithATenantKey")
    assert other["Effect"] == "Deny"
    assert set(other["Action"]) == {"kms:ReEncrypt*", "kms:CreateGrant"}
    assert other["Condition"] == {"Null": {"aws:ResourceTag/tenant_id": "false"}}


S3_VECTORS = f"s3vectors.{REGION}.amazonaws.com"
S3 = f"s3.{REGION}.amazonaws.com"
ON_TENANT_KEY = {"aws:ResourceTag/tenant_id": "aaaaaaaa-0000-4000-8000-000000000001"}
A_TENANT_KEY = f"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/11111111-1111-4111-8111-111111111111"
A_TENANT_INDEX = (
    f"arn:aws:s3vectors:{REGION}:{ACCOUNT_ID}:bucket/assay-dev-t-aaaaaaaa-vectors/index/documents"
)


@pytest.mark.parametrize(
    ("what", "action", "context", "expected"),
    [
        ("decrypt directly", "kms:Decrypt", ON_TENANT_KEY, "explicitDeny"),
        (
            "decrypt through S3",
            "kms:Decrypt",
            {**ON_TENANT_KEY, "kms:ViaService": S3},
            "explicitDeny",
        ),
        # IAM says nothing; the tenant key's own policy allows it for S3 Vectors only.
        (
            "decrypt through S3 Vectors",
            "kms:Decrypt",
            {**ON_TENANT_KEY, "kms:ViaService": S3_VECTORS},
            "implicitDeny",
        ),
        (
            "re-encrypt through S3 Vectors",
            "kms:ReEncryptFrom",
            {**ON_TENANT_KEY, "kms:ViaService": S3_VECTORS},
            "explicitDeny",
        ),
        (
            "create a grant",
            "kms:CreateGrant",
            {**ON_TENANT_KEY, "kms:ViaService": S3_VECTORS},
            "explicitDeny",
        ),
    ],
)
def test_tenant_key_use_by_the_provisioning_role(
    what: str, action: str, context: dict[str, Any], expected: str
) -> None:
    assert decide(provisioning_policy(), action, A_TENANT_KEY, context) == expected, what


def test_provisioning_role_can_create_a_vector_index_but_read_no_vector() -> None:
    """Regression: the second provisioning run failed creating the index (2026-11-23).

    S3 Vectors decrypts with the tenant key, on the caller's behalf, to create an index. The
    role is allowed exactly that, and still has no way to read what the key protects: no
    vector data action in its own policy, every one denied by the vector bucket's policy,
    and no object access at all.
    """
    tenant_id = "aaaaaaaa-0000-4000-8000-000000000001"
    principals = policies.Principals(
        tenant_role_arn=f"{ROLE}/tenant",
        break_glass_role_arn=f"{ROLE}/break-glass",
        provisioning_role_arn=f"{ROLE}/provisioning",
        scan_role_arn=f"{ROLE}/scan",
    )
    key_policy = policies.tenant_key_policy(
        region=REGION,
        account_id=ACCOUNT_ID,
        tenant_id=tenant_id,
        vector_bucket="assay-dev-t-aaaaaaaa-vectors",
        principals=principals,
    )
    use = by_sid(key_policy, "ProvisioningCreatesVectorBucketAndIndex")
    assert "kms:Decrypt" in use["Action"]
    assert use["Condition"] == {"StringEquals": {"kms:ViaService": S3_VECTORS}}

    role_policy = provisioning_policy()
    through_vectors = {**ON_TENANT_KEY, "kms:ViaService": S3_VECTORS}
    assert decide(role_policy, "kms:Decrypt", A_TENANT_KEY, through_vectors) != "explicitDeny"
    assert decide(role_policy, "s3vectors:CreateIndex", A_TENANT_INDEX, {}) == "allowed"

    for action in policies.VECTOR_DATA_ACTIONS:
        assert decide(role_policy, action, A_TENANT_INDEX, {}) == "implicitDeny", action
    vector_bucket_policy = policies.tenant_vector_bucket_policy(
        region=REGION,
        account_id=ACCOUNT_ID,
        vector_bucket="assay-dev-t-aaaaaaaa-vectors",
        tenant_id=tenant_id,
        principals=principals,
    )
    denied = by_sid(vector_bucket_policy, "ProvisioningRoleNeverTouchesVectors")
    assert set(denied["Action"]) == set(policies.VECTOR_DATA_ACTIONS)
    tenant_object = "arn:aws:s3:::assay-dev-t-aaaaaaaa/originals/x.pdf"
    assert decide(role_policy, "s3:GetObject", tenant_object, {}) == "explicitDeny"


def test_provisioning_role_creates_only_keys_tagged_for_a_tenant() -> None:
    create = by_sid(provisioning_policy(), "CreateTenantKeys")
    assert create["Action"] == "kms:CreateKey"
    assert create["Condition"] == {"Null": {"aws:RequestTag/tenant_id": "false"}}


def test_tagging_a_key_at_creation_does_not_depend_on_request_or_resource_tags() -> None:
    """Regression: the first real provisioning run failed here (2026-11-23).

    The role could create a key but was refused `kms:TagResource` for the tags sent with
    CreateKey, because that grant was conditioned on `aws:RequestTag` and `aws:ResourceTag`.
    AWS KMS requires the permission "in an IAM policy that isn't restricted to particular
    KMS keys". So the grant is on every key and conditioned only on the request's tag keys.
    """
    policy = provisioning_policy()
    taggers = [
        s
        for s in policy["Statement"]
        if s["Effect"] == "Allow" and "kms:TagResource" in as_list(s["Action"])
    ]
    [grant] = taggers
    assert grant["Sid"] == "TagKeysWhenCreatingThem"
    assert grant["Action"] == "kms:TagResource"
    assert grant["Resource"] == ANY_KEY
    assert grant["Condition"] == {
        "ForAllValues:StringEquals": {"aws:TagKeys": ["app", "env", "tenant_id"]}
    }
    assert "aws:RequestTag" not in json.dumps(grant)
    assert "aws:ResourceTag" not in json.dumps(grant)


@pytest.mark.parametrize(
    "context",
    [
        # Whichever tag keys KMS puts in the request context when a key is created with
        # tags, the grant must hold. The failed policy held in none of these.
        {},
        {"aws:RequestTag/tenant_id": "t", "aws:RequestTag/app": "a", "aws:RequestTag/env": "e"},
        {"aws:ResourceTag/tenant_id": "t", "aws:ResourceTag/app": "a", "aws:ResourceTag/env": "e"},
        {
            "aws:RequestTag/tenant_id": "t",
            "aws:RequestTag/app": "a",
            "aws:ResourceTag/tenant_id": "t",
            "aws:ResourceTag/app": "a",
        },
    ],
)
def test_tag_grant_holds_in_every_creation_context(context: dict[str, str]) -> None:
    grant = by_sid(provisioning_policy(), "TagKeysWhenCreatingThem")
    request = {**context, "aws:TagKeys": ["app", "env", "tenant_id"]}
    assert conditions_hold(grant["Condition"], request)
    # A tag key the tenant stack never sets is still refused.
    assert not conditions_hold(grant["Condition"], {**request, "aws:TagKeys": ["owner"]})


def test_tag_grant_covers_exactly_the_tags_a_tenant_key_is_created_with() -> None:
    """The provider's default tags plus tenant_id (assay_tss.provision.program)."""
    grant = by_sid(provisioning_policy(), "TagKeysWhenCreatingThem")
    allowed = grant["Condition"]["ForAllValues:StringEquals"]["aws:TagKeys"]
    assert set(allowed) == {*TAGS, policies.TENANT_TAG}


def test_provisioning_role_never_tags_a_platform_key() -> None:
    policy = provisioning_policy()
    deny = by_sid(policy, "NeverTagPlatformKeys")
    assert deny["Effect"] == "Deny" and "Condition" not in deny
    assert set(deny["Action"]) == {"kms:TagResource", "kms:UntagResource"}
    assert deny["Resource"] == sorted(PLATFORM_KEYS)
    # And nothing in IAM lets it untag any key; a tenant key's own policy does that.
    untaggers = [
        s["Sid"]
        for s in policy["Statement"]
        if s["Effect"] == "Allow" and "kms:UntagResource" in as_list(s["Action"])
    ]
    assert untaggers == []


# --- tenant_id on a KMS key is write-once -------------------------------------------------

TENANT_KEY = f"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/11111111-1111-4111-8111-111111111111"
TENANT_A_ID = "aaaaaaaa-0000-4000-8000-000000000001"
TENANT_B_ID = "bbbbbbbb-0000-4000-8000-000000000002"
NEW_KEY_TAGS = {"app": "assay-insights", "env": "dev", "tenant_id": TENANT_A_ID}


def request_tags(**tags: str) -> dict[str, Any]:
    return {f"aws:RequestTag/{key}": value for key, value in tags.items()}


def resource_tags(**tags: str) -> dict[str, Any]:
    return {f"aws:ResourceTag/{key}": value for key, value in tags.items()}


def tag_request(*keys: str, **context: Any) -> dict[str, Any]:
    return {"aws:TagKeys": list(keys), **context}


EXISTING_TENANT_KEY = resource_tags(**NEW_KEY_TAGS)
# What KMS might put in the request context for the tags sent with CreateKey. Which of
# these it is has not been observed; the policy must allow all four.
CREATION_CONTEXTS = {
    "tag keys only": {},
    "request tags": request_tags(**NEW_KEY_TAGS),
    "new tags shown as resource tags": resource_tags(**NEW_KEY_TAGS),
    "request and resource tags": {**request_tags(**NEW_KEY_TAGS), **resource_tags(**NEW_KEY_TAGS)},
}


def test_write_once_statements_are_as_designed() -> None:
    policy = provisioning_policy()
    change = by_sid(policy, "TenantIdTagNeverChanges")
    assert (change["Effect"], change["Action"], change["Resource"]) == (
        "Deny",
        "kms:TagResource",
        ANY_KEY,
    )
    # All three must hold: the key has a tenant_id, the request sets one, and they differ.
    assert change["Condition"] == {
        "Null": {"aws:ResourceTag/tenant_id": "false", "aws:RequestTag/tenant_id": "false"},
        "StringNotEquals": {"aws:RequestTag/tenant_id": "${aws:ResourceTag/tenant_id}"},
    }
    remove = by_sid(policy, "TenantIdTagIsNeverRemoved")
    assert (remove["Effect"], remove["Action"], remove["Resource"]) == (
        "Deny",
        "kms:UntagResource",
        ANY_KEY,
    )
    assert remove["Condition"] == {"ForAnyValue:StringEquals": {"aws:TagKeys": ["tenant_id"]}}


@pytest.mark.parametrize("name", list(CREATION_CONTEXTS))
def test_creating_a_tenant_key_with_its_tags_is_allowed_in_every_context(name: str) -> None:
    """The write-once deny must never apply while a key is being created.

    A deny on "the key has a tenant_id and the request touches tenant_id" would refuse
    creation in the two contexts where the new key's tags appear as resource tags.
    """
    policy = provisioning_policy()
    context = CREATION_CONTEXTS[name]
    tagging = tag_request("app", "env", "tenant_id", **context)
    assert decide(policy, "kms:TagResource", TENANT_KEY, tagging) == "allowed"
    creating = tag_request("app", "env", "tenant_id", **request_tags(**NEW_KEY_TAGS))
    assert decide(policy, "kms:CreateKey", "*", creating) == "allowed"


@pytest.mark.parametrize(
    ("what", "action", "context", "expected"),
    [
        (
            "change tenant_id on an existing tenant key",
            "kms:TagResource",
            tag_request("tenant_id", **request_tags(tenant_id=TENANT_B_ID), **EXISTING_TENANT_KEY),
            "explicitDeny",
        ),
        (
            "change tenant_id together with app",
            "kms:TagResource",
            tag_request(
                "app",
                "tenant_id",
                **request_tags(app="x", tenant_id=TENANT_B_ID),
                **EXISTING_TENANT_KEY,
            ),
            "explicitDeny",
        ),
        (
            "set tenant_id to the value it already has",
            "kms:TagResource",
            tag_request("tenant_id", **request_tags(tenant_id=TENANT_A_ID), **EXISTING_TENANT_KEY),
            "allowed",
        ),
        (
            "untag tenant_id",
            "kms:UntagResource",
            tag_request("tenant_id", **EXISTING_TENANT_KEY),
            "explicitDeny",
        ),
        (
            "untag tenant_id together with app",
            "kms:UntagResource",
            tag_request("app", "tenant_id", **EXISTING_TENANT_KEY),
            "explicitDeny",
        ),
        (
            "change app on a tenant key",
            "kms:TagResource",
            tag_request("app", **request_tags(app="renamed"), **EXISTING_TENANT_KEY),
            "allowed",
        ),
        (
            "change env on a tenant key",
            "kms:TagResource",
            tag_request("env", **request_tags(env="prod"), **EXISTING_TENANT_KEY),
            "allowed",
        ),
        (
            # IAM says nothing here; a tenant key's own policy lets this role untag app or env.
            "untag app from a tenant key",
            "kms:UntagResource",
            tag_request("app", **EXISTING_TENANT_KEY),
            "implicitDeny",
        ),
        (
            "add any other tag key",
            "kms:TagResource",
            tag_request("owner", **request_tags(owner="x"), **EXISTING_TENANT_KEY),
            "implicitDeny",
        ),
        (
            "create a key with no tenant_id tag",
            "kms:CreateKey",
            tag_request("app", "env", **request_tags(app="x", env="y")),
            "implicitDeny",
        ),
    ],
)
def test_tenant_id_on_a_key_is_write_once(
    what: str, action: str, context: dict[str, Any], expected: str
) -> None:
    resource = "*" if action == "kms:CreateKey" else TENANT_KEY
    assert decide(provisioning_policy(), action, resource, context) == expected, what


@pytest.mark.parametrize("key", PLATFORM_KEYS)
def test_platform_keys_cannot_be_tagged_or_untagged_at_all(key: str) -> None:
    policy = provisioning_policy()
    on_platform = resource_tags(app="assay-insights", env="dev")
    for action, request in (
        ("kms:TagResource", tag_request("tenant_id", **request_tags(tenant_id=TENANT_A_ID))),
        ("kms:TagResource", tag_request("app", **request_tags(app="x"))),
        ("kms:UntagResource", tag_request("app")),
        ("kms:UntagResource", tag_request("tenant_id")),
    ):
        assert decide(policy, action, key, {**request, **on_platform}) == "explicitDeny"


def test_provisioning_policy_needs_the_platform_keys() -> None:
    with pytest.raises(ValueError, match="platform_keys"):
        provisioning.provisioning_role_policy(
            region=REGION,
            account_id=ACCOUNT_ID,
            name_prefix=PREFIX,
            tag_keys=["app", "env", "tenant_id"],
            platform_keys=[],
            state_bucket="arn:aws:s3:::state-bucket",
            state_key=f"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/state",
            scan_role=f"{ROLE}/scan",
            invite_function="arn:fn",
            db_user="arn:dbuser",
        )


@pulumi.runtime.test
def test_every_platform_key_is_on_the_provisioning_roles_deny_list() -> Any:
    stack = Stack("k1")

    def check(_: list[Any]) -> None:
        keys = sorted(
            f"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/{name}-key-id"
            for name, _ in registered("aws:kms/key:Key", "k1-")
        )
        assert len(keys) == 3  # database, alarm topic, tenant stack state
        policy = _one("aws:iam/rolePolicy:RolePolicy", "k1-provision-task-policy")["policy"]
        assert by_sid(policy, "NeverTagPlatformKeys")["Resource"] == keys
        allowed = by_sid(policy, "TagKeysWhenCreatingThem")["Condition"]
        assert allowed == {
            "ForAllValues:StringEquals": {"aws:TagKeys": ["app", "env", "tenant_id"]}
        }

    return stack.ready().apply(check)


def test_provisioning_role_passes_only_the_scan_role_and_only_to_guardduty() -> None:
    statement = by_sid(provisioning_policy(), "PassTheScanRoleToGuardDuty")
    assert statement["Resource"] == f"{ROLE}/scan"
    assert statement["Condition"] == {
        "StringEquals": {"iam:PassedToService": "malware-protection-plan.guardduty.amazonaws.com"}
    }
    iam_actions = {
        a
        for s in provisioning_policy()["Statement"]
        for a in as_list(s["Action"])
        if a.startswith("iam:")
    }
    assert iam_actions == {"iam:PassRole"}


# --- The scan role, the state bucket and the state key ----------------------------------------


def test_scan_role_trust_requires_this_account_as_source() -> None:
    trust = json.loads(provisioning.scan_role_trust_policy(ACCOUNT_ID))
    [statement] = trust["Statement"]
    assert statement["Principal"] == {"Service": "malware-protection-plan.guardduty.amazonaws.com"}
    assert statement["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCOUNT_ID}}


def test_only_the_api_gateway_logging_role_admits_a_call_with_no_source_account() -> None:
    """The one role whose trust is `IfExists` (proven necessary on 2026-11-25)."""
    trust = json.loads(tss.api_logs_trust_policy(ACCOUNT_ID))
    [statement] = trust["Statement"]
    assert statement["Effect"] == "Allow"
    assert statement["Principal"] == {"Service": "apigateway.amazonaws.com"}
    assert statement["Action"] == "sts:AssumeRole"
    # Another account's ID is still refused; only a call with none is admitted.
    assert statement["Condition"] == {"StringEqualsIfExists": {"aws:SourceAccount": ACCOUNT_ID}}

    [other] = json.loads(tss._service_trust("lambda.amazonaws.com", ACCOUNT_ID))["Statement"]
    assert other["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCOUNT_ID}}

    source = (REPO_ROOT / "infra").rglob("*.py")
    users = [p.name for p in source if "IfExists" in p.read_text(encoding="utf-8")]
    assert users == ["tss.py"]
    assert (REPO_ROOT / "infra/assay_infra/tss.py").read_text(encoding="utf-8").count(
        "IfExists"
    ) == 1


def test_scan_role_reads_and_tags_tenant_buckets_only() -> None:
    policy = json.loads(
        provisioning.scan_role_policy(region=REGION, account_id=ACCOUNT_ID, name_prefix=PREFIX)
    )
    for statement in policy["Statement"]:
        for resource in as_list(statement["Resource"]):
            assert (
                "assay-dev-t-*" in resource
                or ":rule/DO-NOT-DELETE-" in resource
                or ":key/" in resource
            )
    actions = {a for s in policy["Statement"] for a in as_list(s["Action"])}
    assert not actions & {"s3:DeleteObject", "s3:PutBucketPolicy", "kms:TagResource"}
    put = by_sid(policy, "AllowPutValidationObject")
    assert put["Resource"].endswith("/malware-protection-resource-validation-object")
    key = by_sid(policy, "AllowDecryptForMalwareScan")
    assert key["Condition"]["Null"] == {"aws:ResourceTag/tenant_id": "false"}


def test_state_objects_are_readable_only_by_the_provisioning_role() -> None:
    policy = json.loads(
        provisioning.state_bucket_policy(
            bucket="arn:aws:s3:::state-bucket", provisioning_role=f"{ROLE}/provisioning"
        )
    )
    deny = by_sid(policy, "OnlyProvisioningReadsOrWritesState")
    assert deny["Effect"] == "Deny" and deny["Principal"] == "*"
    assert {"s3:GetObject*", "s3:PutObject*", "s3:DeleteObject*"} <= set(deny["Action"])
    assert deny["Condition"] == {"ArnNotEquals": {"aws:PrincipalArn": f"{ROLE}/provisioning"}}
    assert by_sid(policy, "DenyInsecureTransport")["Effect"] == "Deny"


def test_state_key_is_used_only_by_the_provisioning_role() -> None:
    policy = json.loads(
        provisioning.state_key_policy(
            account_id=ACCOUNT_ID, provisioning_role=f"{ROLE}/provisioning"
        )
    )
    crypto = {"kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:CreateGrant", "kms:*"}
    account = by_sid(policy, "AccountAdministersWithoutUsing")
    assert not crypto & set(account["Action"])
    use = by_sid(policy, "OnlyProvisioningUses")
    assert use["Principal"] == {"AWS": f"{ROLE}/provisioning"}
    assert {"kms:Decrypt", "kms:GenerateDataKey"} <= set(use["Action"])


# --- The Cognito admin Lambda: only the provisioning role -------------------------------------


def test_lambda_endpoint_allows_named_functions_for_named_callers_only() -> None:
    policy = json.loads(
        provisioning.lambda_endpoint_policy(
            invite_function="arn:fn",
            provisioning_role=f"{ROLE}/provisioning",
            grants=[("ApiManagesUsers", "arn:api-fn", [f"{ROLE}/seed", f"{ROLE}/api"])],
        )
    )
    invite, api = policy["Statement"]
    assert invite["Action"] == "lambda:InvokeFunction"
    assert invite["Resource"] == "arn:fn"
    assert invite["Condition"] == {"ArnEquals": {"aws:PrincipalArn": f"{ROLE}/provisioning"}}
    assert api["Sid"] == "ApiManagesUsers"
    assert api["Resource"] == "arn:api-fn"
    assert api["Condition"] == {"ArnEquals": {"aws:PrincipalArn": [f"{ROLE}/api", f"{ROLE}/seed"]}}
    assert {s["Effect"] for s in policy["Statement"]} == {"Allow"}


def test_one_caller_is_written_as_a_string() -> None:
    """As IAM stores a one-item list, so an unchanged stack previews no difference."""
    policy = json.loads(
        provisioning.lambda_endpoint_policy(
            invite_function="arn:fn",
            provisioning_role=f"{ROLE}/provisioning",
            grants=[("One", "arn:api-fn", [f"{ROLE}/api"])],
        )
    )
    assert policy["Statement"][1]["Condition"] == {"ArnEquals": {"aws:PrincipalArn": f"{ROLE}/api"}}


@pulumi.runtime.test
def test_only_the_provisioning_role_may_invoke_the_cognito_admin_lambda() -> Any:
    stack = Stack("p1")

    def check(_: list[Any]) -> None:
        function = _one("aws:lambda/function:Function", f"{stack.name_prefix}-cognito-admin-fn")
        assert "vpcConfig" not in function  # outside the VPC: it must reach Cognito
        assert function["architectures"] == ["x86_64"]
        assert function["environment"]["variables"]["OPERATIONS"] == "invite"
        assert set(function["environment"]["variables"]) == {"TENANT_POOL_ID", "OPERATIONS"}

        invokers = sorted(
            name
            for name, document in stack.role_policies().items()
            for statement in document["Statement"]
            if statement["Effect"] == "Allow"
            and "lambda:InvokeFunction" in as_list(statement["Action"])
        )
        assert invokers == ["p1-provision-task-policy"]

        # No resource-based permission: IAM is the only way in.
        permissions = [
            inputs
            for _, inputs in registered("aws:lambda/permission:Permission")
            if "cognito-admin" in str(inputs["function"])
        ]
        assert permissions == []

        cognito = by_sid(
            _one("aws:iam/rolePolicy:RolePolicy", f"{stack.name_prefix}-cognito-admin-policy")[
                "policy"
            ],
            "InviteTenantPoolUsers",
        )
        assert set(cognito["Action"]) == {
            "cognito-idp:AdminCreateUser",
            "cognito-idp:AdminGetUser",
            "cognito-idp:AdminAddUserToGroup",
        }
        assert "tenant" in cognito["Resource"]

        # Same name and parent as in build step 4: the same resource, never replaced (a
        # deleted endpoint policy resets the endpoint to allow everything).
        endpoint_policy = _one(
            "aws:ec2/vpcEndpointPolicy:VpcEndpointPolicy", "p1-provision-lambda-endpoint-policy"
        )
        assert endpoint_policy["vpcEndpointId"] == "p1-lambda-id"
        statements = json.loads(endpoint_policy["policy"])["Statement"]
        assert [s["Sid"] for s in statements] == [
            "ProvisioningInvitesTheFirstAdmin",
            "ApiAndSeedManageTenantUsers",
        ]
        assert statements[0]["Resource"].endswith(f"{stack.name_prefix}-cognito-admin-fn")
        assert statements[1]["Resource"].endswith(f"{stack.name_prefix}-cognito-admin-api-fn")
        assert statements[1]["Condition"]["ArnEquals"]["aws:PrincipalArn"] == sorted(
            API_COGNITO_CALLERS
        )

    return stack.ready().apply(check)


@pulumi.runtime.test
def test_the_apis_cognito_admin_function() -> Any:
    """Its own role, its own operations; platform pool for MFA resets only (build step 5)."""
    stack = Stack("p6")

    def check(_: list[Any]) -> None:
        name = f"{stack.name_prefix}-cognito-admin-api"
        function = _one("aws:lambda/function:Function", f"{name}-fn")
        assert "vpcConfig" not in function
        variables = function["environment"]["variables"]
        assert set(variables["OPERATIONS"].split(",")) == {
            "invite",
            "resend_invite",
            "reset_password",
            "disable",
            "enable",
            "set_role",
            "reset_mfa",
            "mfa_status",
        }
        assert set(variables) == {"TENANT_POOL_ID", "PLATFORM_POOL_ID", "OPERATIONS"}
        # The same image as the provisioning function.
        invite = _one("aws:lambda/function:Function", f"{stack.name_prefix}-cognito-admin-fn")
        assert function["imageUri"] == invite["imageUri"]
        permissions = [
            inputs
            for _, inputs in registered("aws:lambda/permission:Permission")
            if name in str(inputs["function"])
        ]
        assert permissions == []

        policy = _one("aws:iam/rolePolicy:RolePolicy", f"{name}-policy")["policy"]
        tenant = by_sid(policy, "TenantPoolUserAdministration")
        assert "p6-auth-tenant-pool" in tenant["Resource"]
        assert "cognito-idp:AdminDeleteSoftwareToken" in tenant["Action"]
        assert "cognito-idp:AdminSetUserMFAPreference" not in tenant["Action"]
        platform = by_sid(policy, "PlatformPoolMfaResetOnly")
        assert "p6-auth-platform-pool" in platform["Resource"]
        assert set(platform["Action"]) == {
            "cognito-idp:AdminGetUser",
            "cognito-idp:AdminListGroupsForUser",
            "cognito-idp:AdminDeleteSoftwareToken",
            "cognito-idp:AdminUserGlobalSignOut",
        }
        statements = json.loads(policy)["Statement"]
        assert {s["Effect"] for s in statements} == {"Allow"}
        assert not any("*" in str(s["Action"]) for s in statements)

    return stack.ready().apply(check)


# --- The tenant role and the TSS --------------------------------------------------------------


@pulumi.runtime.test
def test_tenant_role_trusts_only_the_tss_and_uses_the_one_template() -> Any:
    stack = Stack("t1")

    def check(_: list[Any]) -> None:
        role = _one("aws:iam/role:Role", f"{stack.name_prefix}-tenant-role")
        assert role["name"] == f"{stack.name_prefix}-tenant"
        assert role["maxSessionDuration"] == 3600
        trust = json.loads(role["assumeRolePolicy"])
        tss_role = f"{ROLE}/t1-tss-role-a1b2c3d"
        assert trust == policies.tenant_role_trust_policy(
            tss_role_arn=tss_role, name_prefix=stack.name_prefix
        )
        assert all(s["Principal"] == {"AWS": tss_role} for s in trust["Statement"])
        assert [s["Action"] for s in trust["Statement"]] == [
            "sts:AssumeRole",
            "sts:TagSession",
            "sts:SetSourceIdentity",
        ]

        policy = _one("aws:iam/rolePolicy:RolePolicy", f"{stack.name_prefix}-tenant-policy")
        assert json.loads(policy["policy"]) == policies.tenant_role_policy(
            region=REGION, account_id=ACCOUNT_ID
        )

        # Only the TSS's own policy lets anything assume the tenant role (the isolation
        # runner's probe aside, which the trust policy refuses).
        assumers = sorted(
            name
            for name, document in stack.role_policies().items()
            for s in document["Statement"]
            if s["Effect"] == "Allow" and "sts:AssumeRole" in as_list(s["Action"])
        )
        assert assumers == ["t1-isolation-policy", "t1-tss-role-policy"]

    return stack.ready().apply(check)


@pulumi.runtime.test
def test_the_access_logs_role_keeps_the_trust_that_lets_api_gateway_write() -> Any:
    """With `StringEquals` the access log received nothing for two days (2026-11-25):
    API Gateway sends no source account when it assumes the role to write a log entry.
    Do not "tidy" this role back to the form every other service role has."""
    stack = Stack("t1a")

    def check(_: list[Any]) -> None:
        role = _one("aws:iam/role:Role", "t1a-tss-api-logs-role")
        assert json.loads(role["assumeRolePolicy"]) == {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "apigateway.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                    "Condition": {"StringEqualsIfExists": {"aws:SourceAccount": ACCOUNT_ID}},
                }
            ],
        }
        # It is the role the account setting names, and it has the managed policy alone.
        attachment = _one(
            "aws:iam/rolePolicyAttachment:RolePolicyAttachment", "t1a-tss-api-logs-policy"
        )
        assert attachment["policyArn"] == tss.API_GATEWAY_LOGS_POLICY
        assert _one("aws:apigateway/account:Account", "t1a-tss-api-account")
        # No other role in the stack has the wider form.
        wider = [
            name
            for name, inputs in registered("aws:iam/role:Role", "t1a-")
            if "IfExists" in inputs["assumeRolePolicy"]
        ]
        assert wider == ["t1a-tss-api-logs-role"]

    return stack.ready().apply(check)


@pulumi.runtime.test
def test_tss_lambda_runs_in_the_vpc_as_database_role_tss() -> Any:
    stack = Stack("t2")

    def check(_: list[Any]) -> None:
        function = _one("aws:lambda/function:Function", "t2-tss-fn")
        assert function["architectures"] == ["x86_64"]
        assert function["packageType"] == "Image"
        assert function["timeout"] == 29
        assert function["vpcConfig"]["subnetIds"] == ["t2-app-use1-az2-id"]
        variables = function["environment"]["variables"]
        assert variables["NAME_PREFIX"] == stack.name_prefix
        assert variables["TENANT_ROLE_ARN"] == f"{ROLE}/{stack.name_prefix}-tenant"
        assert json.loads(variables["TSS_CALLERS"]) == {
            "t2-isolation-role-a1b2c3d": {
                "service": "isolation",
                "access": ["pipeline", "read", "upload"],
            }
        }
        assert variables["AWS_STS_REGIONAL_ENDPOINTS"] == "regional"

        policy = _one("aws:iam/rolePolicy:RolePolicy", "t2-tss-role-policy")["policy"]
        assert by_sid(policy, "ConnectAsTss")["Resource"].endswith("/tss")
        assume = by_sid(policy, "AssumeTheTenantRole")
        assert assume["Resource"] == f"{ROLE}/{stack.name_prefix}-tenant"
        document = json.loads(policy)
        services = {a.split(":")[0] for s in document["Statement"] for a in as_list(s["Action"])}
        assert services == {"logs", "ec2", "rds-db", "sts"}  # no S3, no KMS: it holds no data

    return stack.ready().apply(check)


@pulumi.runtime.test
def test_the_tss_keeps_a_share_of_the_accounts_concurrency_when_the_stack_says_so() -> Any:
    """Owner decision, 2026-11-25. On that day scan functions held every slot the account
    had, each waiting for a TSS call that could not get one. A reservation is capacity no
    other function can take (and the most the TSS will use)."""
    reserved = Stack("t2r", tss_reserved=300)
    plain = Stack("t2u")

    def check(_: list[Any]) -> None:
        key = "reservedConcurrentExecutions"
        assert _one("aws:lambda/function:Function", "t2r-tss-fn")[key] == 300
        assert _one("aws:lambda/function:Function", "t2u-tss-fn").get(key) is None

    return pulumi.Output.all(reserved.ready(), plain.ready()).apply(check)


@pulumi.runtime.test
def test_tss_api_is_private_with_iam_authorization() -> Any:
    stack = Stack("t3")

    def check(resolved: list[Any]) -> None:
        api = _one("aws:apigateway/restApi:RestApi", "t3-tss-api")
        assert api["endpointConfiguration"] == {
            "types": "PRIVATE",
            "vpcEndpointIds": ["t3-execute-api-id"],
        }
        # The policy is its own resource, written as API Gateway stores it. Given as the
        # API's `policy` argument in shorthand, it differed on every preview.
        assert "policy" not in api
        api_arn = f"arn:aws:execute-api:{REGION}:{ACCOUNT_ID}:t3-tss-api-id"
        attached = _one("aws:apigateway/restApiPolicy:RestApiPolicy", "t3-tss-api-policy")
        assert attached["restApiId"] == "t3-tss-api-id"
        assert tss.THIS_API + "/" not in attached["policy"]
        policy = json.loads(attached["policy"])
        deny = by_sid(policy, "OnlyThroughTheVpcEndpoint")
        assert deny["Effect"] == "Deny" and deny["Principal"] == "*"
        assert deny["Action"] == "execute-api:Invoke"
        assert deny["Resource"] == f"{api_arn}/*"
        assert deny["Condition"] == {"StringNotEquals": {"aws:SourceVpce": "t3-execute-api-id"}}
        allow = by_sid(policy, "KnownCallers")
        # One caller: a string, as AWS stores it, not a one-item list.
        assert allow["Principal"] == {"AWS": f"{ROLE}/t3-isolation-role-a1b2c3d"}
        assert allow["Resource"] == f"{api_arn}/v1/POST/resolve"
        assert [s["Sid"] for s in policy["Statement"]] == [
            "OnlyThroughTheVpcEndpoint",
            "KnownCallers",
        ]

        # The redeploy fingerprint is taken from the portable form, so it does not depend
        # on the API's own ARN.
        portable = tss.api_resource_policy(
            vpc_endpoint_id="t3-execute-api-id", callers=[f"{ROLE}/t3-isolation-role-a1b2c3d"]
        )
        deployment = _one("aws:apigateway/deployment:Deployment", "t3-tss-api-deployment")
        parts = [
            "t3-tss-api-resolve-id",
            "t3-tss-api-resolve-post-id",
            "t3-tss-api-resolve-lambda-id",
            _one("aws:apigateway/integration:Integration", "t3-tss-api-resolve-lambda")["uri"],
            portable,
            # The worker task's route (build step 6, second half).
            "t3-tss-api-resolve-worker-id",
            "t3-tss-api-resolve-worker-post-id",
            "t3-tss-api-resolve-worker-lambda-id",
        ]
        expected = hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()
        assert deployment["triggers"] == {"redeploy": expected}

        method = _one("aws:apigateway/method:Method", "t3-tss-api-resolve-post")
        assert (method["httpMethod"], method["authorization"]) == ("POST", "AWS_IAM")
        integration = _one("aws:apigateway/integration:Integration", "t3-tss-api-resolve-lambda")
        assert integration["type"] == "AWS_PROXY"
        stage = _one("aws:apigateway/stage:Stage", "t3-tss-api-stage")
        assert stage["stageName"] == "v1"
        assert "userArn" in stage["accessLogSettings"]["format"]

        [permission] = [
            inputs
            for name, inputs in registered("aws:lambda/permission:Permission")
            if name == "t3-tss-api-invoke"
        ]
        assert permission["principal"] == "apigateway.amazonaws.com"
        assert permission["sourceArn"].endswith("/*/POST/resolve")

        # The worker task's route: the same authorization, its own method and permission.
        worker = _one("aws:apigateway/method:Method", "t3-tss-api-resolve-worker-post")
        assert (worker["httpMethod"], worker["authorization"]) == ("POST", "AWS_IAM")
        assert (
            _one("aws:apigateway/resource:Resource", "t3-tss-api-resolve-worker")["pathPart"]
            == "resolve-worker"
        )
        [worker_permission] = [
            inputs
            for name, inputs in registered("aws:lambda/permission:Permission")
            if name == "t3-tss-api-invoke-worker"
        ]
        assert worker_permission["sourceArn"].endswith("/*/POST/resolve-worker")
        # With no worker caller in this stack, no one is admitted to it.
        assert "resolve-worker" not in attached["policy"]

        caller = _one("aws:iam/rolePolicy:RolePolicy", "t3-tss-caller-isolation")["policy"]
        [statement] = json.loads(caller)["Statement"]
        assert statement["Action"] == "execute-api:Invoke"
        assert statement["Resource"].endswith("/v1/POST/resolve")
        assert resolved[2] == f"https://t3-tss-api-id.execute-api.{REGION}.amazonaws.com/v1"

    return stack.ready().apply(check)


@pulumi.runtime.test
def test_without_the_isolation_runner_the_tss_has_no_caller() -> Any:
    stack = Stack("t4", with_runner=False)

    def check(_: list[Any]) -> None:
        function = _one("aws:lambda/function:Function", "t4-tss-fn")
        assert function["environment"]["variables"]["TSS_CALLERS"] == "{}"
        attached = _one("aws:apigateway/restApiPolicy:RestApiPolicy", "t4-tss-api-policy")
        policy = json.loads(attached["policy"])
        assert [s["Effect"] for s in policy["Statement"]] == ["Deny"]
        assert registered("aws:lambda/function:Function", "t4-isolation") == []
        assert "t4-isolation-policy" not in stack.role_policies()

    return stack.ready().apply(check)


# --- Break glass ------------------------------------------------------------------------------


@pulumi.runtime.test
def test_break_glass_role_is_assumable_by_one_principal_and_alarmed() -> Any:
    stack = Stack("b1")

    def check(_: list[Any]) -> None:
        name = f"{stack.name_prefix}-break-glass"
        role = _one("aws:iam/role:Role", f"{name}-role")
        assert role["name"] == name
        [statement] = json.loads(role["assumeRolePolicy"])["Statement"]
        assert statement["Principal"] == {"AWS": BREAK_GLASS_PRINCIPAL}
        assert statement["Action"] == "sts:AssumeRole"

        policy = json.loads(_one("aws:iam/rolePolicy:RolePolicy", f"{name}-policy")["policy"])
        actions = {a for s in policy["Statement"] for a in as_list(s["Action"])}
        assert "s3:GetObject" in actions
        assert not actions & {"s3:PutObject", "s3:DeleteObject", "s3:PutBucketTagging"}
        assert all(
            f"{stack.name_prefix}-t-*" in r
            for s in policy["Statement"]
            for r in as_list(s["Resource"])
        )

        rule = _one("aws:cloudwatch/eventRule:EventRule", f"{name}-assumed")
        # AssumeRole is a read-only management event; a rule sees it only in this state.
        assert rule["state"] == "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS"
        pattern = json.loads(rule["eventPattern"])
        assert pattern["detail"]["eventName"] == ["AssumeRole"]
        assert pattern["detail"]["requestParameters"] == {"roleArn": [f"{ROLE}/{name}"]}

        target = _one("aws:cloudwatch/eventTarget:EventTarget", f"{name}-assumed-log")
        assert target["rule"] == f"{name}-assumed"
        alarm = _one("aws:cloudwatch/metricAlarm:MetricAlarm", f"{name}-assumed-alarm")
        assert (alarm["namespace"], alarm["metricName"]) == ("AWS/Events", "MatchedEvents")
        assert alarm["dimensions"] == {"RuleName": f"{name}-assumed"}
        assert alarm["threshold"] == 1 and alarm["treatMissingData"] == "notBreaching"
        assert alarm["alarmActions"] == [stack.topic_arn()]

    return stack.ready().apply(check)


# --- The provisioning task and state machine --------------------------------------------------


def task_environment() -> dict[str, str]:
    return dict.fromkeys(provisioning.TASK_VARIABLES, "x")


def test_task_container_is_locked_down() -> None:
    [container] = json.loads(
        provisioning.container_definitions(
            image="repo@sha256:abc",
            log_group="/aws/ecs/p",
            region=REGION,
            environment=task_environment(),
        )
    )
    assert container["user"] == "10001:10001"
    assert container["readonlyRootFilesystem"] is True
    assert container["privileged"] is False
    assert container["linuxParameters"] == {"capabilities": {"add": [], "drop": ["ALL"]}}
    assert container["mountPoints"] == [
        {"sourceVolume": "work", "containerPath": "/work", "readOnly": False}
    ]
    assert container["portMappings"] == []
    names = [e["name"] for e in container["environment"]]
    assert names == sorted(names)


def test_task_is_never_defined_without_its_settings() -> None:
    environment = task_environment()
    del environment["STATE_BUCKET"]
    environment["SCAN_ROLE_ARN"] = ""
    with pytest.raises(provisioning.TaskEnvironmentError, match="SCAN_ROLE_ARN, STATE_BUCKET"):
        provisioning.container_definitions(
            image="i", log_group="g", region=REGION, environment=environment
        )


def test_state_machine_records_a_failure_and_then_fails() -> None:
    definition = json.loads(
        provisioning.state_machine_definition(
            cluster="cluster-arn",
            task_definition="task-arn",
            subnet="subnet-1",
            security_group="sg-1",
        )
    )
    states = definition["States"]
    assert definition["StartAt"] == "Provision"

    provision = states["Provision"]
    assert provision["Resource"] == "arn:aws:states:::ecs:runTask.sync"
    assert provision["End"] is True
    assert provision["Catch"] == [
        {"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "RecordFailure"}
    ]
    network = provision["Parameters"]["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert network == {
        "Subnets": ["subnet-1"],
        "SecurityGroups": ["sg-1"],
        "AssignPublicIp": "DISABLED",
    }
    [override] = provision["Parameters"]["Overrides"]["ContainerOverrides"]
    assert override["Name"] == "provision"
    assert {"Name": "PROVISION_COMMAND", "Value": "provision"} in override["Environment"]
    assert {
        "Name": "PROVISION_REQUEST",
        "Value.$": "States.JsonToString($.request)",
    } in override["Environment"]

    record = states["RecordFailure"]
    [override] = record["Parameters"]["Overrides"]["ContainerOverrides"]
    assert {"Name": "PROVISION_COMMAND", "Value": "fail"} in override["Environment"]
    assert {"Name": "PROVISION_ERROR", "Value.$": "$.error.Error"} in override["Environment"]
    assert record["Next"] == "Failed"
    assert states["Failed"]["Type"] == "Fail"


@pulumi.runtime.test
def test_provisioning_task_and_state_machine() -> Any:
    stack = Stack("p2")

    def check(_: list[Any]) -> None:
        task = _one("aws:ecs/taskDefinition:TaskDefinition", "p2-provision-task")
        assert task["requiresCompatibilities"] == ["FARGATE"]
        assert task["runtimePlatform"] == {
            "cpuArchitecture": "X86_64",
            "operatingSystemFamily": "LINUX",
        }
        assert (task["cpu"], task["memory"]) == ("1024", "4096")
        assert task["volumes"] == [{"name": "work"}]
        [container] = json.loads(task["containerDefinitions"])
        environment = {e["name"]: e["value"] for e in container["environment"]}
        assert set(environment) == set(provisioning.TASK_VARIABLES)
        assert environment["TENANT_ROLE_ARN"] == f"{ROLE}/{stack.name_prefix}-tenant"
        assert environment["BREAK_GLASS_ROLE_ARN"] == f"{ROLE}/{stack.name_prefix}-break-glass"
        assert environment["PROVISIONING_ROLE_ARN"] == f"{ROLE}/p2-provision-task-role-a1b2c3d"
        assert environment["VECTOR_DIMENSIONS"] == "1024"
        assert json.loads(environment["DEFAULT_TAGS"]) == TAGS
        assert environment["STATE_BUCKET"] == "p2-provision-state-a1b2c3d"

        machine = _one("aws:sfn/stateMachine:StateMachine", "p2-provision-sfn")
        assert machine["type"] == "STANDARD"
        # The execution's input holds the first admin's name and email: never logged.
        assert machine["loggingConfiguration"]["includeExecutionData"] is False
        definition = json.loads(machine["definition"])
        parameters = definition["States"]["Provision"]["Parameters"]
        assert parameters["TaskDefinition"].endswith("task-definition/p2-provision:1")
        assert parameters["NetworkConfiguration"]["AwsvpcConfiguration"]["Subnets"] == [
            "p2-app-use1-az2-id"
        ]

        sfn_policy = _one("aws:iam/rolePolicy:RolePolicy", "p2-provision-sfn-policy")["policy"]
        passed = by_sid(sfn_policy, "PassTheTaskRoles")
        assert passed["Condition"] == {
            "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
        }
        assert len(passed["Resource"]) == 2

        alarm = _one("aws:cloudwatch/metricAlarm:MetricAlarm", "p2-provision-failed-alarm")
        metrics = {q["metric"]["metricName"] for q in alarm["metricQueries"] if q.get("metric")}
        assert metrics == {"ExecutionsFailed", "ExecutionsTimedOut", "ExecutionsAborted"}
        # Each metric is filled with zero: one metric with no data point in a period must
        # not leave the sum without a value, which the alarm would read as "not breaching".
        [expression] = [q["expression"] for q in alarm["metricQueries"] if q.get("expression")]
        assert expression == "FILL(failed, 0) + FILL(timed_out, 0) + FILL(aborted, 0)"
        assert {q["id"] for q in alarm["metricQueries"] if q.get("metric")} == {
            "failed",
            "timed_out",
            "aborted",
        }
        assert alarm["alarmActions"] == [stack.topic_arn()]

    return stack.ready().apply(check)


@pulumi.runtime.test
def test_state_bucket_is_private_versioned_and_encrypted_with_its_own_key() -> Any:
    stack = Stack("p3")

    def check(_: list[Any]) -> None:
        versioning = _one(
            "aws:s3/bucketVersioning:BucketVersioning", "p3-provision-state-versioning"
        )
        assert versioning["versioningConfiguration"] == {"status": "Enabled"}
        block = _one(
            "aws:s3/bucketPublicAccessBlock:BucketPublicAccessBlock",
            "p3-provision-state-public-access",
        )
        assert all(
            block[k]
            for k in (
                "blockPublicAcls",
                "blockPublicPolicy",
                "ignorePublicAcls",
                "restrictPublicBuckets",
            )
        )
        encryption = _one(
            "aws:s3/bucketServerSideEncryptionConfiguration:BucketServerSideEncryptionConfiguration",
            "p3-provision-state-encryption",
        )
        [rule] = encryption["rules"]
        assert rule["bucketKeyEnabled"] is True
        default = rule["applyServerSideEncryptionByDefault"]
        assert default["sseAlgorithm"] == "aws:kms"
        assert default["kmsMasterKeyId"].endswith("key/p3-provision-state-key-key-id")
        # As S3 stores the rule. Left out, it differed on every preview.
        assert rule["blockedEncryptionTypes"] == ["SSE-C"]
        key = _one("aws:kms/key:Key", "p3-provision-state-key")
        assert key["enableKeyRotation"] is True
        use = by_sid(key["policy"], "OnlyProvisioningUses")
        assert use["Principal"] == {"AWS": f"{ROLE}/p3-provision-task-role-a1b2c3d"}
        policy = _one("aws:s3/bucketPolicy:BucketPolicy", "p3-provision-state-policy")["policy"]
        deny = by_sid(policy, "OnlyProvisioningReadsOrWritesState")
        assert deny["Condition"] == {
            "ArnNotEquals": {"aws:PrincipalArn": f"{ROLE}/p3-provision-task-role-a1b2c3d"}
        }

    return stack.ready().apply(check)


# --- The isolation runner ---------------------------------------------------------------------


def test_runner_probes_are_limited_to_tenant_resources() -> None:
    policy = json.loads(
        isolation.runner_role_policy(
            region=REGION,
            account_id=ACCOUNT_ID,
            name_prefix=PREFIX,
            log_group="arn:logs",
            db_user="arn:dbuser/app_tenant",
            tenant_role=f"{ROLE}/{PREFIX}-tenant",
        )
    )
    keys = by_sid(policy, "ProbeTenantKeys")
    # Without this condition the probe would reach keys whose policies trust IAM.
    assert keys["Condition"] == {"Null": {"aws:ResourceTag/tenant_id": "false"}}
    buckets = by_sid(policy, "ProbeTenantBuckets")
    assert all("assay-dev-t-*" in r for r in buckets["Resource"])
    services = {a.split(":")[0] for s in policy["Statement"] for a in as_list(s["Action"])}
    # No Rekognition (a collection has no resource policy to refuse it) and no Lambda.
    assert services == {"logs", "ec2", "rds-db", "sts", "s3", "kms", "s3vectors"}
    assert by_sid(policy, "ConnectAsAppTenant")["Resource"].endswith("/app_tenant")


@pulumi.runtime.test
def test_isolation_runner_runs_in_the_vpc() -> Any:
    stack = Stack("i1")

    def check(_: list[Any]) -> None:
        function = _one("aws:lambda/function:Function", "i1-isolation-fn")
        assert function["architectures"] == ["x86_64"]
        assert function["vpcConfig"]["subnetIds"] == ["i1-app-use1-az2-id"]
        variables = function["environment"]["variables"]
        assert variables["TSS_ENDPOINT"].startswith("https://")
        assert variables["TSS_SERVICE"] == "isolation"
        assert variables["TENANT_ROLE_ARN"] == f"{ROLE}/{stack.name_prefix}-tenant"
        build = _one("command:local:Command", "i1-isolation-image")
        assert build["environment"]["IMAGE_DOCKERFILE"] == "tests/isolation/Dockerfile"

    return stack.ready().apply(check)


@pulumi.runtime.test
def test_new_endpoints_exist_for_the_tss_and_provisioning() -> Any:
    stack = Stack("e1")

    def check(_: list[Any]) -> None:
        services = {
            inputs["serviceName"]
            for _, inputs in registered("aws:ec2/vpcEndpoint:VpcEndpoint", "e1-")
        }
        for service in ("execute-api", "lambda", "guardduty"):
            assert f"com.amazonaws.{REGION}.{service}" in services

    return stack.ready().apply(check)


CALLER_A = f"{ROLE}/api-task"
CALLER_B = f"{ROLE}/worker"
API_ARN = f"arn:aws:execute-api:{REGION}:{ACCOUNT_ID}:abc123"


def test_api_policy_sent_to_aws_is_in_the_form_api_gateway_stores() -> None:
    """Regression: API Gateway rewrites `execute-api:/...` to the API's ARN, and a one-item
    principal list to a string. Anything else shows a difference on every preview."""
    stored = tss.api_resource_policy(
        vpc_endpoint_id="vpce-1", callers=[CALLER_A], execution_arn=API_ARN
    )
    assert json.loads(stored) == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "OnlyThroughTheVpcEndpoint",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "execute-api:Invoke",
                "Resource": f"{API_ARN}/*",
                "Condition": {"StringNotEquals": {"aws:SourceVpce": "vpce-1"}},
            },
            {
                "Sid": "KnownCallers",
                "Effect": "Allow",
                "Principal": {"AWS": CALLER_A},
                "Action": "execute-api:Invoke",
                "Resource": f"{API_ARN}/v1/POST/resolve",
            },
        ],
    }
    assert "execute-api:/" not in stored


def test_api_policy_with_several_callers_lists_them_in_order() -> None:
    stored = json.loads(
        tss.api_resource_policy(
            vpc_endpoint_id="vpce-1", callers=[CALLER_B, CALLER_A], execution_arn=API_ARN
        )
    )
    assert by_sid(stored, "KnownCallers")["Principal"] == {"AWS": [CALLER_A, CALLER_B]}


def test_api_policy_without_callers_only_denies() -> None:
    stored = json.loads(
        tss.api_resource_policy(vpc_endpoint_id="vpce-1", callers=[], execution_arn=API_ARN)
    )
    assert [s["Effect"] for s in stored["Statement"]] == ["Deny"]


def test_portable_api_policy_names_no_api() -> None:
    """Used only for the redeploy fingerprint, which must not depend on the API's ARN."""
    portable = json.loads(tss.api_resource_policy(vpc_endpoint_id="vpce-1", callers=[CALLER_A]))
    resources = [s["Resource"] for s in portable["Statement"]]
    assert resources == ["execute-api:/*", "execute-api:/v1/POST/resolve"]
    assert by_sid(portable, "KnownCallers")["Principal"] == {"AWS": [CALLER_A]}


@pulumi.runtime.test
def test_no_bucket_encryption_rule_leaves_out_what_s3_adds() -> Any:
    """Every encryption rule in the stack is customer-managed KMS and declares the SSE-C
    block S3 puts on new buckets."""
    stack = Stack("s1")
    token = "aws:s3/bucketServerSideEncryptionConfiguration:BucketServerSideEncryptionConfiguration"

    def check(_: list[Any]) -> None:
        rules = [rule for name, inputs in registered(token, "s1-") for rule in inputs["rules"]]
        assert rules, "no bucket encryption registered"
        for rule in rules:
            assert rule["applyServerSideEncryptionByDefault"]["sseAlgorithm"] == "aws:kms"
            assert rule["applyServerSideEncryptionByDefault"]["kmsMasterKeyId"]
            assert rule["bucketKeyEnabled"] is True
            assert rule["blockedEncryptionTypes"] == list(provisioning.BLOCKED_ENCRYPTION_TYPES)

    return stack.ready().apply(check)


def test_tss_access_log_holds_no_request_body() -> None:
    assert not any("body" in value.lower() for value in tss.ACCESS_LOG_FORMAT.values())


# --- The alarm topic ---------------------------------------------------------------------------


def test_alarm_email_comes_from_config_and_is_checked() -> None:
    assert validate_alerts(AlertsConfig(email="alarms@example.test")).email == "alarms@example.test"
    for bad in ("", "not-an-address", "two words@example.test"):
        with pytest.raises(ValueError, match="alarmEmail"):
            validate_alerts(AlertsConfig(email=bad))


def test_only_cloudwatch_alarms_in_this_account_publish_to_the_topic() -> None:
    policy = json.loads(
        alerts.topic_policy(topic="arn:topic", region=REGION, account_id=ACCOUNT_ID)
    )
    publish = by_sid(policy, "CloudWatchAlarmsInThisAccountPublish")
    assert publish["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
    assert publish["Action"] == "sns:Publish"
    assert publish["Condition"] == {
        "ArnLike": {"aws:SourceArn": f"arn:aws:cloudwatch:{REGION}:{ACCOUNT_ID}:alarm:*"},
        "StringEquals": {"aws:SourceAccount": ACCOUNT_ID},
    }
    allows = [s for s in policy["Statement"] if s["Effect"] == "Allow"]
    assert len(allows) == 1
    assert by_sid(policy, "DenyInsecureTransport")["Effect"] == "Deny"


def test_alarm_topic_key_lets_cloudwatch_publish() -> None:
    """CloudWatch cannot publish to a topic encrypted with the AWS-managed SNS key."""
    statement = by_sid(alerts.key_policy(ACCOUNT_ID), "CloudWatchAlarmsPublish")
    assert statement["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
    assert set(statement["Action"]) == {"kms:Decrypt", "kms:GenerateDataKey*"}


@pulumi.runtime.test
def test_alarm_topic_is_encrypted_and_emails_the_configured_address() -> Any:
    stack = Stack("al1")

    def check(_: list[Any]) -> None:
        topic = _one("aws:sns/topic:Topic", "al1-alarms-topic")
        assert topic["name"] == "al1-alarms"
        assert topic["kmsMasterKeyId"] == "al1-alarms-key-key-id"
        key = _one("aws:kms/key:Key", "al1-alarms-key")
        assert key["enableKeyRotation"] is True
        subscription = _one("aws:sns/topicSubscription:TopicSubscription", "al1-alarms-email")
        assert (subscription["protocol"], subscription["endpoint"]) == ("email", ALARM_EMAIL)
        assert subscription["topic"] == stack.topic_arn()
        policy = _one("aws:sns/topicPolicy:TopicPolicy", "al1-alarms-topic-policy")
        assert policy["arn"] == stack.topic_arn()
        # Every alarm in the stack notifies the topic.
        alarms = [
            (name, inputs)
            for name, inputs in registered("aws:cloudwatch/metricAlarm:MetricAlarm")
            if name.startswith(("al1-", "assay-al1-"))
        ]
        # Break glass, provisioning failures, and the auth-event trigger's three.
        assert len(alarms) == 5
        for name, inputs in alarms:
            assert inputs.get("alarmActions") == [stack.topic_arn()], name

    return pulumi.Output.all(
        stack.ready(), stack.alerts.subscription.id, stack.alerts.topic.arn
    ).apply(check)


# --- The worker task's route: its own, and no one else's (build step 6, second half) -----------

WORKER_ROLE = f"{ROLE}/pl-pipeline-worker-task-role-9f8e7d"
API_ROLE = f"{ROLE}/pl-api-task-role-1a2b3c"
STEPS_ROLE = f"{ROLE}/pl-pipeline-steps-role-4d5e6f"
ENDPOINT = "vpce-0123456789abcdef0"


def test_each_caller_is_admitted_to_its_own_route_only() -> None:
    api_arn = f"arn:aws:execute-api:{REGION}:{ACCOUNT_ID}:abc123"
    policy = json.loads(
        tss.api_resource_policy(
            vpc_endpoint_id=ENDPOINT,
            callers=[STEPS_ROLE, API_ROLE],
            worker_callers=[WORKER_ROLE],
            execution_arn=api_arn,
        )
    )
    assert [s["Sid"] for s in policy["Statement"]] == [
        "OnlyThroughTheVpcEndpoint",
        "KnownCallers",
        "WorkerTask",
    ]
    known, worker = by_sid(policy, "KnownCallers"), by_sid(policy, "WorkerTask")
    assert known["Resource"] == f"{api_arn}/v1/POST/resolve"
    assert known["Principal"] == {"AWS": sorted([API_ROLE, STEPS_ROLE])}
    assert WORKER_ROLE not in json.dumps(known)
    assert worker["Resource"] == f"{api_arn}/v1/POST/resolve-worker"
    # One caller: a string, as AWS stores it.
    assert worker["Principal"] == {"AWS": WORKER_ROLE}
    assert API_ROLE not in json.dumps(worker) and STEPS_ROLE not in json.dumps(worker)
    # The portable form (the redeploy fingerprint) names both routes too.
    portable = json.loads(
        tss.api_resource_policy(
            vpc_endpoint_id=ENDPOINT, callers=[API_ROLE], worker_callers=[WORKER_ROLE]
        )
    )
    assert by_sid(portable, "WorkerTask")["Resource"] == "execute-api:/v1/POST/resolve-worker"


def test_a_callers_own_policy_names_its_one_route() -> None:
    api_arn = f"arn:aws:execute-api:{REGION}:{ACCOUNT_ID}:abc123"
    [resolve] = json.loads(tss.invoke_policy(api_arn))["Statement"]
    assert resolve["Resource"] == f"{api_arn}/v1/POST/resolve"
    [worker] = json.loads(tss.invoke_policy(api_arn, tss.WORKER_PATH))["Statement"]
    assert worker["Resource"] == f"{api_arn}/v1/POST/resolve-worker"
    assert worker["Action"] == resolve["Action"] == "execute-api:Invoke"


def test_worker_access_is_never_combined_with_another() -> None:
    role: Any = object()
    worker = TssCaller(role=role, access=("worker",))
    assert worker.is_worker and worker.path == "resolve-worker"
    api = TssCaller(role=role, access=("read", "upload"))
    assert not api.is_worker and api.path == "resolve"
    tss.check_callers({"worker": worker, "api": api})
    for mixed in (("worker", "pipeline"), ("pipeline", "read", "upload", "worker")):
        with pytest.raises(ValueError, match="worker access cannot be combined"):
            tss.check_callers({"runner": TssCaller(role=role, access=mixed)})


def test_the_tss_is_told_each_callers_accesses() -> None:
    role: Any = object()
    grants = json.loads(
        tss.caller_grants(
            {"worker": "pl-worker-role", "api": "pl-api-role"},
            {
                "worker": TssCaller(role=role, access=("worker",)),
                "api": TssCaller(role=role, access=("upload", "read")),
            },
        )
    )
    assert grants == {
        "pl-worker-role": {"service": "worker", "access": ["worker"]},
        "pl-api-role": {"service": "api", "access": ["read", "upload"]},
    }


# --- Policies that name a role created in the same run wait for IAM (2026-11-25) --------------


@pulumi.runtime.test
def test_policies_that_name_new_roles_wait_for_iam_to_know_them() -> Any:
    """The first `pulumi up` of the second half failed: the TSS API's policy named the worker
    task's role a moment after it was created, and API Gateway did not know it yet. Each
    such policy now depends on a wait that runs once the roles exist."""
    from assay_infra import propagation

    stack = Stack("t9")

    def check(_: list[Any]) -> None:
        waits = {
            name: inputs
            for name, inputs in registered("command:local:Command", "t9-")
            if name.endswith("-iam-propagation")
        }
        assert set(waits) == {"t9-tss-iam-propagation", "t9-provision-iam-propagation"}
        for inputs in waits.values():
            # It only waits: no AWS call, nothing to delete, nothing on an ordinary run.
            assert inputs["create"] == propagation.WAIT == "import time; time.sleep(30)"
            assert inputs["interpreter"][-2:] == ["python", "-c"]
            assert "delete" not in inputs and "update" not in inputs
        # It runs again when, and only when, the roles it waited for change.
        [tss_roles] = waits["t9-tss-iam-propagation"]["triggers"]
        assert len(tss_roles) == 2 and tss_roles == sorted(tss_roles)
        assert any("t9-tss-role" in arn for arn in tss_roles)
        assert any("t9-isolation-role" in arn for arn in tss_roles)
        [provision_roles] = waits["t9-provision-iam-propagation"]["triggers"]
        assert len(provision_roles) == 1 and "t9-provision-task-role" in provision_roles[0]

    return pulumi.Output.all(
        stack.ready(), stack.storage.iam_ready.id, stack.provisioning.iam_ready.id
    ).apply(check)


def test_each_policy_with_a_new_role_as_principal_depends_on_the_wait() -> None:
    """Read from the source: the three resources whose policy names, as a principal, a role
    this program creates. (A dependency is not among a resource's inputs, so the mocks
    cannot show it.)"""
    tss_source = (REPO_ROOT / "infra" / "assay_infra" / "tss.py").read_text(encoding="utf-8")
    provisioning_source = (REPO_ROOT / "infra" / "assay_infra" / "provisioning.py").read_text(
        encoding="utf-8"
    )
    waiting = "depends_on=[self.iam_ready]"

    def options_of(source: str, resource: str) -> str:
        start = source.index(resource)
        return source[start : source.index("\n        )\n", start)]

    assert waiting in options_of(tss_source, "self.api_policy = aws.apigateway.RestApiPolicy(")
    assert waiting in options_of(tss_source, "self.tenant_role = aws.iam.Role(")
    state_key = options_of(provisioning_source, "self.state_key = aws.kms.Key(")
    assert waiting in state_key and "protect=True" in state_key


def test_a_wait_needs_a_role() -> None:
    from assay_infra import propagation

    with pytest.raises(ValueError, match="at least one role"):
        propagation.iam_propagation("w", roles=[], repo_root=REPO_ROOT)
