"""The ingestion pipeline, first half of build step 6 (BUILD_SPEC.md sections 4 and 8).

    GuardDuty scan result ──EventBridge──> scan Lambda ──┐
    POST /batches/{id}/close (API) ──────────────────────┼──> ingest queue ──> starter Lambda
    sweeper Lambda (every 5 minutes) ────────────────────┘        │                 │
                                                       dead-letter queue    one execution per PDF
                                                                                    │
                                     pipeline state machine: fingerprint ─┬─> (steps 5-10:
                                                                          │    second half)
                                                                          └─> failed (catch)

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
- **State machine**: Standard, logging errors only and never execution data. Its steps run in
  the step Lambda (`assay_workers.steps`); any failure goes to the `failed` step, which marks
  the document failed with a short code, and the execution fails (alarmed).
- **Sweeper** (`assay_workers.sweeper`), every five minutes (EventBridge Scheduler).

Every function runs in the VPC as database role `app_tenant` and reaches the TSS through
the `execute-api` endpoint with its own caller name and access (`TssCaller`): the scan
Lambda, the step Lambda and the sweeper get `PIPELINE` only; the starter is no TSS caller.
No function has any S3, KMS or AI-service permission of its own: object access is through
TSS-issued tenant credentials only. Each function's ERROR lines raise an alarm (an
unregistered scan key, a scan that never arrived, a job that could not be queued), as do
Lambda errors.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pulumi
import pulumi_aws as aws

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

    def by_function(self) -> dict[str, aws.iam.Role]:
        return {
            "scan": self.scan,
            "starter": self.starter,
            "steps": self.steps,
            "sweeper": self.sweeper,
        }


def pipeline_roles(name: str, account_id: pulumi.Input[str]) -> PipelineRoles:
    trust = pulumi.Output.from_input(account_id).apply(
        lambda acct: _service_trust("lambda.amazonaws.com", acct)
    )

    def role(function: str, description: str) -> aws.iam.Role:
        return aws.iam.Role(
            f"{name}-{function}-role", description=description, assume_role_policy=trust
        )

    return PipelineRoles(
        scan=role("scan", "Pipeline scan results: record them, quarantine infected uploads"),
        starter=role("starter", "Pipeline starter: one execution per eligible document"),
        steps=role("steps", "Pipeline steps: fingerprint and failure handling"),
        sweeper=role("sweeper", "Pipeline sweeper: stale batches, overdue scans, requeues"),
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


def state_machine_definition(*, steps_function: str) -> str:
    """Input: an ingest job, `{"tenant_id", "document_id"}` (assay_schemas.pipeline.IngestJob).

    First half of build step 6: fingerprint only. The second half adds steps 5 to 10 after
    `Continue`. A step's error goes to its own Pass state, which sets `failed_step` to that
    step's name (fixed text from this definition, never from the error), then to `Failed`.
    `Failed` gets the step's name and the error's name (`$.error.Error`) only, never its
    `Cause` (a message or stack trace), records them on the document and fails the execution.
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
            **(extra or {}),
        }
        return {
            "Type": "Task",
            "Resource": "arn:aws:states:::lambda:invoke",
            "Parameters": {"FunctionName": steps_function, "Payload": payload},
            "OutputPath": "$.Payload",
            "Retry": [LAMBDA_RETRY],
        }

    return json.dumps(
        {
            "Comment": "Ingest one PDF (build step 6, first half: fingerprint)",
            "StartAt": "Fingerprint",
            "TimeoutSeconds": 3600,
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
                            "Next": "Continue",
                        },
                        {
                            "Variable": "$.fingerprint.outcome",
                            "StringEquals": "refused",
                            "Next": "Refused",
                        },
                    ],
                    # duplicate, on_hold, failed: the document's status says why.
                    "Default": "Stopped",
                },
                "Continue": {"Type": "Succeed"},
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


def state_machine_role_policy(*, steps_function: str) -> str:
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
            description="Pipeline state machine: run the pipeline's Lambda steps",
            assume_role_policy=account.apply(
                lambda acct: _service_trust("states.amazonaws.com", acct)
            ),
            opts=child,
        )
        self.state_machine_policy = state_machine_policy = aws.iam.RolePolicy(
            f"{name}-sfn-policy",
            role=state_machine_role.id,
            policy=steps_fn.arn.apply(lambda arn: state_machine_role_policy(steps_function=arn)),
            opts=child,
        )
        self.state_machine = aws.sfn.StateMachine(
            f"{name}-sfn",
            name=name,
            type="STANDARD",
            role_arn=state_machine_role.arn,
            definition=steps_fn.arn.apply(lambda arn: state_machine_definition(steps_function=arn)),
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
