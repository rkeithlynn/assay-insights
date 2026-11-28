"""Sign-in and password-reset audit events (BUILD_SPEC.md section 7a), owner design D1.

    Cognito (both pools) ──trigger──> SQS queue ──> consumer (as audit_writer) ──> audit_log
                                         └─ after 5 failed receives ─> dead-letter queue

- **Trigger** (`api/assay_api/cognito_auth_trigger.py`): post-authentication and
  post-confirmation on both pools. In the VPC, it only sends one message through the `sqs`
  endpoint and always returns, so it never blocks a sign-in. Any error it logs raises an
  alarm.
- **Queue**: SSE-SQS encryption, TLS only. Its policy and the `sqs` endpoint's policy let
  only the trigger's role send to it. Messages that fail five times go to the dead-letter
  queue, whose depth raises an alarm.
- **Consumer** (`api/assay_api/auth_event_consumer.py`): in the VPC, signs in as
  `audit_writer` and calls `auth_events.record()`, which finds the user's tenant in the
  database. An unknown user fails the message, so it reaches the dead-letter queue.

The trigger exists before the user pools (they name it); the consumer after them (it maps
each pool ID to the tenant or platform pool), so it is added with `add_consumer`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pulumi
import pulumi_aws as aws

from assay_infra.database import Database, db_user_arn
from assay_infra.images import ContainerImage
from assay_infra.network import Network
from assay_infra.tss import VPC_NETWORK_INTERFACE_STATEMENT, _service_trust

AUDIT_WRITER_DB_USER = "audit_writer"  # assay_db.roles.AUDIT_WRITER

TRIGGER_DOCKERFILE = "api/auth-trigger.Dockerfile"
TRIGGER_SOURCES = ("api/assay_api/__init__.py", "api/assay_api/cognito_auth_trigger.py")
CONSUMER_HANDLER = "assay_api.auth_event_consumer.handler"

# Cognito waits at most 5 seconds for a trigger.
TRIGGER_TIMEOUT_SECONDS = 5
CONSUMER_TIMEOUT_SECONDS = 60
# AWS recommends a visibility timeout of six times the function's timeout for SQS sources.
VISIBILITY_TIMEOUT_SECONDS = 6 * CONSUMER_TIMEOUT_SECONDS
MAX_RECEIVES = 5
DLQ_RETENTION_SECONDS = 14 * 24 * 3600
LOG_RETENTION_DAYS = 30
METRIC_NAMESPACE = "Assay/AuthEvents"
TRIGGER_ERROR_METRIC = "TriggerErrors"
# Lambda's JSON log format puts the level in `level`.
ERROR_LINES = '{ $.level = "ERROR" }'

SEND_ACTIONS = ("sqs:SendMessage",)
CONSUME_ACTIONS = (
    "sqs:ReceiveMessage",
    "sqs:DeleteMessage",
    "sqs:ChangeMessageVisibility",
    "sqs:GetQueueAttributes",
)


def _json(document: object) -> str:
    return json.dumps(document, separators=(",", ":"))


def _logs(log_group: str) -> dict[str, object]:
    return {
        "Sid": "Logs",
        "Effect": "Allow",
        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": f"{log_group}:*",
    }


def trigger_role_policy(*, log_group: str, queue: str) -> str:
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                _logs(log_group),
                VPC_NETWORK_INTERFACE_STATEMENT,
                {
                    "Sid": "QueueAuthEvents",
                    "Effect": "Allow",
                    "Action": list(SEND_ACTIONS),
                    "Resource": queue,
                },
            ],
        }
    )


def consumer_role_policy(*, log_group: str, queue: str, db_user: str) -> str:
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                _logs(log_group),
                VPC_NETWORK_INTERFACE_STATEMENT,
                {
                    "Sid": "ConsumeAuthEvents",
                    "Effect": "Allow",
                    "Action": list(CONSUME_ACTIONS),
                    "Resource": queue,
                },
                {
                    "Sid": "ConnectAsAuditWriter",
                    "Effect": "Allow",
                    "Action": "rds-db:connect",
                    "Resource": db_user,
                },
            ],
        }
    )


def queue_policy(*, queue: str, trigger_role: str) -> str:
    """Only the trigger's role may send. TLS only."""
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "OnlyTheTriggerSends",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": list(SEND_ACTIONS),
                    "Resource": queue,
                    "Condition": {"ArnNotEquals": {"aws:PrincipalArn": trigger_role}},
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


# (Sid, queue ARN, sender role ARNs): one statement each in the SQS endpoint's policy.
SqsGrant = tuple[str, str, Sequence[str]]


def sqs_endpoint_policy(*, queue: str, trigger_role: str, grants: Sequence[SqsGrant] = ()) -> str:
    """Through the VPC's SQS endpoint: named queues, each for named senders only.

    The auth-event trigger sends to its queue (build step 5); from build step 6 the API, the
    scan Lambda and the sweeper send ingest jobs (`grants`). Queues read by an event source
    mapping need no statement: the Lambda service polls them, not through the endpoint.
    """
    statements: list[dict[str, object]] = [
        {
            "Sid": "TriggerQueuesAuthEvents",
            "Effect": "Allow",
            "Principal": "*",
            "Action": list(SEND_ACTIONS),
            "Resource": queue,
            "Condition": {"ArnEquals": {"aws:PrincipalArn": trigger_role}},
        }
    ]
    for sid, target, senders in grants:
        names = sorted(senders)
        principal: str | list[str] = names[0] if len(names) == 1 else names
        statements.append(
            {
                "Sid": sid,
                "Effect": "Allow",
                "Principal": "*",
                "Action": list(SEND_ACTIONS),
                "Resource": target,
                "Condition": {"ArnEquals": {"aws:PrincipalArn": principal}},
            }
        )
    return _json({"Version": "2012-10-17", "Statement": statements})


class AuthEvents(pulumi.ComponentResource):
    def __init__(
        self,
        name: str,
        *,
        repo_root: Path,
        region: str,
        account_id: pulumi.Input[str],
        network: Network,
        alarm_topic_arn: pulumi.Input[str],
        aws_profile: str | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:auth:AuthEvents", name, None, opts)
        self._name = name
        self._region = region
        self._account_id = account_id
        self._network = network
        self._alarm_topic_arn = alarm_topic_arn
        child = pulumi.ResourceOptions(parent=self)
        account = pulumi.Output.from_input(account_id)
        lambda_trust = account.apply(lambda acct: _service_trust("lambda.amazonaws.com", acct))
        self._lambda_trust = lambda_trust

        # --- Queues -----------------------------------------------------------------------
        self.dead_letter_queue = aws.sqs.Queue(
            f"{name}-dlq",
            name=f"{name}-dlq",
            sqs_managed_sse_enabled=True,
            message_retention_seconds=DLQ_RETENTION_SECONDS,
            opts=child,
        )
        self.queue = aws.sqs.Queue(
            f"{name}-queue",
            name=name,
            sqs_managed_sse_enabled=True,
            visibility_timeout_seconds=VISIBILITY_TIMEOUT_SECONDS,
            message_retention_seconds=DLQ_RETENTION_SECONDS,
            redrive_policy=self.dead_letter_queue.arn.apply(
                lambda arn: _json({"deadLetterTargetArn": arn, "maxReceiveCount": MAX_RECEIVES})
            ),
            opts=child,
        )

        # --- Trigger ----------------------------------------------------------------------
        trigger_name = f"{name}-trigger"
        self.trigger_image = ContainerImage(
            trigger_name,
            repo_root=repo_root,
            dockerfile=TRIGGER_DOCKERFILE,
            files=TRIGGER_SOURCES,
            region=region,
            account_id=account_id,
            lambda_pull=True,
            aws_profile=aws_profile,
            opts=child,
        )
        self.trigger_log_group = aws.cloudwatch.LogGroup(
            f"{trigger_name}-logs",
            name=f"/aws/lambda/{trigger_name}",
            retention_in_days=LOG_RETENTION_DAYS,
            opts=child,
        )
        self.trigger_role = aws.iam.Role(
            f"{trigger_name}-role",
            description="Cognito auth-event trigger: queue sign-ins and password resets",
            assume_role_policy=lambda_trust,
            opts=child,
        )
        trigger_policy = aws.iam.RolePolicy(
            f"{trigger_name}-policy",
            role=self.trigger_role.id,
            policy=pulumi.Output.all(
                log_group=self.trigger_log_group.arn, queue=self.queue.arn
            ).apply(lambda a: trigger_role_policy(**a)),
            opts=child,
        )
        self.trigger_function = aws.lambda_.Function(
            f"{trigger_name}-fn",
            name=trigger_name,
            description="Cognito post-auth and post-confirmation: queue the audit event",
            role=self.trigger_role.arn,
            package_type="Image",
            image_uri=self.trigger_image.image_uri,
            architectures=["x86_64"],
            memory_size=256,
            timeout=TRIGGER_TIMEOUT_SECONDS,
            vpc_config=aws.lambda_.FunctionVpcConfigArgs(
                subnet_ids=[network.workload_subnet.id],
                security_group_ids=[network.workload_security_group.id],
            ),
            environment=aws.lambda_.FunctionEnvironmentArgs(
                variables={"QUEUE_URL": self.queue.url}
            ),
            logging_config=aws.lambda_.FunctionLoggingConfigArgs(
                log_format="JSON",
                log_group=self.trigger_log_group.name,
                application_log_level="INFO",
                system_log_level="WARN",
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[trigger_policy]),
        )
        aws.sqs.QueuePolicy(
            f"{name}-queue-policy",
            queue_url=self.queue.url,
            policy=pulumi.Output.all(
                queue=self.queue.arn, trigger_role=self.trigger_role.arn
            ).apply(lambda a: queue_policy(**a)),
            opts=child,
        )

        # --- Alarms on the trigger and the dead-letter queue ------------------------------
        self.trigger_error_filter = aws.cloudwatch.LogMetricFilter(
            f"{trigger_name}-errors",
            name=f"{trigger_name}-errors",
            log_group_name=self.trigger_log_group.name,
            pattern=ERROR_LINES,
            metric_transformation=aws.cloudwatch.LogMetricFilterMetricTransformationArgs(
                name=TRIGGER_ERROR_METRIC, namespace=METRIC_NAMESPACE, value="1", default_value="0"
            ),
            opts=child,
        )
        self.alarms = [
            self._alarm(
                f"{trigger_name}-errors",
                "The Cognito auth-event trigger could not queue a sign-in or password reset.",
                namespace=METRIC_NAMESPACE,
                metric=TRIGGER_ERROR_METRIC,
                dimensions=None,
            ),
            self._alarm(
                f"{trigger_name}-failed",
                "The Cognito auth-event trigger failed (Lambda errors).",
                namespace="AWS/Lambda",
                metric="Errors",
                dimensions={"FunctionName": self.trigger_function.name},
            ),
            self._alarm(
                f"{name}-dlq-depth",
                "Auth events reached the dead-letter queue and are not in audit_log.",
                namespace="AWS/SQS",
                metric="ApproximateNumberOfMessagesVisible",
                dimensions={"QueueName": self.dead_letter_queue.name},
                stat="Maximum",
            ),
        ]
        self.register_outputs({"queue_url": self.queue.url})

    def _alarm(
        self,
        name: str,
        description: str,
        *,
        namespace: str,
        metric: str,
        dimensions: dict[str, pulumi.Input[str]] | None,
        stat: str = "Sum",
    ) -> aws.cloudwatch.MetricAlarm:
        return aws.cloudwatch.MetricAlarm(
            f"{name}-alarm",
            name=name,
            alarm_description=description,
            alarm_actions=[self._alarm_topic_arn],
            namespace=namespace,
            metric_name=metric,
            dimensions=dimensions,
            statistic=stat,
            period=300,
            evaluation_periods=1,
            threshold=1,
            comparison_operator="GreaterThanOrEqualToThreshold",
            treat_missing_data="notBreaching",
            opts=pulumi.ResourceOptions(parent=self),
        )

    def sqs_endpoint_access(
        self, grants: Sequence[tuple[str, pulumi.Input[str], Sequence[pulumi.Input[str]]]] = ()
    ) -> aws.ec2.VpcEndpointPolicy:
        """The `sqs` endpoint's policy: the auth-event trigger, plus these grants.

        Created here, with the name and parent it has had since build step 5, so it is the
        same resource: deleting a VPC endpoint policy resets the endpoint to allow all. Every
        queue a workload in the VPC sends to must have a grant (build step 6).
        """
        sids = [sid for sid, _, _ in grants]
        queues = [queue for _, queue, _ in grants]
        senders = [pulumi.Output.all(*roles) for _, _, roles in grants]
        self.sqs_endpoint_policy = aws.ec2.VpcEndpointPolicy(
            f"{self._name}-sqs-endpoint-policy",
            vpc_endpoint_id=self._network.interface_endpoints["sqs"].id,
            policy=pulumi.Output.all(
                queue=self.queue.arn,
                trigger_role=self.trigger_role.arn,
                queues=pulumi.Output.all(*queues),
                senders=pulumi.Output.all(*senders),
            ).apply(
                lambda a: sqs_endpoint_policy(
                    queue=a["queue"],
                    trigger_role=a["trigger_role"],
                    grants=list(zip(sids, a["queues"], a["senders"], strict=True)),
                )
            ),
            opts=pulumi.ResourceOptions(parent=self),
        )
        return self.sqs_endpoint_policy

    def attach(self, name: str, pool: aws.cognito.UserPool, account_id: pulumi.Input[str]) -> None:
        """Let one user pool invoke the trigger."""
        aws.lambda_.Permission(
            f"{name}-invoke",
            action="lambda:InvokeFunction",
            function=self.trigger_function.name,
            principal="cognito-idp.amazonaws.com",
            source_arn=pool.arn,
            source_account=account_id,
            opts=pulumi.ResourceOptions(parent=self),
        )

    def add_consumer(
        self,
        *,
        image_uri: pulumi.Input[str],
        database: Database,
        database_name: str,
        pool_ids: Sequence[pulumi.Input[str]],
    ) -> None:
        """The consumer Lambda and its event source. `pool_ids` is (tenant, platform)."""
        name = f"{self._name}-consumer"
        child = pulumi.ResourceOptions(parent=self)
        tenant_pool, platform_pool = pool_ids
        self.consumer_log_group = aws.cloudwatch.LogGroup(
            f"{name}-logs",
            name=f"/aws/lambda/{name}",
            retention_in_days=LOG_RETENTION_DAYS,
            opts=child,
        )
        self.consumer_role = aws.iam.Role(
            f"{name}-role",
            description="Auth-event consumer: write sign-ins and resets to audit_log",
            assume_role_policy=self._lambda_trust,
            opts=child,
        )
        policy = aws.iam.RolePolicy(
            f"{name}-policy",
            role=self.consumer_role.id,
            policy=pulumi.Output.all(
                log_group=self.consumer_log_group.arn,
                queue=self.queue.arn,
                db_user=db_user_arn(
                    self._region,
                    self._account_id,
                    database.cluster_resource_id,
                    AUDIT_WRITER_DB_USER,
                ),
            ).apply(lambda a: consumer_role_policy(**a)),
            opts=child,
        )
        self.consumer_function = aws.lambda_.Function(
            f"{name}-fn",
            name=name,
            description="Write queued sign-ins and password resets to audit_log",
            role=self.consumer_role.arn,
            package_type="Image",
            image_uri=image_uri,
            image_config=aws.lambda_.FunctionImageConfigArgs(commands=[CONSUMER_HANDLER]),
            architectures=["x86_64"],
            memory_size=512,
            timeout=CONSUMER_TIMEOUT_SECONDS,
            vpc_config=aws.lambda_.FunctionVpcConfigArgs(
                subnet_ids=[self._network.workload_subnet.id],
                security_group_ids=[self._network.workload_security_group.id],
            ),
            environment=aws.lambda_.FunctionEnvironmentArgs(
                variables={
                    "DB_HOST": database.endpoint,
                    "DB_PORT": database.port.apply(str),
                    "DB_NAME": database_name,
                    "TENANT_POOL_ID": tenant_pool,
                    "PLATFORM_POOL_ID": platform_pool,
                }
            ),
            logging_config=aws.lambda_.FunctionLoggingConfigArgs(
                log_format="JSON",
                log_group=self.consumer_log_group.name,
                application_log_level="INFO",
                system_log_level="WARN",
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[policy]),
        )
        self.consumer_source = aws.lambda_.EventSourceMapping(
            f"{name}-source",
            event_source_arn=self.queue.arn,
            function_name=self.consumer_function.arn,
            batch_size=10,
            function_response_types=["ReportBatchItemFailures"],
            opts=pulumi.ResourceOptions(parent=self, depends_on=[policy]),
        )
        self.alarms.append(
            self._alarm(
                f"{name}-failed",
                "The auth-event consumer failed (Lambda errors).",
                namespace="AWS/Lambda",
                metric="Errors",
                dimensions={"FunctionName": self.consumer_function.name},
            )
        )
