"""The ingestion pipeline (BUILD_SPEC.md sections 4 and 8; build step 6).

    GuardDuty scan result ──EventBridge──> scan Lambda ──┐
    POST /batches/{id}/close (API) ──────────────────────┼──> ingest queue ──> starter Lambda
    sweeper Lambda (every 5 minutes) ────────────────────┘        │                 │
                                                       dead-letter queue    one execution per PDF
                                                                                    │
       pipeline state machine: fingerprint ──> claim ──> worker task ──> release
                                   │             │  ^        │
                                   │             └──┘ wait   └─> release ─┬─> failed (catch)
                                   └─> failed (catch)   (no slot; or the  └─> wait, claim again
                                                         task never ran)      (it never ran)

- **Scan Lambda** (`assay_workers.scan`): EventBridge invokes it for GuardDuty Malware
  Protection Object Scan Result events about tenant buckets. Lambda retries a failed event
  twice, then sends it to the scan dead-letter queue (as does EventBridge, if it cannot
  deliver it). Both dead-letter queues raise an alarm when anything arrives.
- **Ingest queue**: one job per eligible PDF (IDs only). TLS only; only the API, the scan
  Lambda and the sweeper may send (its policy and the `sqs` endpoint's policy). A job that
  fails five times stays in its dead-letter queue.
- **Starter** (`assay_workers.starter`): reads the queue and starts one execution per
  document, named after it, through the `states` endpoint. A suspended tenant's documents
  are held (owner decision 19).
- **State machine**: Standard, logging errors only and never execution data. Its Lambda steps
  run in the step Lambda (`assay_workers.steps`); any failure goes to the `failed` step,
  which marks the document failed with a short code, and the execution fails (alarmed).
- **Worker task** (`assay_workers.worker`, second half of build step 6): one Fargate task per
  document's attempt, started by the state machine with `ecs:runTask.sync`. It is the only
  part of the pipeline that opens a PDF, and it does so in a sandbox child
  (`assay_workers.sandbox`). The `claim` step first takes one of the tenant's worker slots
  and creates the attempt grant, whose one-time token the state machine passes to that one
  task; `release` ends the grant afterwards. A task that never started (no Fargate capacity,
  for example) is not the document's failure: the state machine waits and claims again.
- **Sweeper** (`assay_workers.sweeper`), every five minutes (EventBridge Scheduler).

Every function runs in the VPC as database role `app_tenant` and reaches the TSS through
the `execute-api` endpoint with its own caller name and access (`TssCaller`): the scan
Lambda, the step Lambda and the sweeper get `PIPELINE` only; the starter is no TSS caller.
No function has any S3, KMS or AI-service permission of its own: object access is through
TSS-issued tenant credentials only. Each function's ERROR lines raise an alarm (an
unregistered scan key, a scan that never arrived, a job that could not be queued), as do
Lambda errors.

The worker task's role has even less (owner conditions A and B, 2026-11-24). It may connect
to the database as `app_worker` only, whose sessions see one document while its grant is
live, and call the TSS's `/resolve-worker` route only, which issues credentials for that one
document's keys. It has no S3, KMS or queue permission and, in part 1, no AI-service
permission (Textract and Bedrock arrive with the stages that use them). Its container runs
as root with every capability dropped except SETUID, SETGID and KILL, which starting and
stopping the sandbox child as another user needs, on a read-only root filesystem.

**The grant's token is visible** in three places, to principals that can read them: the
execution's history (Step Functions keeps the state), the task's description (the container
overrides), and CloudTrail (the `RunTask` request parameters). This is accepted (owner,
2026-11-24): the token is useless without the worker task's role, which alone may present it
to the TSS or sign in as `app_worker`, and it is dead when the attempt ends (`release`) or
the grant expires. The state machine's own log still records no execution data.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pulumi
import pulumi_aws as aws

from assay_infra.api import _ecs_tasks_trust, execution_role_policy
from assay_infra.database import Database, db_user_arn
from assay_infra.images import WORKSPACE_MANIFESTS, ContainerImage
from assay_infra.network import Network
from assay_infra.tss import VPC_NETWORK_INTERFACE_STATEMENT, _service_trust

APP_TENANT_DB_USER = "app_tenant"  # assay_db.roles.APP_TENANT

DOCKERFILE = "workers/lambda.Dockerfile"
IMAGE_TREES = ("db/assay_db", "schemas/assay_schemas", "tss/assay_tss", "workers/assay_workers")

# The TSS caller names (source identities `<service>.<job>`), assay_workers.*.SERVICE.
SCAN_SERVICE = "scan"
STEPS_SERVICE = "pipeline"
SWEEPER_SERVICE = "sweeper"
# The worker task: `/resolve-worker` only (assay_tss.service.Resolver.resolve_worker).
WORKER_SERVICE = "worker"

# --- The worker task (build step 6, second half) -------------------------------------------
WORKER_DB_USER = "app_worker"  # assay_db.roles.APP_WORKER
WORKER_DOCKERFILE = "workers/worker.Dockerfile"
WORKER_CONTAINER = "worker"
# Root, on purpose (workers/worker.Dockerfile): the task's main process starts each sandbox
# child as another user. Its capabilities are cut to the three below.
WORKER_USER = "0:0"
WORKER_CPU = "1024"
WORKER_MEMORY = "4096"
WORK_VOLUME = "work"
WORK_DIR = "/work"  # workers/worker.Dockerfile
WORKER_TIMEOUT_SECONDS = 1800
# A grant lasts as long as its task may, plus the time a task takes to start.
WORKER_GRANT_SECONDS = WORKER_TIMEOUT_SECONDS + 600
# What the task's main process keeps: to switch a child to the sandbox user and group, and
# to stop it. Fargate cannot add capabilities, so "drop ALL, add three" is not available:
# every other capability is dropped by name.
WORKER_CAPABILITIES = frozenset({"SETUID", "SETGID", "KILL"})
ALL_CAPABILITIES = (
    "AUDIT_CONTROL",
    "AUDIT_WRITE",
    "BLOCK_SUSPEND",
    "CHOWN",
    "DAC_OVERRIDE",
    "DAC_READ_SEARCH",
    "FOWNER",
    "FSETID",
    "IPC_LOCK",
    "IPC_OWNER",
    "KILL",
    "LEASE",
    "LINUX_IMMUTABLE",
    "MAC_ADMIN",
    "MAC_OVERRIDE",
    "MKNOD",
    "NET_ADMIN",
    "NET_BIND_SERVICE",
    "NET_BROADCAST",
    "NET_RAW",
    "SETFCAP",
    "SETGID",
    "SETPCAP",
    "SETUID",
    "SYS_ADMIN",
    "SYS_BOOT",
    "SYS_CHROOT",
    "SYS_MODULE",
    "SYS_NICE",
    "SYS_PACCT",
    "SYS_PTRACE",
    "SYS_RAWIO",
    "SYS_RESOURCE",
    "SYS_TIME",
    "SYS_TTY_CONFIG",
    "SYSLOG",
    "WAKE_ALARM",
)
DROPPED_CAPABILITIES = tuple(c for c in ALL_CAPABILITIES if c not in WORKER_CAPABILITIES)
# What the task reads from its environment. The document, attempt and token are not here:
# Step Functions passes those to each task as overrides.
WORKER_VARIABLES = (
    "AWS_REGION",
    "NAME_PREFIX",
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "TSS_ENDPOINT_URL",
)
# How long a document waits before it asks again for a worker slot, or for Fargate capacity.
SLOT_WAIT_SECONDS = 30
CAPACITY_WAIT_SECONDS = 60
# One execution may wait for a slot or for capacity for this long. A Standard execution
# keeps at most 25,000 history events; at about 13 a wait this stays well inside it.
EXECUTION_TIMEOUT_SECONDS = 12 * 3600

LOG_RETENTION_DAYS = 30
METRIC_NAMESPACE = "Assay/Pipeline"
# Lambda's JSON log format puts the level in `level`.
ERROR_LINES = '{ $.level = "ERROR" }'
DLQ_RETENTION_SECONDS = 14 * 24 * 3600
MAX_RECEIVES = 5
SWEEP_SCHEDULE = "rate(5 minutes)"
# GuardDuty's event for Malware Protection for S3.
SCAN_EVENT_SOURCE = "aws.guardduty"
SCAN_EVENT_DETAIL_TYPE = "GuardDuty Malware Protection Object Scan Result"

SEND_ACTIONS = ("sqs:SendMessage",)
CONSUME_ACTIONS = (
    "sqs:ReceiveMessage",
    "sqs:DeleteMessage",
    "sqs:ChangeMessageVisibility",
    "sqs:GetQueueAttributes",
)


@dataclass(frozen=True)
class FunctionSpec:
    handler: str
    description: str
    memory: int
    timeout: int
    # /tmp, in MB. The step Lambda downloads a PDF of up to 100 MB there.
    ephemeral: int = 512


FUNCTIONS: Mapping[str, FunctionSpec] = {
    "scan": FunctionSpec(
        "assay_workers.scan.handler", "Record GuardDuty scan results for uploads", 512, 60
    ),
    "starter": FunctionSpec(
        "assay_workers.starter.handler", "Start one pipeline execution per eligible PDF", 256, 30
    ),
    "steps": FunctionSpec(
        "assay_workers.steps.handler",
        "Pipeline steps: fingerprint (step 4) and the failure handler",
        2048,
        300,
        ephemeral=1024,
    ),
    "sweeper": FunctionSpec(
        "assay_workers.sweeper.handler",
        "Close stale batches, time out scans, queue unstarted documents",
        512,
        240,
    ),
}
# AWS recommends a visibility timeout of six times the function's timeout for SQS sources.
INGEST_VISIBILITY_SECONDS = 6 * FUNCTIONS["starter"].timeout


def _json(document: object) -> str:
    return json.dumps(document, separators=(",", ":"))


@dataclass(frozen=True)
class PipelineRoles:
    """Created before the TSS, which names each of them as a caller."""

    scan: aws.iam.Role
    starter: aws.iam.Role
    steps: aws.iam.Role
    sweeper: aws.iam.Role
    # The worker task's role: the only caller of the TSS's /resolve-worker route.
    worker: aws.iam.Role

    def by_function(self) -> dict[str, aws.iam.Role]:
        return {
            "scan": self.scan,
            "starter": self.starter,
            "steps": self.steps,
            "sweeper": self.sweeper,
        }


def pipeline_roles(name: str, account_id: pulumi.Input[str], region: str) -> PipelineRoles:
    account = pulumi.Output.from_input(account_id)
    trust = account.apply(lambda acct: _service_trust("lambda.amazonaws.com", acct))

    def role(function: str, description: str) -> aws.iam.Role:
        return aws.iam.Role(
            f"{name}-{function}-role", description=description, assume_role_policy=trust
        )

    return PipelineRoles(
        scan=role("scan", "Pipeline scan results: record them, quarantine infected uploads"),
        starter=role("starter", "Pipeline starter: one execution per eligible document"),
        steps=role("steps", "Pipeline steps: fingerprint and failure handling"),
        sweeper=role("sweeper", "Pipeline sweeper: stale batches, overdue scans, requeues"),
        worker=aws.iam.Role(
            f"{name}-worker-task-role",
            description="Pipeline worker task: one document per task, bound to its grant",
            assume_role_policy=account.apply(lambda acct: _ecs_tasks_trust(acct, region)),
        ),
    )


def worker_role_policy(*, db_user: str) -> str:
    """The worker task's own permissions: sign in to the database as `app_worker`, and
    nothing else. Its one TSS route is granted by TenantStorage; it has no S3, KMS, queue or
    AI-service permission (Textract and Bedrock arrive with the stages that use them)."""
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "ConnectAsAppWorker",
                    "Effect": "Allow",
                    "Action": "rds-db:connect",
                    "Resource": db_user,
                }
            ],
        }
    )


class WorkerEnvironmentError(ValueError):
    """The worker task would start without what it needs. Fails the preview."""


def worker_container_definitions(
    *, image: str, log_group: str, region: str, environment: Mapping[str, str]
) -> str:
    """Written exactly as ECS stores a container definition (see assay_infra.api)."""
    missing = [name for name in WORKER_VARIABLES if not environment.get(name)]
    if missing:
        raise WorkerEnvironmentError(f"worker task environment lacks {', '.join(missing)}")
    return json.dumps(
        [
            {
                "name": WORKER_CONTAINER,
                "image": image,
                "cpu": 0,
                "essential": True,
                "user": WORKER_USER,
                "readonlyRootFilesystem": True,
                "privileged": False,
                "portMappings": [],
                "environment": [{"name": k, "value": v} for k, v in sorted(environment.items())],
                "linuxParameters": {
                    "capabilities": {"add": [], "drop": list(DROPPED_CAPABILITIES)}
                },
                # The task's only writable directory: the PDF being inspected.
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
                        "awslogs-stream-prefix": WORKER_CONTAINER,
                        "mode": "non-blocking",
                    },
                },
            }
        ],
        sort_keys=True,
    )


def _logs(log_group: str) -> dict[str, object]:
    return {
        "Sid": "Logs",
        "Effect": "Allow",
        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": f"{log_group}:*",
    }


def _db(db_user: str) -> dict[str, object]:
    return {
        "Sid": "ConnectAsAppTenant",
        "Effect": "Allow",
        "Action": "rds-db:connect",
        "Resource": db_user,
    }


def function_role_policy(
    function: str,
    *,
    log_group: str,
    db_user: str,
    ingest_queue: str,
    scan_dlq: str,
    state_machine: str,
    security_topic: str,
    notices_key: str,
    region: str,
) -> str:
    """Each function's own permissions. The TSS invoke grant is added by TenantStorage."""
    statements: list[dict[str, object]] = [
        _logs(log_group),
        dict(VPC_NETWORK_INTERFACE_STATEMENT),
        _db(db_user),
    ]
    if function in ("scan", "sweeper"):
        statements.append(
            {
                "Sid": "QueueIngestJobs",
                "Effect": "Allow",
                "Action": list(SEND_ACTIONS),
                "Resource": ingest_queue,
            }
        )
    if function == "scan":
        # Lambda's on-failure destination sends with the function's own role.
        statements.append(
            {
                "Sid": "FailedEventsToTheDeadLetterQueue",
                "Effect": "Allow",
                "Action": list(SEND_ACTIONS),
                "Resource": scan_dlq,
            }
        )
        # The quarantine notice (owner decision 4, second half): publish to the one
        # security-notice topic, and use its key only through SNS, which encrypts the
        # message with it. The role cannot call KMS with this key itself.
        statements += [
            {
                "Sid": "PublishSecurityNotices",
                "Effect": "Allow",
                "Action": "sns:Publish",
                "Resource": security_topic,
            },
            {
                "Sid": "NoticeKeyThroughSnsOnly",
                "Effect": "Allow",
                "Action": ["kms:GenerateDataKey*", "kms:Decrypt"],
                "Resource": notices_key,
                "Condition": {"StringEquals": {"kms:ViaService": f"sns.{region}.amazonaws.com"}},
            },
        ]
    if function == "starter":
        statements += [
            {
                "Sid": "ConsumeIngestJobs",
                "Effect": "Allow",
                "Action": list(CONSUME_ACTIONS),
                "Resource": ingest_queue,
            },
            {
                "Sid": "StartDocumentIngestion",
                "Effect": "Allow",
                "Action": "states:StartExecution",
                "Resource": state_machine,
            },
        ]
    return _json({"Version": "2012-10-17", "Statement": statements})


def _role_policy(function: str, values: dict[str, str]) -> str:
    return function_role_policy(function, **values)


def ingest_queue_policy(*, queue: str, senders: list[str]) -> str:
    """Only the named senders may send. TLS only."""
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "OnlyNamedSenders",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": list(SEND_ACTIONS),
                    "Resource": queue,
                    "Condition": {"ArnNotEquals": {"aws:PrincipalArn": sorted(senders)}},
                },
                {
                    "Sid": "DenyInsecureTransport",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "sqs:*",
                    "Resource": queue,
                    "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                },
            ],
        }
    )


def scan_dlq_policy(*, queue: str, rule: str) -> str:
    """EventBridge may put events it could not deliver here, for this rule only. TLS only."""
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "EventBridgeDeadLetters",
                    "Effect": "Allow",
                    "Principal": {"Service": "events.amazonaws.com"},
                    "Action": list(SEND_ACTIONS),
                    "Resource": queue,
                    "Condition": {"ArnEquals": {"aws:SourceArn": rule}},
                },
                {
                    "Sid": "DenyInsecureTransport",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "sqs:*",
                    "Resource": queue,
                    "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                },
            ],
        }
    )


def scan_event_pattern(name_prefix: str) -> str:
    """Scan results for tenant buckets (`<prefix>-t-...`) only."""
    return _json(
        {
            "source": [SCAN_EVENT_SOURCE],
            "detail-type": [SCAN_EVENT_DETAIL_TYPE],
            "detail": {"s3ObjectDetails": {"bucketName": [{"prefix": f"{name_prefix}-t-"}]}},
        }
    )


# Lambda's own transient errors: always worth retrying.
LAMBDA_RETRY: dict[str, object] = {
    "ErrorEquals": [
        "Lambda.ServiceException",
        "Lambda.AWSLambdaException",
        "Lambda.SdkClientException",
        "Lambda.TooManyRequestsException",
    ],
    "IntervalSeconds": 5,
    "MaxAttempts": 4,
    "BackoffRate": 2,
}
# The step's own errors (the database resuming, S3, the TSS): twice more, then fail.
STEP_RETRY: dict[str, object] = {
    "ErrorEquals": ["States.TaskFailed"],
    "IntervalSeconds": 30,
    "MaxAttempts": 2,
    "BackoffRate": 2,
}


# The RunTask call itself failing (throttling, a service error): tried again here. A task
# that was accepted and then never ran is handled after the catch, by `release`.
ECS_RETRY: dict[str, object] = {
    "ErrorEquals": ["ECS.AmazonECSException", "ECS.ServerException", "ECS.ThrottlingException"],
    "IntervalSeconds": 30,
    "MaxAttempts": 6,
    "BackoffRate": 2,
    "MaxDelaySeconds": 300,
    "JitterStrategy": "FULL",
}


def state_machine_definition(
    *, steps_function: str, cluster: str, task_definition: str, subnet: str, security_group: str
) -> str:
    """Input: `{"tenant_id", "document_id", "attempt"}`, from the starter. Every Lambda step
    gets the attempt and does nothing unless it is still the document's current one.

    Fingerprint (Lambda), then the worker task for everything that needs the PDF opened, then
    `Companion` (Lambda, step 9: the companion file's values, plain code):

    - `Claim` takes one of the tenant's worker slots and creates the attempt grant. With no
      slot free it answers `wait`; the execution waits and asks again.
    - `Worker` runs one Fargate task and waits for it. The task gets the document, the
      attempt and the grant's token as overrides, and nothing else about the document. Its
      result is discarded: the task's description repeats those overrides.
    - `Release` ends the grant. After a failed task it also says whether the task ever
      started. If it never did (no capacity, an image that could not be pulled), that is not
      the document's failure: the execution waits and claims again. The execution's own
      timeout is the only limit, and it leaves the document `processing`, never `failed`.

    A step's error goes to its own Pass state, which sets `failed_step` to that step's name
    (fixed text from this definition, never from the error), then to `Failed`. `Failed` gets
    the step's name and the error's name (`$.error.Error`) only, never its `Cause` (a message
    or stack trace), records them on the document and fails the execution. For the worker
    task, `Failed` reads which of its steps was under way from the database.

    `$.claim` holds the token only between `Claim` and `Release`, which overwrites it.
    """

    def caught(step: str) -> dict[str, object]:
        return {
            "Type": "Pass",
            "Result": step,
            "ResultPath": "$.failed_step",
            "Next": "Failed",
        }

    def invoke(step: str, extra: Mapping[str, object] | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "step": step,
            "tenant_id.$": "$.tenant_id",
            "document_id.$": "$.document_id",
            "attempt.$": "$.attempt",
            **(extra or {}),
        }
        return {
            "Type": "Task",
            "Resource": "arn:aws:states:::lambda:invoke",
            "Parameters": {"FunctionName": steps_function, "Payload": payload},
            "OutputPath": "$.Payload",
            "Retry": [LAMBDA_RETRY],
        }

    def step(name: str, *, result: str, failed: str, then: str) -> dict[str, object]:
        """A Lambda step whose whole answer goes to `result`, with its catch."""
        return {
            **invoke(name),
            "ResultSelector": {"result.$": "$.Payload"},
            "ResultPath": result,
            "OutputPath": "$",
            "TimeoutSeconds": FUNCTIONS["steps"].timeout + 60,
            "Retry": [LAMBDA_RETRY, STEP_RETRY],
            "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": failed}],
            "Next": then,
        }

    worker = {
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
                "ContainerOverrides": [
                    {
                        "Name": WORKER_CONTAINER,
                        "Environment": [
                            {"Name": "DOCUMENT_ID", "Value.$": "$.document_id"},
                            {
                                "Name": "ATTEMPT",
                                "Value.$": "States.Format('{}', $.claim.result.attempt)",
                            },
                            {"Name": "WORKER_TOKEN", "Value.$": "$.claim.result.token"},
                        ],
                    }
                ]
            },
        },
        # Discarded: the task's description repeats the overrides, the token among them.
        "ResultPath": None,
        "TimeoutSeconds": WORKER_TIMEOUT_SECONDS,
        "Retry": [ECS_RETRY],
        "Catch": [
            {"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "ReleaseAfterFailure"}
        ],
        "Next": "Release",
    }

    return json.dumps(
        {
            "Comment": "Ingest one PDF: fingerprint, then the worker task (build step 6)",
            "StartAt": "Fingerprint",
            "TimeoutSeconds": EXECUTION_TIMEOUT_SECONDS,
            "States": {
                "Fingerprint": {
                    **invoke("fingerprint"),
                    "ResultPath": "$.fingerprint",
                    "OutputPath": "$",
                    "ResultSelector": {"outcome.$": "$.Payload.outcome"},
                    "TimeoutSeconds": FUNCTIONS["steps"].timeout + 60,
                    "Retry": [LAMBDA_RETRY, STEP_RETRY],
                    "Catch": [
                        {
                            "ErrorEquals": ["States.ALL"],
                            "ResultPath": "$.error",
                            "Next": "FingerprintFailed",
                        }
                    ],
                    "Next": "Outcome",
                },
                "FingerprintFailed": caught("fingerprint"),
                "Outcome": {
                    "Type": "Choice",
                    "Choices": [
                        {
                            "Variable": "$.fingerprint.outcome",
                            "StringEquals": "continue",
                            "Next": "Claim",
                        },
                        {
                            "Variable": "$.fingerprint.outcome",
                            "StringEquals": "refused",
                            "Next": "Refused",
                        },
                    ],
                    # duplicate, failed: the document's status says why.
                    "Default": "Stopped",
                },
                "Claim": step("claim", result="$.claim", failed="ClaimFailed", then="Claimed"),
                "ClaimFailed": caught("claim"),
                "Claimed": {
                    "Type": "Choice",
                    "Choices": [
                        {
                            "Variable": "$.claim.result.outcome",
                            "StringEquals": "claimed",
                            "Next": "Worker",
                        },
                        {
                            "Variable": "$.claim.result.outcome",
                            "StringEquals": "wait",
                            "Next": "WaitForSlot",
                        },
                    ],
                    # The document is no longer to be processed (its batch was discarded).
                    "Default": "Stopped",
                },
                "WaitForSlot": {"Type": "Wait", "Seconds": SLOT_WAIT_SECONDS, "Next": "Claim"},
                "Worker": worker,
                # The task ended well. A release that fails leaves the grant to expire and
                # fails the execution (alarmed); the document's state is the task's.
                "Release": {
                    **invoke("release"),
                    "ResultSelector": {"result.$": "$.Payload"},
                    "ResultPath": "$.claim",
                    "OutputPath": "$",
                    "TimeoutSeconds": FUNCTIONS["steps"].timeout + 60,
                    "Retry": [LAMBDA_RETRY, STEP_RETRY],
                    "Next": "Companion",
                },
                # Step 9. It answers `stopped` for a document the task held or failed.
                "Companion": step(
                    "companion", result="$.companion", failed="CompanionFailed", then="Done"
                ),
                "CompanionFailed": caught("companion"),
                "ReleaseAfterFailure": step(
                    "release", result="$.claim", failed="WorkerFailed", then="WorkerStarted"
                ),
                "WorkerStarted": {
                    "Type": "Choice",
                    "Choices": [
                        {
                            "Variable": "$.claim.result.worker_started",
                            "BooleanEquals": True,
                            "Next": "WorkerFailed",
                        }
                    ],
                    # The task never ran: not the document's failure. Wait, and claim again.
                    "Default": "WaitForCapacity",
                },
                "WaitForCapacity": {
                    "Type": "Wait",
                    "Seconds": CAPACITY_WAIT_SECONDS,
                    "Next": "Claim",
                },
                "WorkerFailed": caught("worker"),
                "Done": {"Type": "Succeed"},
                "Stopped": {"Type": "Succeed"},
                "Refused": {
                    "Type": "Fail",
                    "Error": "Refused",
                    "Cause": "The reserved isolation-test document ID is never processed.",
                },
                "Failed": {
                    **invoke(
                        "failed",
                        {"failed_step.$": "$.failed_step", "error_type.$": "$.error.Error"},
                    ),
                    "TimeoutSeconds": FUNCTIONS["steps"].timeout + 60,
                    "Next": "FailedEnd",
                },
                "FailedEnd": {
                    "Type": "Fail",
                    "Error": "DocumentFailed",
                    "Cause": "The document failed. See its pipeline steps in ingestion history.",
                },
            },
        },
        sort_keys=True,
    )


def state_machine_role_policy(
    *,
    steps_function: str,
    region: str,
    account_id: str,
    cluster_name: str,
    cluster: str,
    task_family: str,
    task_role: str,
    execution_role: str,
) -> str:
    """Run the Lambda steps and the worker task, and nothing else. `iam:PassRole` is for the
    worker task's two roles only, and only to ECS tasks."""
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "RunTheSteps",
                    "Effect": "Allow",
                    "Action": "lambda:InvokeFunction",
                    "Resource": [steps_function, f"{steps_function}:*"],
                },
                {
                    "Sid": "RunTheWorkerTask",
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
                    "Sid": "PassTheWorkerTaskRoles",
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


def scheduler_role_policy(*, function: str) -> str:
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "RunTheSweeper",
                    "Effect": "Allow",
                    "Action": "lambda:InvokeFunction",
                    "Resource": function,
                }
            ],
        }
    )


class Pipeline(pulumi.ComponentResource):
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
        roles: PipelineRoles,
        tss_endpoint_url: pulumi.Input[str],
        # The API's task role: the only sender of ingest jobs besides the scan Lambda and the
        # sweeper (POST /batches/{id}/close).
        api_task_role: aws.iam.Role,
        alarm_topic_arn: pulumi.Input[str],
        # Where the scan function publishes a quarantine notice, and the key that topic is
        # encrypted with (assay_infra.alerts.SecurityNotices).
        security_topic_arn: pulumi.Input[str],
        notices_key_arn: pulumi.Input[str],
        # The cap on one tenant's concurrent worker tasks (stack config workerMaxPerTenant).
        worker_max_per_tenant: int = 5,
        # The scan function's reserved concurrency (stack config scanReservedConcurrency):
        # the most scan results handled at once. None: no reservation. No other function
        # here gets one: their callers are synchronous, and a throttled step fails its
        # document (the throttle test of 2026-11-24).
        scan_reserved_concurrency: int | None = None,
        aws_profile: str | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:pipeline:Pipeline", name, None, opts)
        self._alarm_topic_arn = alarm_topic_arn
        child = pulumi.ResourceOptions(parent=self)
        account = pulumi.Output.from_input(account_id)
        self.roles = roles

        # --- Queues -----------------------------------------------------------------------
        self.ingest_dlq = aws.sqs.Queue(
            f"{name}-ingest-dlq",
            name=f"{name}-ingest-dlq",
            sqs_managed_sse_enabled=True,
            message_retention_seconds=DLQ_RETENTION_SECONDS,
            opts=child,
        )
        self.ingest_queue = aws.sqs.Queue(
            f"{name}-ingest",
            name=f"{name}-ingest",
            sqs_managed_sse_enabled=True,
            visibility_timeout_seconds=INGEST_VISIBILITY_SECONDS,
            message_retention_seconds=DLQ_RETENTION_SECONDS,
            redrive_policy=self.ingest_dlq.arn.apply(
                lambda arn: _json({"deadLetterTargetArn": arn, "maxReceiveCount": MAX_RECEIVES})
            ),
            opts=child,
        )
        self.scan_dlq = aws.sqs.Queue(
            f"{name}-scan-dlq",
            name=f"{name}-scan-dlq",
            sqs_managed_sse_enabled=True,
            message_retention_seconds=DLQ_RETENTION_SECONDS,
            opts=child,
        )
        self.ingest_senders = [api_task_role, roles.scan, roles.sweeper]
        self.ingest_policy = aws.sqs.QueuePolicy(
            f"{name}-ingest-policy",
            queue_url=self.ingest_queue.url,
            policy=pulumi.Output.all(
                self.ingest_queue.arn, *[r.arn for r in self.ingest_senders]
            ).apply(lambda v: ingest_queue_policy(queue=v[0], senders=list(v[1:]))),
            opts=child,
        )

        # --- Image and functions ----------------------------------------------------------
        self.image = ContainerImage(
            name,
            repo_root=repo_root,
            dockerfile=DOCKERFILE,
            files=WORKSPACE_MANIFESTS,
            trees=IMAGE_TREES,
            region=region,
            account_id=account_id,
            lambda_pull=True,
            aws_profile=aws_profile,
            opts=child,
        )
        db_user = db_user_arn(region, account_id, database.cluster_resource_id, APP_TENANT_DB_USER)
        # The state machine's ARN, built from its name, so the starter's policy and
        # environment need not wait for it (and the state machine names the step function).
        self.state_machine_name = name
        state_machine_arn = account.apply(
            lambda acct: f"arn:aws:states:{region}:{acct}:stateMachine:{name}"
        )
        self.log_groups: dict[str, aws.cloudwatch.LogGroup] = {}
        self.functions: dict[str, aws.lambda_.Function] = {}
        self.alarms: list[aws.cloudwatch.MetricAlarm] = []
        for function, spec in FUNCTIONS.items():
            fn_name = f"{name}-{function}"
            role = roles.by_function()[function]
            log_group = aws.cloudwatch.LogGroup(
                f"{fn_name}-logs",
                name=f"/aws/lambda/{fn_name}",
                retention_in_days=LOG_RETENTION_DAYS,
                opts=child,
            )
            policy = aws.iam.RolePolicy(
                f"{fn_name}-policy",
                role=role.id,
                policy=pulumi.Output.all(
                    log_group=log_group.arn,
                    db_user=db_user,
                    ingest_queue=self.ingest_queue.arn,
                    scan_dlq=self.scan_dlq.arn,
                    state_machine=state_machine_arn,
                    security_topic=security_topic_arn,
                    notices_key=notices_key_arn,
                    region=region,
                ).apply(functools.partial(_role_policy, function)),
                opts=child,
            )
            variables: dict[str, pulumi.Input[str]] = {
                "DB_HOST": database.endpoint,
                "DB_PORT": database.port.apply(str),
                "DB_NAME": database_name,
                "NAME_PREFIX": name_prefix,
                "TSS_ENDPOINT_URL": tss_endpoint_url,
                "INGEST_QUEUE_URL": self.ingest_queue.url,
            }
            if function == "starter":
                variables["STATE_MACHINE_ARN"] = state_machine_arn
            if function == "scan":
                variables["SECURITY_TOPIC_ARN"] = security_topic_arn
            if function == "steps":
                # The claim step: the tenant's worker slots, and how long a grant lasts.
                variables["WORKER_MAX_PER_TENANT"] = str(worker_max_per_tenant)
                variables["WORKER_GRANT_SECONDS"] = str(WORKER_GRANT_SECONDS)
            self.log_groups[function] = log_group
            self.functions[function] = aws.lambda_.Function(
                f"{fn_name}-fn",
                name=fn_name,
                description=spec.description,
                role=role.arn,
                package_type="Image",
                image_uri=self.image.image_uri,
                image_config=aws.lambda_.FunctionImageConfigArgs(commands=[spec.handler]),
                architectures=["x86_64"],
                memory_size=spec.memory,
                timeout=spec.timeout,
                ephemeral_storage=aws.lambda_.FunctionEphemeralStorageArgs(size=spec.ephemeral),
                # Results over the cap wait in Lambda's asynchronous queue, are retried with
                # backoff for the event age below (an hour), then go to the scan dead-letter
                # queue, whose depth raises an alarm.
                reserved_concurrent_executions=(
                    scan_reserved_concurrency if function == "scan" else None
                ),
                vpc_config=aws.lambda_.FunctionVpcConfigArgs(
                    subnet_ids=[network.workload_subnet.id],
                    security_group_ids=[network.workload_security_group.id],
                ),
                environment=aws.lambda_.FunctionEnvironmentArgs(variables=variables),
                logging_config=aws.lambda_.FunctionLoggingConfigArgs(
                    log_format="JSON",
                    log_group=log_group.name,
                    application_log_level="INFO",
                    system_log_level="WARN",
                ),
                opts=pulumi.ResourceOptions(parent=self, depends_on=[policy]),
            )
            aws.cloudwatch.LogMetricFilter(
                f"{fn_name}-errors",
                name=f"{fn_name}-errors",
                log_group_name=log_group.name,
                pattern=ERROR_LINES,
                metric_transformation=aws.cloudwatch.LogMetricFilterMetricTransformationArgs(
                    name=f"{function}-errors",
                    namespace=METRIC_NAMESPACE,
                    value="1",
                    default_value="0",
                ),
                opts=child,
            )
            self.alarms += [
                self._alarm(
                    f"{fn_name}-errors",
                    f"The pipeline's {function} function logged an error.",
                    namespace=METRIC_NAMESPACE,
                    metric=f"{function}-errors",
                    dimensions=None,
                ),
                self._alarm(
                    f"{fn_name}-failed",
                    f"The pipeline's {function} function failed (Lambda errors).",
                    namespace="AWS/Lambda",
                    metric="Errors",
                    dimensions={"FunctionName": self.functions[function].name},
                ),
            ]

        # --- Scan results -----------------------------------------------------------------
        scan_fn = self.functions["scan"]
        self.scan_rule = aws.cloudwatch.EventRule(
            f"{name}-scan-results",
            name=f"{name}-scan-results",
            description="GuardDuty Malware Protection scan results for tenant buckets",
            event_pattern=scan_event_pattern(name_prefix),
            opts=child,
        )
        self.scan_dlq_policy = aws.sqs.QueuePolicy(
            f"{name}-scan-dlq-policy",
            queue_url=self.scan_dlq.url,
            policy=pulumi.Output.all(queue=self.scan_dlq.arn, rule=self.scan_rule.arn).apply(
                lambda a: scan_dlq_policy(**a)
            ),
            opts=child,
        )
        permission = aws.lambda_.Permission(
            f"{name}-scan-invoke",
            action="lambda:InvokeFunction",
            function=scan_fn.name,
            principal="events.amazonaws.com",
            source_arn=self.scan_rule.arn,
            opts=child,
        )
        self.scan_target = aws.cloudwatch.EventTarget(
            f"{name}-scan-target",
            rule=self.scan_rule.name,
            target_id="scan",
            arn=scan_fn.arn,
            retry_policy=aws.cloudwatch.EventTargetRetryPolicyArgs(
                maximum_event_age_in_seconds=3600, maximum_retry_attempts=10
            ),
            dead_letter_config=aws.cloudwatch.EventTargetDeadLetterConfigArgs(
                arn=self.scan_dlq.arn
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[permission]),
        )
        self.scan_retries = aws.lambda_.FunctionEventInvokeConfig(
            f"{name}-scan-retries",
            function_name=scan_fn.name,
            maximum_retry_attempts=2,
            maximum_event_age_in_seconds=3600,
            destination_config=aws.lambda_.FunctionEventInvokeConfigDestinationConfigArgs(
                on_failure=aws.lambda_.FunctionEventInvokeConfigDestinationConfigOnFailureArgs(
                    destination=self.scan_dlq.arn
                )
            ),
            opts=child,
        )

        # --- The worker task (build step 6, second half) ----------------------------------
        self.worker_image = ContainerImage(
            f"{name}-worker",
            repo_root=repo_root,
            dockerfile=WORKER_DOCKERFILE,
            files=WORKSPACE_MANIFESTS,
            trees=IMAGE_TREES,
            region=region,
            account_id=account_id,
            aws_profile=aws_profile,
            opts=child,
        )
        self.worker_log_group = aws.cloudwatch.LogGroup(
            f"{name}-worker-logs",
            name=f"/aws/ecs/{name}-worker",
            retention_in_days=LOG_RETENTION_DAYS,
            opts=child,
        )
        self.worker_policy = aws.iam.RolePolicy(
            f"{name}-worker-task-policy",
            role=roles.worker.id,
            policy=db_user_arn(
                region, account_id, database.cluster_resource_id, WORKER_DB_USER
            ).apply(lambda arn: worker_role_policy(db_user=arn)),
            opts=child,
        )
        self.worker_execution_role = aws.iam.Role(
            f"{name}-worker-execution-role",
            description="Pipeline worker task execution: pull the image, write logs",
            assume_role_policy=account.apply(lambda acct: _ecs_tasks_trust(acct, region)),
            opts=child,
        )
        aws.iam.RolePolicy(
            f"{name}-worker-execution-policy",
            role=self.worker_execution_role.id,
            policy=pulumi.Output.all(
                repository=self.worker_image.repository.arn, log_group=self.worker_log_group.arn
            ).apply(lambda a: execution_role_policy(**a)),
            opts=child,
        )
        self.worker_cluster = aws.ecs.Cluster(
            f"{name}-worker-cluster",
            name=f"{name}-worker",
            settings=[aws.ecs.ClusterSettingArgs(name="containerInsights", value="enabled")],
            opts=child,
        )
        worker_environment = pulumi.Output.all(
            DB_HOST=database.endpoint,
            DB_PORT=database.port.apply(str),
            TSS_ENDPOINT_URL=tss_endpoint_url,
        ).apply(
            lambda env: {
                **env,
                "AWS_REGION": region,
                "NAME_PREFIX": name_prefix,
                "DB_NAME": database_name,
            }
        )
        self.worker_task_definition = aws.ecs.TaskDefinition(
            f"{name}-worker-task",
            family=f"{name}-worker",
            cpu=WORKER_CPU,
            memory=WORKER_MEMORY,
            network_mode="awsvpc",
            requires_compatibilities=["FARGATE"],
            runtime_platform=aws.ecs.TaskDefinitionRuntimePlatformArgs(
                cpu_architecture="X86_64", operating_system_family="LINUX"
            ),
            execution_role_arn=self.worker_execution_role.arn,
            task_role_arn=roles.worker.arn,
            volumes=[aws.ecs.TaskDefinitionVolumeArgs(name=WORK_VOLUME)],
            container_definitions=pulumi.Output.all(
                image=self.worker_image.image_uri,
                log_group=self.worker_log_group.name,
                environment=worker_environment,
            ).apply(lambda a: worker_container_definitions(region=region, **a)),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self.worker_policy]),
        )
        aws.cloudwatch.LogMetricFilter(
            f"{name}-worker-errors",
            name=f"{name}-worker-errors",
            log_group_name=self.worker_log_group.name,
            pattern=ERROR_LINES,
            metric_transformation=aws.cloudwatch.LogMetricFilterMetricTransformationArgs(
                name="worker-errors",
                namespace=METRIC_NAMESPACE,
                value="1",
                default_value="0",
            ),
            opts=child,
        )
        self.alarms.append(
            self._alarm(
                f"{name}-worker-errors",
                "The pipeline's worker task logged an error.",
                namespace=METRIC_NAMESPACE,
                metric="worker-errors",
                dimensions=None,
            )
        )

        # --- The state machine ------------------------------------------------------------
        steps_fn = self.functions["steps"]
        self.state_machine_log_group = aws.cloudwatch.LogGroup(
            f"{name}-sfn-logs",
            name=f"/aws/vendedlogs/states/{name}",
            retention_in_days=LOG_RETENTION_DAYS,
            opts=child,
        )
        state_machine_role = aws.iam.Role(
            f"{name}-sfn-role",
            # Unchanged since the first half (a description cannot change in place): it
            # now also runs the worker task.
            description="Pipeline state machine: run the pipeline's Lambda steps",
            assume_role_policy=account.apply(
                lambda acct: _service_trust("states.amazonaws.com", acct)
            ),
            opts=child,
        )
        self.state_machine_policy = state_machine_policy = aws.iam.RolePolicy(
            f"{name}-sfn-policy",
            role=state_machine_role.id,
            policy=pulumi.Output.all(
                steps_function=steps_fn.arn,
                account_id=account,
                cluster_name=self.worker_cluster.name,
                cluster=self.worker_cluster.arn,
                task_family=self.worker_task_definition.family,
                task_role=roles.worker.arn,
                execution_role=self.worker_execution_role.arn,
            ).apply(lambda a: state_machine_role_policy(region=region, **a)),
            opts=child,
        )
        self.state_machine = aws.sfn.StateMachine(
            f"{name}-sfn",
            name=name,
            type="STANDARD",
            role_arn=state_machine_role.arn,
            definition=pulumi.Output.all(
                steps_function=steps_fn.arn,
                cluster=self.worker_cluster.arn,
                task_definition=self.worker_task_definition.arn,
                subnet=network.workload_subnet.id,
                security_group=network.workload_security_group.id,
            ).apply(lambda a: state_machine_definition(**a)),
            # Errors only, and never the execution's data.
            logging_configuration=aws.sfn.StateMachineLoggingConfigurationArgs(
                level="ERROR",
                include_execution_data=False,
                log_destination=pulumi.Output.concat(self.state_machine_log_group.arn, ":*"),
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[state_machine_policy]),
        )

        # --- The starter ------------------------------------------------------------------
        self.starter_source = aws.lambda_.EventSourceMapping(
            f"{name}-starter-source",
            event_source_arn=self.ingest_queue.arn,
            function_name=self.functions["starter"].arn,
            batch_size=10,
            function_response_types=["ReportBatchItemFailures"],
            # Starting executions is quick; a small cap keeps the database calm.
            scaling_config=aws.lambda_.EventSourceMappingScalingConfigArgs(maximum_concurrency=2),
            opts=child,
        )

        # --- The sweeper ------------------------------------------------------------------
        scheduler_role = aws.iam.Role(
            f"{name}-sweep-schedule-role",
            description="Scheduler: run the pipeline sweeper every five minutes",
            assume_role_policy=account.apply(
                lambda acct: _service_trust("scheduler.amazonaws.com", acct)
            ),
            opts=child,
        )
        self.schedule_policy = aws.iam.RolePolicy(
            f"{name}-sweep-schedule-policy",
            role=scheduler_role.id,
            policy=self.functions["sweeper"].arn.apply(
                lambda arn: scheduler_role_policy(function=arn)
            ),
            opts=child,
        )
        self.schedule = aws.scheduler.Schedule(
            f"{name}-sweep",
            name=f"{name}-sweep",
            description="Batch housekeeping: stale batches, overdue scans, unstarted documents",
            schedule_expression=SWEEP_SCHEDULE,
            flexible_time_window=aws.scheduler.ScheduleFlexibleTimeWindowArgs(mode="OFF"),
            target=aws.scheduler.ScheduleTargetArgs(
                arn=self.functions["sweeper"].arn,
                role_arn=scheduler_role.arn,
                retry_policy=aws.scheduler.ScheduleTargetRetryPolicyArgs(
                    maximum_event_age_in_seconds=300, maximum_retry_attempts=0
                ),
            ),
            opts=child,
        )

        # --- Alarms (section 4, item 9) ---------------------------------------------------
        for queue_name, queue, description in (
            ("ingest-dlq", self.ingest_dlq, "Ingest jobs failed five times and were not started."),
            ("scan-dlq", self.scan_dlq, "Scan results could not be recorded."),
        ):
            self.alarms.append(
                self._alarm(
                    f"{name}-{queue_name}-depth",
                    description,
                    namespace="AWS/SQS",
                    metric="ApproximateNumberOfMessagesVisible",
                    dimensions={"QueueName": queue.name},
                    stat="Maximum",
                )
            )
        for metric, description in (
            ("ExecutionsFailed", "A document's pipeline execution failed."),
            ("ExecutionsTimedOut", "A document's pipeline execution timed out."),
        ):
            self.alarms.append(
                self._alarm(
                    f"{name}-{metric.lower()}",
                    description,
                    namespace="AWS/States",
                    metric=metric,
                    dimensions={"StateMachineArn": self.state_machine.arn},
                )
            )

        self.register_outputs(
            {
                "ingest_queue_url": self.ingest_queue.url,
                "state_machine_arn": self.state_machine.arn,
            }
        )

    def _alarm(
        self,
        name: str,
        description: str,
        *,
        namespace: str,
        metric: str,
        dimensions: Mapping[str, pulumi.Input[str]] | None,
        stat: str = "Sum",
    ) -> aws.cloudwatch.MetricAlarm:
        return aws.cloudwatch.MetricAlarm(
            f"{name}-alarm",
            name=name,
            alarm_description=description,
            alarm_actions=[self._alarm_topic_arn],
            namespace=namespace,
            metric_name=metric,
            dimensions=dict(dimensions) if dimensions else None,
            statistic=stat,
            period=300,
            evaluation_periods=1,
            threshold=1,
            comparison_operator="GreaterThanOrEqualToThreshold",
            treat_missing_data="notBreaching",
            opts=pulumi.ResourceOptions(parent=self),
        )
