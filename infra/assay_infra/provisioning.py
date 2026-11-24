"""Tenant provisioning (BUILD_SPEC.md section 6).

A Step Functions state machine runs one Fargate task per tenant. The task
(`assay_tss.provision`) runs the tenant's own Pulumi stack with the Automation API, writes
the `tenant_storage` row, invites the first admin and sets the tenant `active`. If the task
dies, the state machine's catch runs it again with the `fail` command to record that.

What is here:

- **The provisioning role** (the task's role). Scoped to resources named `<prefix>-t-*` and
  the state bucket. It manages settings, tags and policies; it has no object read access
  and cannot decrypt with a tenant key except where S3 Vectors does it on its behalf
  to create a vector index. Its own policy denies both.
- **The state bucket and key.** Tenant stacks keep their state here, because the task has
  no route to Pulumi Cloud. Versioned, encrypted with its own key, and its objects are
  readable only by the provisioning role.
- **The scan role** that GuardDuty Malware Protection for S3 assumes for every tenant
  bucket. Its trust policy requires this account as the source account.
- **The Cognito admin Lambda**, outside the VPC, which invites the first admin. Only the
  provisioning role may invoke it, and the `lambda` endpoint's policy allows nothing else.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pulumi
import pulumi_aws as aws

from assay_infra.api import _ecs_tasks_trust, execution_role_policy
from assay_infra.auth import Auth
from assay_infra.database import Database, db_user_arn
from assay_infra.images import WORKSPACE_MANIFESTS, ContainerImage
from assay_infra.network import Network
from assay_infra.tss import TenantStorage, _service_trust
from assay_tss import policies
from assay_tss.naming import tenant_pattern

PLATFORM_WRITER_DB_USER = "platform_writer"  # assay_db.roles.PLATFORM_WRITER

TASK_DOCKERFILE = "tss/provision.Dockerfile"
TASK_IMAGE_TREES = ("db/assay_db", "tss/assay_tss")
INVITE_DOCKERFILE = "api/cognito-admin.Dockerfile"
INVITE_SOURCES = (
    "api/assay_api/__init__.py",
    "api/assay_api/claims.py",
    "api/assay_api/cognito_admin.py",
)

CONTAINER_NAME = "provision"
CONTAINER_USER = "10001:10001"  # tss/provision.Dockerfile
WORK_VOLUME = "work"
WORK_DIR = "/work"  # tss/provision.Dockerfile
# The AWS provider plugin needs well over 1 GB while it runs.
TASK_CPU = "1024"
TASK_MEMORY = "4096"
TASK_TIMEOUT_SECONDS = 1800
FAIL_TIMEOUT_SECONDS = 600
INVITE_TIMEOUT_SECONDS = 30
LOG_RETENTION_DAYS = 30

GUARDDUTY_PLAN_SERVICE = "malware-protection-plan.guardduty.amazonaws.com"
GUARDDUTY_RULE = "DO-NOT-DELETE-AmazonGuardDutyMalwareProtectionS3*"

# A math expression has no value for a period in which any of its metrics has no data
# point, and the alarm then treats the period as not breaching. FILL keeps one metric's
# silence from hiding another's count.
FAILURES_EXPRESSION = "FILL(failed, 0) + FILL(timed_out, 0) + FILL(aborted, 0)"

# Bucket settings only: no action here reads, writes or deletes an object. s3:ListBucket is
# what HeadBucket needs, which the AWS provider calls to read a bucket.
BUCKET_SETTINGS_ACTIONS = (
    "s3:CreateBucket",
    "s3:DeleteBucket",
    "s3:ListBucket",
    "s3:GetBucket*",
    "s3:PutBucket*",
    "s3:DeleteBucketPolicy",
    "s3:GetEncryptionConfiguration",
    "s3:PutEncryptionConfiguration",
    "s3:GetLifecycleConfiguration",
    "s3:GetReplicationConfiguration",
    "s3:GetAccelerateConfiguration",
)
VECTOR_SETTINGS_ACTIONS = (
    "s3vectors:CreateVectorBucket",
    "s3vectors:GetVectorBucket",
    "s3vectors:DeleteVectorBucket",
    "s3vectors:GetVectorBucketPolicy",
    "s3vectors:PutVectorBucketPolicy",
    "s3vectors:DeleteVectorBucketPolicy",
    "s3vectors:ListIndexes",
    "s3vectors:CreateIndex",
    "s3vectors:GetIndex",
    "s3vectors:DeleteIndex",
    "s3vectors:TagResource",
    "s3vectors:UntagResource",
    "s3vectors:ListTagsForResource",
)
COLLECTION_SETTINGS_ACTIONS = (
    "rekognition:CreateCollection",
    "rekognition:DescribeCollection",
    "rekognition:DeleteCollection",
    "rekognition:TagResource",
    "rekognition:UntagResource",
    "rekognition:ListTagsForResource",
)
MALWARE_PLAN_ACTIONS = (
    "guardduty:GetMalwareProtectionPlan",
    "guardduty:UpdateMalwareProtectionPlan",
    "guardduty:DeleteMalwareProtectionPlan",
    "guardduty:TagResource",
    "guardduty:UntagResource",
    "guardduty:ListTagsForResource",
)
# S3 blocks SSE-C on every new bucket and reports it in the bucket's encryption rule, so the
# rule must say so too or every preview shows a difference. The encryption rule is also the
# last bucket setting applied: the provider reads a setting back right after writing it, and
# on a bucket only seconds old S3 can still answer with the default (AES256), which Pulumi
# then records. If a preview ever shows AES256 becoming aws:kms, check the bucket itself.
BLOCKED_ENCRYPTION_TYPES = ("SSE-C",)  # assay_tss.provision.program.BLOCKED_ENCRYPTION_TYPES
STATE_OBJECT_ACTIONS = ("s3:GetObject", "s3:PutObject", "s3:DeleteObject")
STATE_KEY_USE_ACTIONS = ("kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey")
# Administration of the state key through IAM. No cryptographic operation and no grants.
STATE_KEY_ADMIN_ACTIONS = (
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
    "kms:ListGrants",
    "kms:ListKeyPolicies",
    "kms:RevokeGrant",
)
# Object data in the state bucket, denied to everyone but the provisioning role.
STATE_DATA_ACTIONS = (
    "s3:GetObject*",
    "s3:PutObject*",
    "s3:DeleteObject*",
    "s3:RestoreObject",
    "s3:ListBucketVersions",
)
INVITE_COGNITO_ACTIONS = (
    "cognito-idp:AdminCreateUser",
    "cognito-idp:AdminGetUser",
    "cognito-idp:AdminAddUserToGroup",
)

# What the task reads from its environment (assay_tss.provision.settings and
# assay_db.connect), besides the request the state machine adds for each run.
TASK_VARIABLES = (
    "AWS_REGION",
    "ACCOUNT_ID",
    "NAME_PREFIX",
    "TENANT_ROLE_ARN",
    "BREAK_GLASS_ROLE_ARN",
    "PROVISIONING_ROLE_ARN",
    "SCAN_ROLE_ARN",
    "STATE_BUCKET",
    "STATE_KEY_ARN",
    "INVITE_FUNCTION",
    "VECTOR_DIMENSIONS",
    "DEFAULT_TAGS",
    "DEFAULT_INGEST_MODEL",
    "DEFAULT_QUERY_MODEL",
    "UPLOAD_ORIGINS",
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
)


def _json(document: object) -> str:
    return json.dumps(document, separators=(",", ":"))


# --- Policies (pure, unit tested) ---------------------------------------------------------


def provisioning_role_policy(
    *,
    region: str,
    account_id: str,
    name_prefix: str,
    tag_keys: Sequence[str],
    platform_keys: Sequence[str],
    state_bucket: str,
    state_key: str,
    scan_role: str,
    invite_function: str,
    db_user: str,
) -> str:
    """Everything the provisioning task may do, and what it never may.

    Tagging a key in the request that creates it is the one KMS permission that has to
    come from this policy rather than the key's own. AWS KMS requires it "in an IAM policy
    that isn't restricted to particular KMS keys", so the grant is on every key in the
    account and may be conditioned only on the tag keys in the request (`tag_keys`: the
    provider's default tags and `tenant_id`). Conditions on `aws:RequestTag` or
    `aws:ResourceTag` are not met at creation: the first provisioning run failed on one.

    What keeps the role away from the platform's own keys is therefore an explicit deny
    that names them (`platform_keys`). After creation, a tenant key's own policy is what
    lets this role administer and tag it.

    `tenant_id` on a key is write-once (the tenant role's policy finds a key by it): the
    role is denied changing its value on a key that has one, and denied removing it from
    any key. It may still change the other tags.
    """
    if not platform_keys:
        raise ValueError("platform_keys must name every KMS key the platform stack creates")
    pattern = tenant_pattern(name_prefix)
    bucket = f"arn:aws:s3:::{pattern}"
    vector_bucket = f"arn:aws:s3vectors:{region}:{account_id}:bucket/{pattern}"
    tenant_tag = f"aws:ResourceTag/{policies.TENANT_TAG}"
    tenant_request_tag = f"aws:RequestTag/{policies.TENANT_TAG}"
    any_key = f"arn:aws:kms:{region}:{account_id}:key/*"
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "StateBucket",
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                    "Resource": state_bucket,
                },
                {
                    "Sid": "StateObjects",
                    "Effect": "Allow",
                    "Action": list(STATE_OBJECT_ACTIONS),
                    "Resource": f"{state_bucket}/*",
                },
                {
                    "Sid": "StateKey",
                    "Effect": "Allow",
                    "Action": list(STATE_KEY_USE_ACTIONS),
                    "Resource": state_key,
                },
                {
                    "Sid": "TenantBucketSettings",
                    "Effect": "Allow",
                    "Action": list(BUCKET_SETTINGS_ACTIONS),
                    "Resource": bucket,
                },
                {
                    # Only keys created with a tenant_id tag.
                    "Sid": "CreateTenantKeys",
                    "Effect": "Allow",
                    "Action": "kms:CreateKey",
                    "Resource": "*",
                    "Condition": {"Null": {f"aws:RequestTag/{policies.TENANT_TAG}": "false"}},
                },
                {
                    # Tags sent with CreateKey. See the docstring for why it is this broad.
                    "Sid": "TagKeysWhenCreatingThem",
                    "Effect": "Allow",
                    "Action": "kms:TagResource",
                    "Resource": f"arn:aws:kms:{region}:{account_id}:key/*",
                    "Condition": {"ForAllValues:StringEquals": {"aws:TagKeys": sorted(tag_keys)}},
                },
                {
                    "Sid": "NeverTagPlatformKeys",
                    "Effect": "Deny",
                    "Action": ["kms:TagResource", "kms:UntagResource"],
                    "Resource": sorted(platform_keys),
                },
                {
                    # Write-once: a key that has a tenant_id keeps that value. The deny needs
                    # the tag on the key, the tag in the request, and the two to differ, so it
                    # cannot apply while a key is being created, whichever tags KMS puts in
                    # the request context at that moment.
                    "Sid": "TenantIdTagNeverChanges",
                    "Effect": "Deny",
                    "Action": "kms:TagResource",
                    "Resource": any_key,
                    "Condition": {
                        "Null": {tenant_tag: "false", tenant_request_tag: "false"},
                        "StringNotEquals": {tenant_request_tag: "${" + tenant_tag + "}"},
                    },
                },
                {
                    "Sid": "TenantIdTagIsNeverRemoved",
                    "Effect": "Deny",
                    "Action": "kms:UntagResource",
                    "Resource": any_key,
                    "Condition": {
                        "ForAnyValue:StringEquals": {"aws:TagKeys": [policies.TENANT_TAG]}
                    },
                },
                {
                    "Sid": "TenantKeyAliases",
                    "Effect": "Allow",
                    "Action": ["kms:CreateAlias", "kms:UpdateAlias", "kms:DeleteAlias"],
                    "Resource": f"arn:aws:kms:{region}:{account_id}:alias/{pattern}",
                },
                {
                    # Listing has no resource to scope to.
                    "Sid": "ListForTheProvider",
                    "Effect": "Allow",
                    "Action": ["kms:ListAliases", "s3vectors:ListVectorBuckets"],
                    "Resource": "*",
                },
                {
                    "Sid": "TenantVectorBucketSettings",
                    "Effect": "Allow",
                    "Action": list(VECTOR_SETTINGS_ACTIONS),
                    "Resource": [vector_bucket, f"{vector_bucket}/index/*"],
                },
                {
                    "Sid": "TenantFaceCollections",
                    "Effect": "Allow",
                    "Action": list(COLLECTION_SETTINGS_ACTIONS),
                    "Resource": f"arn:aws:rekognition:{region}:{account_id}:collection/{pattern}",
                },
                {
                    # A plan's ARN is not known before it exists.
                    "Sid": "CreateMalwareProtectionPlans",
                    "Effect": "Allow",
                    "Action": "guardduty:CreateMalwareProtectionPlan",
                    "Resource": "*",
                },
                {
                    "Sid": "MalwareProtectionPlans",
                    "Effect": "Allow",
                    "Action": list(MALWARE_PLAN_ACTIONS),
                    "Resource": (
                        f"arn:aws:guardduty:{region}:{account_id}:malware-protection-plan/*"
                    ),
                },
                {
                    "Sid": "PassTheScanRoleToGuardDuty",
                    "Effect": "Allow",
                    "Action": "iam:PassRole",
                    "Resource": scan_role,
                    "Condition": {"StringEquals": {"iam:PassedToService": GUARDDUTY_PLAN_SERVICE}},
                },
                {
                    "Sid": "InviteTheFirstAdmin",
                    "Effect": "Allow",
                    "Action": "lambda:InvokeFunction",
                    "Resource": invite_function,
                },
                {
                    "Sid": "ConnectAsPlatformWriter",
                    "Effect": "Allow",
                    "Action": "rds-db:connect",
                    "Resource": db_user,
                },
                {
                    "Sid": "NeverTenantObjects",
                    "Effect": "Deny",
                    "Action": list(policies.OBJECT_DATA_ACTIONS),
                    "Resource": [bucket, f"{bucket}/*"],
                },
                {
                    # Decrypt with a tenant key is denied unless S3 Vectors is making the call
                    # for this role, which creating a vector index requires. A direct call
                    # has no kms:ViaService, and a call through S3 names S3: both are denied.
                    "Sid": "NeverDecryptWithATenantKey",
                    "Effect": "Deny",
                    "Action": "kms:Decrypt",
                    "Resource": any_key,
                    "Condition": {
                        "Null": {tenant_tag: "false"},
                        "StringNotEquals": {"kms:ViaService": f"s3vectors.{region}.amazonaws.com"},
                    },
                },
                {
                    "Sid": "NeverReEncryptOrGrantWithATenantKey",
                    "Effect": "Deny",
                    "Action": ["kms:ReEncrypt*", "kms:CreateGrant"],
                    "Resource": any_key,
                    "Condition": {"Null": {tenant_tag: "false"}},
                },
            ],
        }
    )


def scan_role_trust_policy(account_id: str) -> str:
    """GuardDuty Malware Protection for S3, acting for this account only."""
    return _service_trust(GUARDDUTY_PLAN_SERVICE, account_id)


def scan_role_policy(*, region: str, account_id: str, name_prefix: str) -> str:
    """AWS's documented role policy for Malware Protection for S3, for every tenant bucket."""
    bucket = f"arn:aws:s3:::{tenant_pattern(name_prefix)}"
    rule = f"arn:aws:events:{region}:{account_id}:rule/{GUARDDUTY_RULE}"
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AllowManagedRuleToSendS3EventsToGuardDuty",
                    "Effect": "Allow",
                    "Action": [
                        "events:PutRule",
                        "events:DeleteRule",
                        "events:PutTargets",
                        "events:RemoveTargets",
                    ],
                    "Resource": rule,
                    "Condition": {"StringLike": {"events:ManagedBy": GUARDDUTY_PLAN_SERVICE}},
                },
                {
                    "Sid": "AllowGuardDutyToMonitorEventBridgeManagedRule",
                    "Effect": "Allow",
                    "Action": ["events:DescribeRule", "events:ListTargetsByRule"],
                    "Resource": rule,
                },
                {
                    "Sid": "AllowPostScanTag",
                    "Effect": "Allow",
                    "Action": [
                        "s3:PutObjectTagging",
                        "s3:GetObjectTagging",
                        "s3:PutObjectVersionTagging",
                        "s3:GetObjectVersionTagging",
                    ],
                    "Resource": f"{bucket}/*",
                },
                {
                    "Sid": "AllowEnableS3EventBridgeEvents",
                    "Effect": "Allow",
                    "Action": ["s3:PutBucketNotification", "s3:GetBucketNotification"],
                    "Resource": bucket,
                },
                {
                    "Sid": "AllowPutValidationObject",
                    "Effect": "Allow",
                    "Action": "s3:PutObject",
                    "Resource": f"{bucket}/malware-protection-resource-validation-object",
                },
                {
                    "Sid": "AllowCheckBucketOwnership",
                    "Effect": "Allow",
                    "Action": "s3:ListBucket",
                    "Resource": bucket,
                },
                {
                    "Sid": "AllowMalwareScan",
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                    "Resource": f"{bucket}/*",
                },
                {
                    "Sid": "AllowDecryptForMalwareScan",
                    "Effect": "Allow",
                    "Action": ["kms:GenerateDataKey", "kms:Decrypt"],
                    "Resource": f"arn:aws:kms:{region}:{account_id}:key/*",
                    "Condition": {
                        "StringLike": {"kms:ViaService": f"s3.{region}.amazonaws.com"},
                        "Null": {f"aws:ResourceTag/{policies.TENANT_TAG}": "false"},
                    },
                },
            ],
        }
    )


def state_key_policy(*, account_id: str, provisioning_role: str) -> str:
    """The account administers the key through IAM; only the provisioning role uses it."""
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AccountAdministersWithoutUsing",
                    "Effect": "Allow",
                    "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
                    "Action": list(STATE_KEY_ADMIN_ACTIONS),
                    "Resource": "*",
                },
                {
                    "Sid": "OnlyProvisioningUses",
                    "Effect": "Allow",
                    "Principal": {"AWS": provisioning_role},
                    "Action": list(STATE_KEY_USE_ACTIONS),
                    "Resource": "*",
                },
            ],
        }
    )


def state_bucket_policy(*, bucket: str, provisioning_role: str) -> str:
    """State objects are readable and writable only by the provisioning role.

    Bucket settings stay with the account's IAM policies, so this stack can manage the
    bucket. That includes s3:ListBucket (HeadBucket), so an administrator can see stack
    names, which are tenant IDs, but not what any stack holds.
    """
    both = [bucket, f"{bucket}/*"]
    return _json(
        {
            "Version": "2012-10-17",
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
                    "Sid": "OnlyProvisioningReadsOrWritesState",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": list(STATE_DATA_ACTIONS),
                    "Resource": both,
                    "Condition": {"ArnNotEquals": {"aws:PrincipalArn": provisioning_role}},
                },
            ],
        }
    )


def invite_role_policy(*, log_group: str, user_pool: str) -> str:
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "Logs",
                    "Effect": "Allow",
                    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                    "Resource": f"{log_group}:*",
                },
                {
                    "Sid": "InviteTenantPoolUsers",
                    "Effect": "Allow",
                    "Action": list(INVITE_COGNITO_ACTIONS),
                    "Resource": user_pool,
                },
            ],
        }
    )


# Invoke statements other components add to the `lambda` endpoint's policy:
# (statement ID, function ARN, the caller role ARNs).
LambdaGrant = tuple[str, str, Sequence[str]]


def lambda_endpoint_policy(
    *, invite_function: str, provisioning_role: str, grants: Sequence[LambdaGrant] = ()
) -> str:
    """Through the VPC's Lambda endpoint: named functions, each for named callers only."""
    statements: list[dict[str, Any]] = [
        {
            "Sid": "ProvisioningInvitesTheFirstAdmin",
            "Effect": "Allow",
            "Principal": "*",
            "Action": "lambda:InvokeFunction",
            "Resource": invite_function,
            "Condition": {"ArnEquals": {"aws:PrincipalArn": provisioning_role}},
        }
    ]
    for sid, function, callers in grants:
        names = sorted(callers)
        # One caller is written as a string, as IAM stores a one-item list.
        principal: str | list[str] = names[0] if len(names) == 1 else names
        statements.append(
            {
                "Sid": sid,
                "Effect": "Allow",
                "Principal": "*",
                "Action": "lambda:InvokeFunction",
                "Resource": function,
                "Condition": {"ArnEquals": {"aws:PrincipalArn": principal}},
            }
        )
    return _json({"Version": "2012-10-17", "Statement": statements})


def state_machine_role_policy(
    *,
    region: str,
    account_id: str,
    cluster_name: str,
    cluster: str,
    task_family: str,
    task_role: str,
    execution_role: str,
) -> str:
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "RunTheProvisioningTask",
                    "Effect": "Allow",
                    "Action": "ecs:RunTask",
                    "Resource": (
                        f"arn:aws:ecs:{region}:{account_id}:task-definition/{task_family}:*"
                    ),
                    "Condition": {"ArnEquals": {"ecs:cluster": cluster}},
                },
                {
                    "Sid": "WatchAndStopIt",
                    "Effect": "Allow",
                    "Action": ["ecs:StopTask", "ecs:DescribeTasks"],
                    "Resource": f"arn:aws:ecs:{region}:{account_id}:task/{cluster_name}/*",
                },
                {
                    "Sid": "PassTheTaskRoles",
                    "Effect": "Allow",
                    "Action": "iam:PassRole",
                    "Resource": sorted([task_role, execution_role]),
                    "Condition": {
                        "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
                    },
                },
                {
                    # The managed rule Step Functions uses to learn that the task stopped.
                    "Sid": "TaskStateChangeRule",
                    "Effect": "Allow",
                    "Action": ["events:PutTargets", "events:PutRule", "events:DescribeRule"],
                    "Resource": (
                        f"arn:aws:events:{region}:{account_id}:rule/"
                        "StepFunctionsGetEventsForECSTaskRule"
                    ),
                },
                {
                    # Log delivery actions do not support resource scoping.
                    "Sid": "LogDelivery",
                    "Effect": "Allow",
                    "Action": [
                        "logs:CreateLogDelivery",
                        "logs:GetLogDelivery",
                        "logs:UpdateLogDelivery",
                        "logs:DeleteLogDelivery",
                        "logs:ListLogDeliveries",
                        "logs:PutResourcePolicy",
                        "logs:DescribeResourcePolicies",
                        "logs:DescribeLogGroups",
                    ],
                    "Resource": "*",
                },
            ],
        }
    )


class TaskEnvironmentError(ValueError):
    """The provisioning task would start without what it needs. Fails the preview."""


def container_definitions(
    *, image: str, log_group: str, region: str, environment: Mapping[str, str]
) -> str:
    """Written exactly as ECS stores a container definition (see assay_infra.api)."""
    missing = [name for name in TASK_VARIABLES if not environment.get(name)]
    if missing:
        raise TaskEnvironmentError(f"provisioning task environment lacks {', '.join(missing)}")
    return json.dumps(
        [
            {
                "name": CONTAINER_NAME,
                "image": image,
                "cpu": 0,
                "essential": True,
                "user": CONTAINER_USER,
                "readonlyRootFilesystem": True,
                "privileged": False,
                "portMappings": [],
                "environment": [{"name": k, "value": v} for k, v in sorted(environment.items())],
                "linuxParameters": {"capabilities": {"add": [], "drop": ["ALL"]}},
                # The task's only writable directory: Pulumi's workspace and temp files.
                "mountPoints": [
                    {"sourceVolume": WORK_VOLUME, "containerPath": WORK_DIR, "readOnly": False}
                ],
                "volumesFrom": [],
                "systemControls": [],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": log_group,
                        "awslogs-region": region,
                        "awslogs-stream-prefix": CONTAINER_NAME,
                        "mode": "non-blocking",
                    },
                },
            }
        ],
        sort_keys=True,
    )


def _run_task(
    *, cluster: str, task_definition: str, subnet: str, security_group: str, command: str
) -> dict[str, object]:
    environment: list[dict[str, str]] = [
        {"Name": "PROVISION_COMMAND", "Value": command},
        {"Name": "PROVISION_REQUEST", "Value.$": "States.JsonToString($.request)"},
    ]
    if command == "fail":
        environment.append({"Name": "PROVISION_ERROR", "Value.$": "$.error.Error"})
    return {
        "Type": "Task",
        "Resource": "arn:aws:states:::ecs:runTask.sync",
        "Parameters": {
            "Cluster": cluster,
            "TaskDefinition": task_definition,
            "LaunchType": "FARGATE",
            "PlatformVersion": "LATEST",
            "NetworkConfiguration": {
                "AwsvpcConfiguration": {
                    "Subnets": [subnet],
                    "SecurityGroups": [security_group],
                    "AssignPublicIp": "DISABLED",
                }
            },
            "Overrides": {
                "ContainerOverrides": [{"Name": CONTAINER_NAME, "Environment": environment}]
            },
        },
        "ResultPath": None,
    }


def state_machine_definition(
    *, cluster: str, task_definition: str, subnet: str, security_group: str
) -> str:
    """Input: `{"request": {...}}` (assay_tss.provision.task.ProvisionRequest).

    The execution succeeds only if the tenant ended `active`. If the task fails, the catch
    runs it once more to record the failure (a no-op when the task already did), and the
    execution ends as failed either way.
    """
    common = {
        "cluster": cluster,
        "task_definition": task_definition,
        "subnet": subnet,
        "security_group": security_group,
    }
    return json.dumps(
        {
            "Comment": "Provision one tenant: storage, tenant_storage row, first admin",
            "StartAt": "Provision",
            "TimeoutSeconds": TASK_TIMEOUT_SECONDS + FAIL_TIMEOUT_SECONDS + 300,
            "States": {
                "Provision": {
                    **_run_task(**common, command="provision"),
                    "TimeoutSeconds": TASK_TIMEOUT_SECONDS,
                    "Catch": [
                        {
                            "ErrorEquals": ["States.ALL"],
                            "ResultPath": "$.error",
                            "Next": "RecordFailure",
                        }
                    ],
                    "End": True,
                },
                "RecordFailure": {
                    **_run_task(**common, command="fail"),
                    "TimeoutSeconds": FAIL_TIMEOUT_SECONDS,
                    "Next": "Failed",
                },
                "Failed": {
                    "Type": "Fail",
                    "Error": "ProvisioningFailed",
                    "Cause": "Tenant provisioning failed. See audit_log and the task's logs.",
                },
            },
        },
        sort_keys=True,
    )


# --- Component ----------------------------------------------------------------------------


class Provisioning(pulumi.ComponentResource):
    def __init__(
        self,
        name: str,
        *,
        repo_root: Path,
        region: str,
        account_id: pulumi.Input[str],
        name_prefix: str,
        network: Network,
        database: Database,
        database_name: str,
        auth: Auth,
        storage: TenantStorage,
        vector_dimensions: int,
        default_tags: Mapping[str, str],
        # Profile IDs of the models a tenant starts with (stack config), set at activation.
        default_models: tuple[str, str],
        # Origins that may use presigned links in a tenant's bucket (build step 6).
        upload_origins: Sequence[str],
        # Every other KMS key the platform stack creates. The provisioning role is denied
        # tagging them (and the state key, which this component adds itself).
        platform_key_arns: Sequence[pulumi.Input[str]],
        # The SNS topic alarms notify (assay_infra.alerts).
        alarm_topic_arn: pulumi.Input[str],
        aws_profile: str | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:tenancy:Provisioning", name, None, opts)
        child = pulumi.ResourceOptions(parent=self)
        protected = pulumi.ResourceOptions(parent=self, protect=True)
        account = pulumi.Output.from_input(account_id)
        # Every tag key a tenant key is created with: the provider's default tags and the
        # tenant ID (assay_tss.provision.program).
        tenant_key_tag_keys = sorted({*default_tags, policies.TENANT_TAG})

        # --- Roles (policies come later; the key and bucket policies need the ARNs) -------
        ecs_trust = account.apply(lambda acct: _ecs_tasks_trust(acct, region))
        self.task_role = aws.iam.Role(
            f"{name}-task-role",
            description="Tenant provisioning: settings of <prefix>-t-* resources, never data",
            assume_role_policy=ecs_trust,
            opts=child,
        )
        self.execution_role = aws.iam.Role(
            f"{name}-execution-role",
            description="Tenant provisioning task execution: pull the image, write logs",
            assume_role_policy=ecs_trust,
            opts=child,
        )
        self.scan_role = aws.iam.Role(
            f"{name_prefix}-malware-scan-role",
            description="GuardDuty Malware Protection for S3: scan and tag tenant objects",
            assume_role_policy=account.apply(scan_role_trust_policy),
            opts=child,
        )
        aws.iam.RolePolicy(
            f"{name_prefix}-malware-scan-policy",
            role=self.scan_role.id,
            policy=account.apply(
                lambda acct: scan_role_policy(
                    region=region, account_id=acct, name_prefix=name_prefix
                )
            ),
            opts=child,
        )

        # --- Tenant stack state -------------------------------------------------------------
        self.state_key = aws.kms.Key(
            f"{name}-state-key",
            description=f"{name}: tenant stack state and secrets",
            enable_key_rotation=True,
            rotation_period_in_days=365,
            deletion_window_in_days=30,
            policy=pulumi.Output.all(account=account, role=self.task_role.arn).apply(
                lambda a: state_key_policy(account_id=a["account"], provisioning_role=a["role"])
            ),
            opts=protected,
        )
        aws.kms.Alias(
            f"{name}-state-key-alias",
            name=f"alias/{name}-state",
            target_key_id=self.state_key.key_id,
            opts=child,
        )
        self.state_bucket = aws.s3.Bucket(
            f"{name}-state",
            force_destroy=False,
            opts=protected,
        )
        state_ownership = aws.s3.BucketOwnershipControls(
            f"{name}-state-ownership",
            bucket=self.state_bucket.id,
            rule=aws.s3.BucketOwnershipControlsRuleArgs(object_ownership="BucketOwnerEnforced"),
            opts=child,
        )
        state_public_access = aws.s3.BucketPublicAccessBlock(
            f"{name}-state-public-access",
            bucket=self.state_bucket.id,
            block_public_acls=True,
            block_public_policy=True,
            ignore_public_acls=True,
            restrict_public_buckets=True,
            opts=child,
        )
        state_versioning = aws.s3.BucketVersioning(
            f"{name}-state-versioning",
            bucket=self.state_bucket.id,
            versioning_configuration=aws.s3.BucketVersioningVersioningConfigurationArgs(
                status="Enabled"
            ),
            opts=child,
        )
        state_encryption = aws.s3.BucketServerSideEncryptionConfiguration(
            f"{name}-state-encryption",
            bucket=self.state_bucket.id,
            rules=[
                aws.s3.BucketServerSideEncryptionConfigurationRuleArgs(
                    apply_server_side_encryption_by_default=aws.s3.BucketServerSideEncryptionConfigurationRuleApplyServerSideEncryptionByDefaultArgs(
                        sse_algorithm="aws:kms", kms_master_key_id=self.state_key.arn
                    ),
                    bucket_key_enabled=True,
                    blocked_encryption_types=list(BLOCKED_ENCRYPTION_TYPES),
                )
            ],
            # Last of the bucket's settings: see BLOCKED_ENCRYPTION_TYPES.
            opts=pulumi.ResourceOptions(
                parent=self, depends_on=[state_ownership, state_public_access, state_versioning]
            ),
        )
        aws.s3.BucketPolicy(
            f"{name}-state-policy",
            bucket=self.state_bucket.id,
            policy=pulumi.Output.all(bucket=self.state_bucket.arn, role=self.task_role.arn).apply(
                lambda a: state_bucket_policy(bucket=a["bucket"], provisioning_role=a["role"])
            ),
            # One bucket setting at a time: S3 rejects concurrent changes to a new bucket.
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[
                    state_ownership,
                    state_public_access,
                    state_versioning,
                    state_encryption,
                ],
            ),
        )

        # --- Cognito admin Lambda (outside the VPC) -------------------------------------------
        invite_name = f"{name_prefix}-cognito-admin"
        self.invite_image = ContainerImage(
            invite_name,
            repo_root=repo_root,
            dockerfile=INVITE_DOCKERFILE,
            files=INVITE_SOURCES,
            region=region,
            account_id=account_id,
            lambda_pull=True,
            aws_profile=aws_profile,
            opts=child,
        )
        invite_log_group = aws.cloudwatch.LogGroup(
            f"{invite_name}-logs",
            name=f"/aws/lambda/{invite_name}",
            retention_in_days=LOG_RETENTION_DAYS,
            opts=child,
        )
        invite_role = aws.iam.Role(
            f"{invite_name}-role",
            description="Cognito admin Lambda: invite tenant-pool users",
            assume_role_policy=account.apply(
                lambda acct: _service_trust("lambda.amazonaws.com", acct)
            ),
            opts=child,
        )
        invite_policy = aws.iam.RolePolicy(
            f"{invite_name}-policy",
            role=invite_role.id,
            policy=pulumi.Output.all(
                log_group=invite_log_group.arn, user_pool=auth.tenant.pool.arn
            ).apply(lambda a: invite_role_policy(**a)),
            opts=child,
        )
        # No resource-based permission is ever added to this function: the only way to
        # invoke it is an IAM policy, and only the provisioning role has one.
        self.invite_function = aws.lambda_.Function(
            f"{invite_name}-fn",
            name=invite_name,
            description="Cognito admin: invites tenant-pool users (first admin of a new tenant)",
            role=invite_role.arn,
            package_type="Image",
            image_uri=self.invite_image.image_uri,
            architectures=["x86_64"],
            memory_size=256,
            timeout=INVITE_TIMEOUT_SECONDS,
            environment=aws.lambda_.FunctionEnvironmentArgs(
                variables={"TENANT_POOL_ID": auth.tenant.pool.id, "OPERATIONS": "invite"}
            ),
            logging_config=aws.lambda_.FunctionLoggingConfigArgs(
                log_format="JSON",
                log_group=invite_log_group.name,
                application_log_level="INFO",
                system_log_level="WARN",
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[invite_policy]),
        )
        # The `lambda` endpoint's policy is written by lambda_endpoint_access(), once every
        # other function invoked from the VPC exists.
        self._name = name
        self._lambda_endpoint = network.interface_endpoints["lambda"]

        # --- The task -------------------------------------------------------------------------
        self.task_role_policy = aws.iam.RolePolicy(
            f"{name}-task-policy",
            role=self.task_role.id,
            policy=pulumi.Output.all(
                account_id=account,
                platform_keys=pulumi.Output.all(self.state_key.arn, *platform_key_arns),
                state_bucket=self.state_bucket.arn,
                state_key=self.state_key.arn,
                scan_role=self.scan_role.arn,
                invite_function=self.invite_function.arn,
                db_user=db_user_arn(
                    region, account_id, database.cluster_resource_id, PLATFORM_WRITER_DB_USER
                ),
            ).apply(
                lambda a: provisioning_role_policy(
                    region=region, name_prefix=name_prefix, tag_keys=tenant_key_tag_keys, **a
                )
            ),
            opts=child,
        )
        self.image = ContainerImage(
            name,
            repo_root=repo_root,
            dockerfile=TASK_DOCKERFILE,
            files=WORKSPACE_MANIFESTS,
            trees=TASK_IMAGE_TREES,
            region=region,
            account_id=account_id,
            aws_profile=aws_profile,
            opts=child,
        )
        self.log_group = aws.cloudwatch.LogGroup(
            f"{name}-logs",
            name=f"/aws/ecs/{name}",
            retention_in_days=LOG_RETENTION_DAYS,
            opts=child,
        )
        aws.iam.RolePolicy(
            f"{name}-execution-policy",
            role=self.execution_role.id,
            policy=pulumi.Output.all(
                repository=self.image.repository.arn, log_group=self.log_group.arn
            ).apply(lambda a: execution_role_policy(**a)),
            opts=child,
        )
        self.cluster = aws.ecs.Cluster(
            f"{name}-cluster",
            name=name,
            settings=[aws.ecs.ClusterSettingArgs(name="containerInsights", value="enabled")],
            opts=child,
        )
        environment = pulumi.Output.all(
            ACCOUNT_ID=account,
            TENANT_ROLE_ARN=storage.tenant_role.arn,
            BREAK_GLASS_ROLE_ARN=storage.break_glass.role.arn,
            PROVISIONING_ROLE_ARN=self.task_role.arn,
            SCAN_ROLE_ARN=self.scan_role.arn,
            STATE_BUCKET=self.state_bucket.bucket,
            STATE_KEY_ARN=self.state_key.arn,
            INVITE_FUNCTION=self.invite_function.arn,
            DB_HOST=database.endpoint,
            DB_PORT=database.port.apply(str),
        ).apply(
            lambda env: {
                **env,
                "AWS_REGION": region,
                "NAME_PREFIX": name_prefix,
                "VECTOR_DIMENSIONS": str(vector_dimensions),
                "DEFAULT_TAGS": json.dumps(dict(sorted(default_tags.items()))),
                "DEFAULT_INGEST_MODEL": default_models[0],
                "DEFAULT_QUERY_MODEL": default_models[1],
                # Bucket CORS (build step 6): the tenant portal, which uploads and views.
                "UPLOAD_ORIGINS": json.dumps(sorted(upload_origins)),
                "DB_NAME": database_name,
            }
        )
        self.task_definition = aws.ecs.TaskDefinition(
            f"{name}-task",
            family=name,
            cpu=TASK_CPU,
            memory=TASK_MEMORY,
            network_mode="awsvpc",
            requires_compatibilities=["FARGATE"],
            runtime_platform=aws.ecs.TaskDefinitionRuntimePlatformArgs(
                cpu_architecture="X86_64", operating_system_family="LINUX"
            ),
            execution_role_arn=self.execution_role.arn,
            task_role_arn=self.task_role.arn,
            volumes=[aws.ecs.TaskDefinitionVolumeArgs(name=WORK_VOLUME)],
            container_definitions=pulumi.Output.all(
                image=self.image.image_uri, log_group=self.log_group.name, environment=environment
            ).apply(lambda a: container_definitions(region=region, **a)),
            opts=child,
        )

        # --- The state machine --------------------------------------------------------------
        self.state_machine_log_group = aws.cloudwatch.LogGroup(
            f"{name}-sfn-logs",
            name=f"/aws/vendedlogs/states/{name}",
            retention_in_days=LOG_RETENTION_DAYS,
            opts=child,
        )
        state_machine_role = aws.iam.Role(
            f"{name}-sfn-role",
            description="Provisioning state machine: run the provisioning task",
            assume_role_policy=account.apply(
                lambda acct: _service_trust("states.amazonaws.com", acct)
            ),
            opts=child,
        )
        state_machine_policy = aws.iam.RolePolicy(
            f"{name}-sfn-policy",
            role=state_machine_role.id,
            policy=pulumi.Output.all(
                account_id=account,
                cluster_name=self.cluster.name,
                cluster=self.cluster.arn,
                task_family=self.task_definition.family,
                task_role=self.task_role.arn,
                execution_role=self.execution_role.arn,
            ).apply(lambda a: state_machine_role_policy(region=region, **a)),
            opts=child,
        )
        self.state_machine = aws.sfn.StateMachine(
            f"{name}-sfn",
            name=name,
            type="STANDARD",
            role_arn=state_machine_role.arn,
            definition=pulumi.Output.all(
                cluster=self.cluster.arn,
                task_definition=self.task_definition.arn,
                subnet=network.workload_subnet.id,
                security_group=network.workload_security_group.id,
            ).apply(lambda a: state_machine_definition(**a)),
            # Errors only, and never the execution's input (the first admin's name and email).
            logging_configuration=aws.sfn.StateMachineLoggingConfigurationArgs(
                level="ERROR",
                include_execution_data=False,
                log_destination=pulumi.Output.concat(self.state_machine_log_group.arn, ":*"),
            ),
            opts=pulumi.ResourceOptions(
                parent=self, depends_on=[state_machine_policy, self.task_role_policy]
            ),
        )
        self.failure_alarm = aws.cloudwatch.MetricAlarm(
            f"{name}-failed-alarm",
            name=f"{name}-failed",
            alarm_description="A tenant provisioning execution failed, timed out or was aborted.",
            alarm_actions=[alarm_topic_arn],
            metric_queries=[
                aws.cloudwatch.MetricAlarmMetricQueryArgs(
                    id="failures",
                    expression=FAILURES_EXPRESSION,
                    label="Unsuccessful executions",
                    return_data=True,
                ),
                *[
                    aws.cloudwatch.MetricAlarmMetricQueryArgs(
                        id=query_id,
                        metric=aws.cloudwatch.MetricAlarmMetricQueryMetricArgs(
                            namespace="AWS/States",
                            metric_name=metric,
                            dimensions={"StateMachineArn": self.state_machine.arn},
                            stat="Sum",
                            period=300,
                        ),
                    )
                    for query_id, metric in (
                        ("failed", "ExecutionsFailed"),
                        ("timed_out", "ExecutionsTimedOut"),
                        ("aborted", "ExecutionsAborted"),
                    )
                ],
            ],
            evaluation_periods=1,
            threshold=1,
            comparison_operator="GreaterThanOrEqualToThreshold",
            treat_missing_data="notBreaching",
            opts=child,
        )

        self.register_outputs(
            {
                "state_machine_arn": self.state_machine.arn,
                "state_bucket": self.state_bucket.bucket,
                "provisioning_role_arn": self.task_role.arn,
            }
        )

    def lambda_endpoint_access(
        self, grants: Sequence[tuple[str, pulumi.Input[str], Sequence[pulumi.Input[str]]]]
    ) -> aws.ec2.VpcEndpointPolicy:
        """The `lambda` endpoint's policy: the first-admin invite plus these grants.

        Created here, with the name and parent it has had since build step 4, so it is the
        same resource: deleting a VPC endpoint policy resets the endpoint to allow all.
        """
        callers = [pulumi.Output.all(*roles) for _, _, roles in grants]
        functions = [function for _, function, _ in grants]
        sids = [sid for sid, _, _ in grants]
        self.lambda_endpoint_policy = aws.ec2.VpcEndpointPolicy(
            f"{self._name}-lambda-endpoint-policy",
            vpc_endpoint_id=self._lambda_endpoint.id,
            policy=pulumi.Output.all(
                invite_function=self.invite_function.arn,
                provisioning_role=self.task_role.arn,
                functions=pulumi.Output.all(*functions),
                callers=pulumi.Output.all(*callers),
            ).apply(
                lambda a: lambda_endpoint_policy(
                    invite_function=a["invite_function"],
                    provisioning_role=a["provisioning_role"],
                    grants=list(zip(sids, a["functions"], a["callers"], strict=True)),
                )
            ),
            opts=pulumi.ResourceOptions(parent=self),
        )
        return self.lambda_endpoint_policy
