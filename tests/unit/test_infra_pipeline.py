"""The ingestion pipeline's infrastructure, build step 6 (Pulumi mocks).

What it must never get: an S3, KMS or AI-service permission of its own (tenant data is
reached only with TSS-issued credentials), a queue anyone may send to, a state machine that
logs execution data, or an alarm that notifies no one.

The worker task (second half) on top of that: a role that may only sign in as `app_worker`,
a container with exactly three capabilities on a read-only root filesystem, and a state
machine that may pass only the worker's two roles.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pulumi
import pulumi_aws as aws

from assay_infra import alerts, pipeline
from assay_infra.api import api_task_role
from assay_infra.auth_events import sqs_endpoint_policy
from assay_infra.database import Database
from assay_infra.network import Network, states_endpoint_policy
from tests.unit.conftest import ACCOUNT_ID, REGION, registered
from tests.unit.test_infra_data import DB, NET

REPO_ROOT = Path(__file__).resolve().parents[2]
TOPIC = f"arn:aws:sns:{REGION}:{ACCOUNT_ID}:alarms"
NOTICES = f"arn:aws:sns:{REGION}:{ACCOUNT_ID}:security-notices"
NOTICES_KEY = f"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/notices"
TSS = f"https://tss.execute-api.{REGION}.amazonaws.com/v1"
ROLE = f"arn:aws:iam::{ACCOUNT_ID}:role"


def _one(type_token: str, name: str) -> dict[str, Any]:
    matches = [inputs for n, inputs in registered(type_token) if n == name]
    assert len(matches) == 1, f"{type_token} {name}: {len(matches)} registered"
    return matches[0]


def _stack(prefix: str) -> pipeline.Pipeline:
    network = Network(prefix, cfg=NET, region=REGION, account_id=ACCOUNT_ID)
    database = Database(f"{prefix}-db", cfg=DB, network=network, account_id=ACCOUNT_ID)
    return pipeline.Pipeline(
        f"{prefix}-pipeline",
        repo_root=REPO_ROOT,
        region=REGION,
        account_id=ACCOUNT_ID,
        name_prefix=prefix,
        network=network,
        database=database,
        database_name=DB.database_name,
        roles=pipeline.pipeline_roles(f"{prefix}-pipeline", ACCOUNT_ID, REGION),
        tss_endpoint_url=TSS,
        api_task_role=api_task_role(f"{prefix}-api", account_id=ACCOUNT_ID, region=REGION),
        alarm_topic_arn=TOPIC,
        security_topic_arn=NOTICES,
        notices_key_arn=NOTICES_KEY,
    )


def _policy(type_token: str, name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(_one(type_token, name)["policy"])
    return document


ROLE_POLICY = "aws:iam/rolePolicy:RolePolicy"
# Tenant data and AI services: reached only with TSS-issued credentials, never by a role.
FORBIDDEN_SERVICES = ("s3:", "s3vectors:", "kms:", "bedrock:", "textract:", "rekognition:")
QUEUE_POLICY = "aws:sqs/queuePolicy:QueuePolicy"


def _ready(p: pipeline.Pipeline) -> pulumi.Output[list[Any]]:
    # Every resource the tests read, so none is checked before it is registered.
    return pulumi.Output.all(
        p.state_machine.id,
        p.state_machine_policy.id,
        p.ingest_queue.id,
        p.ingest_policy.id,
        p.scan_dlq_policy.id,
        p.scan_target.id,
        p.scan_retries.id,
        p.starter_source.id,
        p.schedule.id,
        p.schedule_policy.id,
        p.worker_task_definition.id,
        p.worker_policy.id,
        p.worker_cluster.id,
        p.worker_execution_role.id,
        *[f.id for f in p.functions.values()],
        *[a.id for a in p.alarms],
    )


def _actions(policy: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for statement in policy["Statement"]:
        action = statement["Action"]
        out |= set(action if isinstance(action, list) else [action])
    return out


@pulumi.runtime.test
def test_no_function_has_any_storage_or_ai_permission_of_its_own() -> Any:
    p = _stack("pl1")

    def check(_: list[Any]) -> None:
        for function in pipeline.FUNCTIONS:
            policy = json.loads(
                _one("aws:iam/rolePolicy:RolePolicy", f"pl1-pipeline-{function}-policy")["policy"]
            )
            # One exception, the scan function's only: the security-notice topic's key, and
            # only through SNS (it encrypts the notice). Checked on its own, then set aside.
            notice_key = [
                s for s in policy["Statement"] if s.get("Sid") == "NoticeKeyThroughSnsOnly"
            ]
            assert len(notice_key) == (1 if function == "scan" else 0), function
            for statement in notice_key:
                assert statement == {
                    "Sid": "NoticeKeyThroughSnsOnly",
                    "Effect": "Allow",
                    "Action": ["kms:GenerateDataKey*", "kms:Decrypt"],
                    "Resource": NOTICES_KEY,
                    "Condition": {
                        "StringEquals": {"kms:ViaService": f"sns.{REGION}.amazonaws.com"}
                    },
                }
            rest = {**policy, "Statement": [s for s in policy["Statement"] if s not in notice_key]}
            actions = _actions(rest)
            for forbidden in FORBIDDEN_SERVICES:
                assert not any(a.startswith(forbidden) for a in actions), (function, forbidden)
            # Only the scan function publishes, and only to the security-notice topic.
            publish = [s for s in policy["Statement"] if s["Action"] == "sns:Publish"]
            assert [s["Resource"] for s in publish] == ([NOTICES] if function == "scan" else [])
            assert not any(a.startswith("sns:") and a != "sns:Publish" for a in actions)
            assert "rds-db:connect" in actions
            db = next(s for s in policy["Statement"] if s["Action"] == "rds-db:connect")
            assert db["Resource"].endswith("/app_tenant"), function

    return _ready(p).apply(check)


@pulumi.runtime.test
def test_each_function_gets_only_its_own_queue_and_state_machine_actions() -> Any:
    p = _stack("pl2")

    def check(_: list[Any]) -> None:
        def actions(function: str) -> set[str]:
            return _actions(_policy(ROLE_POLICY, f"pl2-pipeline-{function}-policy"))

        common = {"logs:CreateLogStream", "logs:PutLogEvents", "rds-db:connect"}
        assert actions("steps") - common <= {a for a in actions("steps") if a.startswith("ec2:")}
        assert "sqs:SendMessage" in actions("scan") and "sqs:SendMessage" in actions("sweeper")
        assert "states:StartExecution" in actions("starter")
        assert "sqs:ReceiveMessage" in actions("starter")
        for function in ("scan", "steps", "sweeper"):
            assert "states:StartExecution" not in actions(function)
        assert "sqs:SendMessage" not in actions("steps")

    return _ready(p).apply(check)


@pulumi.runtime.test
def test_functions_run_in_the_vpc_as_x86_64_images() -> Any:
    p = _stack("pl3")

    def check(_: list[Any]) -> None:
        for function, spec in pipeline.FUNCTIONS.items():
            fn = _one("aws:lambda/function:Function", f"pl3-pipeline-{function}-fn")
            assert fn["architectures"] == ["x86_64"]
            assert fn["packageType"] == "Image"
            assert fn["imageConfig"]["commands"] == [spec.handler]
            assert fn["vpcConfig"]["subnetIds"]
            assert fn["environment"]["variables"]["TSS_ENDPOINT_URL"] == TSS
            assert fn["loggingConfig"]["logFormat"] == "JSON"
        steps = _one("aws:lambda/function:Function", "pl3-pipeline-steps-fn")
        assert steps["ephemeralStorage"]["size"] >= 1024  # a 100 MB PDF in /tmp
        # The claim step's cap and the grant's lifetime: the task's timeout and a margin.
        variables = steps["environment"]["variables"]
        assert variables["WORKER_MAX_PER_TENANT"] == "5"
        assert int(variables["WORKER_GRANT_SECONDS"]) > pipeline.WORKER_TIMEOUT_SECONDS

    return _ready(p).apply(check)


@pulumi.runtime.test
def test_only_the_named_senders_may_queue_ingest_jobs() -> Any:
    p = _stack("pl4")

    def check(_: list[Any]) -> None:
        policy = json.loads(
            _one("aws:sqs/queuePolicy:QueuePolicy", "pl4-pipeline-ingest-policy")["policy"]
        )
        deny = policy["Statement"][0]
        assert deny["Effect"] == "Deny" and deny["Action"] == ["sqs:SendMessage"]
        senders = deny["Condition"]["ArnNotEquals"]["aws:PrincipalArn"]
        assert sorted(senders) == sorted(
            [
                f"{ROLE}/pl4-api-task-role-a1b2c3d",
                f"{ROLE}/pl4-pipeline-scan-role-a1b2c3d",
                f"{ROLE}/pl4-pipeline-sweeper-role-a1b2c3d",
            ]
        )
        assert policy["Statement"][1]["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
        queue = _one("aws:sqs/queue:Queue", "pl4-pipeline-ingest")
        assert queue["sqsManagedSseEnabled"] is True
        assert json.loads(queue["redrivePolicy"])["maxReceiveCount"] == 5

    return _ready(p).apply(check)


@pulumi.runtime.test
def test_scan_results_come_from_guardduty_about_tenant_buckets_only() -> Any:
    p = _stack("pl5")

    def check(_: list[Any]) -> None:
        rule = _one("aws:cloudwatch/eventRule:EventRule", "pl5-pipeline-scan-results")
        pattern = json.loads(rule["eventPattern"])
        assert pattern == {
            "source": ["aws.guardduty"],
            "detail-type": ["GuardDuty Malware Protection Object Scan Result"],
            "detail": {"s3ObjectDetails": {"bucketName": [{"prefix": "pl5-t-"}]}},
        }
        target = _one("aws:cloudwatch/eventTarget:EventTarget", "pl5-pipeline-scan-target")
        assert target["deadLetterConfig"]["arn"].endswith("pl5-pipeline-scan-dlq")
        retries = _one(
            "aws:lambda/functionEventInvokeConfig:FunctionEventInvokeConfig",
            "pl5-pipeline-scan-retries",
        )
        assert retries["destinationConfig"]["onFailure"]["destination"].endswith(
            "pl5-pipeline-scan-dlq"
        )
        dlq = json.loads(
            _one("aws:sqs/queuePolicy:QueuePolicy", "pl5-pipeline-scan-dlq-policy")["policy"]
        )
        assert dlq["Statement"][0]["Principal"] == {"Service": "events.amazonaws.com"}
        assert "aws:SourceArn" in dlq["Statement"][0]["Condition"]["ArnEquals"]

    return _ready(p).apply(check)


def _definition(prefix: str) -> dict[str, Any]:
    sm = _one("aws:sfn/stateMachine:StateMachine", f"{prefix}-pipeline-sfn")
    definition: dict[str, Any] = json.loads(sm["definition"])
    return definition


@pulumi.runtime.test
def test_the_state_machine_never_logs_execution_data_and_only_runs_its_steps() -> Any:
    p = _stack("pl6")

    def check(_: list[Any]) -> None:
        sm = _one("aws:sfn/stateMachine:StateMachine", "pl6-pipeline-sfn")
        assert sm["type"] == "STANDARD"
        # Errors only, and never the execution's data: the grant's token is in that data.
        assert sm["loggingConfiguration"]["includeExecutionData"] is False
        assert sm["loggingConfiguration"]["level"] == "ERROR"
        states = _definition("pl6")["States"]
        resources = {s.get("Resource") for s in states.values() if s["Type"] == "Task"}
        assert resources == {
            "arn:aws:states:::lambda:invoke",
            "arn:aws:states:::ecs:runTask.sync",
        }
        # A failure names its step (fixed text), and passes the error's name, never its Cause.
        assert states["Fingerprint"]["Catch"][0]["Next"] == "FingerprintFailed"
        assert states["FingerprintFailed"] == {
            "Type": "Pass",
            "Result": "fingerprint",
            "ResultPath": "$.failed_step",
            "Next": "Failed",
        }
        payload = states["Failed"]["Parameters"]["Payload"]
        assert payload["failed_step.$"] == "$.failed_step"
        assert payload["error_type.$"] == "$.error.Error"
        assert not any("Cause" in str(v) or v == "$.error" for v in payload.values())
        # Every Lambda step's catch goes through a Pass state that names it. The worker
        # task's goes through `release` first, whose own catch does the same.
        for name, state in states.items():
            for catch in state.get("Catch", []):
                assert catch["ResultPath"] == "$.error", name
                if name == "Worker":
                    assert catch["Next"] == "ReleaseAfterFailure"
                    continue
                assert states[catch["Next"]]["Type"] == "Pass", name
                assert states[catch["Next"]]["Next"] == "Failed", name
        named = {s["Result"] for s in states.values() if s["Type"] == "Pass"}
        assert named == {"fingerprint", "claim", "worker", "companion"}
        from assay_workers.steps import PIPELINE_STEPS

        assert named <= PIPELINE_STEPS
        # Step 9 runs after the task's grant is released, and its failure names it.
        assert states["Release"]["Next"] == "Companion"
        assert states["Companion"]["Parameters"]["Payload"]["step"] == "companion"
        assert states["Companion"]["Next"] == "Done"
        assert states["Companion"]["Catch"][0]["Next"] == "CompanionFailed"
        # Every Lambda step is told which attempt its execution was started for.
        for name, state in states.items():
            if state.get("Resource") == "arn:aws:states:::lambda:invoke":
                assert state["Parameters"]["Payload"]["attempt.$"] == "$.attempt", name

    return _ready(p).apply(check)


def test_only_the_scan_function_publishes_security_notices() -> None:
    """Owner decision 4 (second half): a second topic, apart from the alarms, with its own
    endpoint policy statement."""
    scan_role = f"{ROLE}/assay-dev-pipeline-scan-role"
    topic = json.loads(alerts.security_topic_policy(topic=NOTICES, publisher=scan_role))
    only, tls = topic["Statement"]
    assert only == {
        "Sid": "OnlyTheScanFunctionPublishes",
        "Effect": "Deny",
        "Principal": "*",
        "Action": "sns:Publish",
        "Resource": NOTICES,
        "Condition": {"ArnNotEquals": {"aws:PrincipalArn": scan_role}},
    }
    assert tls["Effect"] == "Deny" and tls["Condition"] == {
        "Bool": {"aws:SecureTransport": "false"}
    }
    # No Allow at all: the scan role's own policy is what lets it publish.
    assert all(s["Effect"] == "Deny" for s in topic["Statement"])

    endpoint = json.loads(alerts.sns_endpoint_policy(topic=NOTICES, publisher=scan_role))
    assert endpoint["Statement"] == [
        {
            "Sid": "ScanFunctionPublishesSecurityNotices",
            "Effect": "Allow",
            "Principal": "*",
            "Action": "sns:Publish",
            "Resource": NOTICES,
            "Condition": {"ArnEquals": {"aws:PrincipalArn": scan_role}},
        }
    ]


@pulumi.runtime.test
def test_security_notices_have_their_own_topic_and_sender_and_no_address_until_one_is_set() -> Any:
    network = Network("sn1", cfg=NET, region=REGION, account_id=ACCOUNT_ID)
    roles = pipeline.pipeline_roles("sn1-pipeline", ACCOUNT_ID, REGION)

    def build(name: str, email: str) -> alerts.SecurityNotices:
        return alerts.SecurityNotices(
            name,
            email=email,
            key_id="key-id",
            publisher_role_arn=roles.scan.arn,
            sns_endpoint_id=network.interface_endpoints["sns"].id,
        )

    blank = build("sn1-notices", "")
    named = build("sn1-notices-b", "security@example.test")

    def check(_: list[Any]) -> None:
        topic = _one("aws:sns/topic:Topic", "sn1-notices-topic")
        assert topic["displayName"] == "Assay Insights security notices"
        assert topic["kmsMasterKeyId"] == "key-id"
        assert _one("aws:sns/topicPolicy:TopicPolicy", "sn1-notices-topic-policy")
        endpoint = _one(
            "aws:ec2/vpcEndpointPolicy:VpcEndpointPolicy", "sn1-notices-sns-endpoint-policy"
        )
        assert endpoint["vpcEndpointId"] == "sn1-sns-id"
        subscriptions = dict(registered("aws:sns/topicSubscription:TopicSubscription", "sn1-"))
        assert set(subscriptions) == {"sn1-notices-b-email"}
        assert subscriptions["sn1-notices-b-email"]["endpoint"] == "security@example.test"
        assert blank.subscription is None and named.subscription is not None

    return pulumi.Output.all(
        blank.topic.arn, blank.policy.id, blank.endpoint_policy.id, named.topic.arn
    ).apply(check)


def test_the_scan_function_is_told_the_security_topic() -> None:
    policy = json.loads(
        pipeline.function_role_policy(
            "scan",
            log_group="arn:log",
            db_user="arn:db/app_tenant",
            ingest_queue="arn:ingest",
            scan_dlq="arn:dlq",
            state_machine="arn:sm",
            security_topic=NOTICES,
            notices_key=NOTICES_KEY,
            region=REGION,
        )
    )
    sids = [s["Sid"] for s in policy["Statement"]]
    assert "PublishSecurityNotices" in sids and "NoticeKeyThroughSnsOnly" in sids
    for function in ("steps", "starter", "sweeper"):
        other = pipeline.function_role_policy(
            function,
            log_group="arn:log",
            db_user="arn:db/app_tenant",
            ingest_queue="arn:ingest",
            scan_dlq="arn:dlq",
            state_machine="arn:sm",
            security_topic=NOTICES,
            notices_key=NOTICES_KEY,
            region=REGION,
        )
        assert "sns:" not in other and "kms:" not in other


@pulumi.runtime.test
def test_the_worker_task_gets_three_values_and_its_result_is_discarded() -> Any:
    p = _stack("pl10")

    def check(_: list[Any]) -> None:
        states = _definition("pl10")["States"]
        worker = states["Worker"]
        assert worker["Resource"] == "arn:aws:states:::ecs:runTask.sync"
        parameters = worker["Parameters"]
        assert parameters["LaunchType"] == "FARGATE"
        assert parameters["NetworkConfiguration"]["AwsvpcConfiguration"]["AssignPublicIp"] == (
            "DISABLED"
        )
        [override] = parameters["Overrides"]["ContainerOverrides"]
        assert override["Name"] == pipeline.WORKER_CONTAINER
        # The document, the attempt and the token. No tenant: the worker never names one.
        assert override["Environment"] == [
            {"Name": "DOCUMENT_ID", "Value.$": "$.document_id"},
            {"Name": "ATTEMPT", "Value.$": "States.Format('{}', $.claim.result.attempt)"},
            {"Name": "WORKER_TOKEN", "Value.$": "$.claim.result.token"},
        ]
        assert "tenant" not in json.dumps(override).lower()
        # The task's description repeats the overrides: it is not kept in the state.
        assert worker["ResultPath"] is None
        assert worker["TimeoutSeconds"] == pipeline.WORKER_TIMEOUT_SECONDS
        # The token leaves the state as soon as the task is over, either way.
        for release in ("Release", "ReleaseAfterFailure"):
            assert states[release]["ResultPath"] == "$.claim"
            assert states[release]["Parameters"]["Payload"]["step"] == "release"
        assert worker["Next"] == "Release"

    return _ready(p).apply(check)


@pulumi.runtime.test
def test_a_document_waits_for_a_slot_or_for_capacity_and_never_fails_for_either() -> Any:
    p = _stack("pl11")

    def check(_: list[Any]) -> None:
        definition = _definition("pl11")
        states = definition["States"]
        # No slot free: wait and claim again.
        waits = {c["StringEquals"]: c["Next"] for c in states["Claimed"]["Choices"]}
        assert waits == {"claimed": "Worker", "wait": "WaitForSlot"}
        assert states["WaitForSlot"] == {
            "Type": "Wait",
            "Seconds": pipeline.SLOT_WAIT_SECONDS,
            "Next": "Claim",
        }
        # The task failed. Only if it actually started is that the document's failure.
        started = states["WorkerStarted"]
        assert started["Choices"] == [
            {
                "Variable": "$.claim.result.worker_started",
                "BooleanEquals": True,
                "Next": "WorkerFailed",
            }
        ]
        assert started["Default"] == "WaitForCapacity"
        assert states["WaitForCapacity"] == {
            "Type": "Wait",
            "Seconds": pipeline.CAPACITY_WAIT_SECONDS,
            "Next": "Claim",
        }
        # The RunTask call itself is retried first.
        [retry] = states["Worker"]["Retry"]
        assert "ECS.AmazonECSException" in retry["ErrorEquals"]
        # No path from a wait to `Failed`: only the execution's own timeout ends the waiting,
        # and that leaves the document `processing`.
        assert states["WaitForSlot"]["Next"] == states["WaitForCapacity"]["Next"] == "Claim"
        assert definition["TimeoutSeconds"] == pipeline.EXECUTION_TIMEOUT_SECONDS
        # Within a Standard execution's 25,000 history events, at about 13 a wait.
        loops = pipeline.EXECUTION_TIMEOUT_SECONDS / pipeline.SLOT_WAIT_SECONDS
        assert loops * 13 < 25_000

    return _ready(p).apply(check)


@pulumi.runtime.test
def test_the_state_machine_may_pass_only_the_workers_two_roles() -> Any:
    p = _stack("pl12")

    def check(_: list[Any]) -> None:
        policy = _policy(ROLE_POLICY, "pl12-pipeline-sfn-policy")
        by_sid = {s["Sid"]: s for s in policy["Statement"]}
        assert _actions(policy) - {a for a in _actions(policy) if a.startswith("logs:")} == {
            "lambda:InvokeFunction",
            "ecs:RunTask",
            "ecs:StopTask",
            "ecs:DescribeTasks",
            "iam:PassRole",
            "events:PutTargets",
            "events:PutRule",
            "events:DescribeRule",
        }
        passed = by_sid["PassTheWorkerTaskRoles"]
        assert len(passed["Resource"]) == 2 and "*" not in json.dumps(passed["Resource"])
        assert all("worker" in arn for arn in passed["Resource"])
        assert passed["Condition"] == {
            "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
        }
        run = by_sid["RunTheWorkerTask"]
        assert run["Resource"].endswith(":task-definition/pl12-pipeline-worker:*")
        assert "ecs:cluster" in run["Condition"]["ArnEquals"]
        assert by_sid["WatchAndStopIt"]["Resource"].endswith(":task/pl12-pipeline-worker/*")
        assert by_sid["TaskStateChangeRule"]["Resource"].endswith(
            ":rule/StepFunctionsGetEventsForECSTaskRule"
        )

    return _ready(p).apply(check)


@pulumi.runtime.test
def test_the_worker_role_may_only_sign_in_as_app_worker() -> Any:
    p = _stack("pl13")

    def check(_: list[Any]) -> None:
        policy = _policy(ROLE_POLICY, "pl13-pipeline-worker-task-policy")
        # Nothing of its own but the database sign-in. No storage, no key, no queue, no AI
        # service (those arrive with the stages that use them), not even logs: the task's
        # execution role writes them.
        assert _actions(policy) == {"rds-db:connect"}
        [statement] = policy["Statement"]
        assert statement["Resource"].endswith("/app_worker")
        assert not statement["Resource"].endswith("/app_tenant")
        role = _one("aws:iam/role:Role", "pl13-pipeline-worker-task-role")
        trust = json.loads(role["assumeRolePolicy"])
        assert trust["Statement"][0]["Principal"] == {"Service": "ecs-tasks.amazonaws.com"}
        execution = _policy(ROLE_POLICY, "pl13-pipeline-worker-execution-policy")
        assert _actions(execution) == {
            "ecr:GetAuthorizationToken",
            "ecr:BatchCheckLayerAvailability",
            "ecr:BatchGetImage",
            "ecr:GetDownloadUrlForLayer",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
        }

    return _ready(p).apply(check)


@pulumi.runtime.test
def test_the_worker_container_has_three_capabilities_and_a_read_only_root() -> Any:
    p = _stack("pl14")

    def check(_: list[Any]) -> None:
        task = _one("aws:ecs/taskDefinition:TaskDefinition", "pl14-pipeline-worker-task")
        assert task["requiresCompatibilities"] == ["FARGATE"]
        assert task["runtimePlatform"]["cpuArchitecture"] == "X86_64"
        assert task["networkMode"] == "awsvpc"
        [container] = json.loads(task["containerDefinitions"])
        assert container["readonlyRootFilesystem"] is True
        assert container["privileged"] is False
        assert container["user"] == "0:0"
        capabilities = container["linuxParameters"]["capabilities"]
        assert capabilities["add"] == []
        dropped = set(capabilities["drop"])
        # Everything but the three the sandbox needs to start and stop its child.
        assert set(pipeline.ALL_CAPABILITIES) - dropped == {"SETUID", "SETGID", "KILL"}
        assert {"NET_RAW", "NET_ADMIN", "SYS_ADMIN", "SYS_PTRACE", "DAC_OVERRIDE", "CHOWN"} <= (
            dropped
        )
        assert container["mountPoints"] == [
            {"sourceVolume": "work", "containerPath": "/work", "readOnly": False}
        ]
        names = {e["name"] for e in container["environment"]}
        assert names == set(pipeline.WORKER_VARIABLES)
        # Nothing about any document or tenant, and no credentials, in the definition.
        assert not names & {"DOCUMENT_ID", "ATTEMPT", "WORKER_TOKEN", "TENANT_ID"}
        assert container["logConfiguration"]["options"]["awslogs-group"] == (
            "/aws/ecs/pl14-pipeline-worker"
        )

    return _ready(p).apply(check)


def test_the_worker_definition_refuses_a_missing_setting() -> None:
    import pytest

    environment = {name: "x" for name in pipeline.WORKER_VARIABLES}
    del environment["TSS_ENDPOINT_URL"]
    with pytest.raises(pipeline.WorkerEnvironmentError, match="TSS_ENDPOINT_URL"):
        pipeline.worker_container_definitions(
            image="i", log_group="g", region=REGION, environment=environment
        )


def test_the_dockerfile_and_the_sandbox_agree_on_the_user() -> None:
    from assay_workers.sandbox import runner

    dockerfile = (REPO_ROOT / pipeline.WORKER_DOCKERFILE).read_text(encoding="utf-8")
    assert f"--gid {runner.SANDBOX_GID} sandbox" in dockerfile
    assert f"--uid {runner.SANDBOX_UID} --gid sandbox" in dockerfile
    assert "USER 0:0" in dockerfile and pipeline.WORKER_USER == "0:0"
    assert f"install -d -m 0755 /opt/rds-ca {pipeline.WORK_DIR}" in dockerfile


@pulumi.runtime.test
def test_every_alarm_notifies_the_alarm_topic() -> Any:
    p = _stack("pl7")

    def check(_: list[Any]) -> None:
        alarms = registered("aws:cloudwatch/metricAlarm:MetricAlarm", "pl7-pipeline")
        names = {inputs["name"] for _name, inputs in alarms}
        assert {"pl7-pipeline-ingest-dlq-depth", "pl7-pipeline-scan-dlq-depth"} <= names
        assert {"pl7-pipeline-executionsfailed", "pl7-pipeline-executionstimedout"} <= names
        for function in pipeline.FUNCTIONS:
            assert {f"pl7-pipeline-{function}-errors", f"pl7-pipeline-{function}-failed"} <= names
        assert "pl7-pipeline-worker-errors" in names
        for _name, inputs in alarms:
            assert inputs["alarmActions"] == [TOPIC], inputs["name"]

    return _ready(p).apply(check)


@pulumi.runtime.test
def test_the_sweeper_runs_every_five_minutes() -> Any:
    p = _stack("pl8")

    def check(_: list[Any]) -> None:
        schedule = _one("aws:scheduler/schedule:Schedule", "pl8-pipeline-sweep")
        assert schedule["scheduleExpression"] == "rate(5 minutes)"
        assert schedule["target"]["arn"].endswith("pl8-pipeline-sweeper-fn")
        policy = json.loads(
            _one("aws:iam/rolePolicy:RolePolicy", "pl8-pipeline-sweep-schedule-policy")["policy"]
        )
        assert _actions(policy) == {"lambda:InvokeFunction"}

    return _ready(p).apply(check)


@pulumi.runtime.test
def test_api_task_role_keeps_its_name_description_and_trust() -> Any:
    """Condition (h): the role moved out of ApiService, with an alias to its old parent, so
    the preview shows no replacement. Its name, description and trust policy are as before;
    the preview against the real stack is the proof."""
    role = api_task_role("pl9-api", account_id=ACCOUNT_ID, region=REGION)
    assert isinstance(role, aws.iam.Role)

    def check(_: Any) -> None:
        [inputs] = [i for n, i in registered("aws:iam/role:Role") if n == "pl9-api-task-role"]
        assert inputs["description"] == (
            "API task: database sign-in, Cognito admin calls, tenant provisioning"
        )
        trust = json.loads(inputs["assumeRolePolicy"])
        assert trust["Statement"][0]["Principal"] == {"Service": "ecs-tasks.amazonaws.com"}

    return role.id.apply(check)


# --- Endpoint policies (pure) -----------------------------------------------------------------


def test_the_sqs_endpoint_names_every_queue_and_sender() -> None:
    policy = json.loads(
        sqs_endpoint_policy(
            queue="arn:auth",
            trigger_role="arn:trigger",
            grants=[
                ("PipelineQueuesIngestJobs", "arn:ingest", ["arn:scan", "arn:api", "arn:sweeper"])
            ],
        )
    )
    by_sid = {s["Sid"]: s for s in policy["Statement"]}
    assert set(by_sid) == {"TriggerQueuesAuthEvents", "PipelineQueuesIngestJobs"}
    ingest = by_sid["PipelineQueuesIngestJobs"]
    assert ingest["Resource"] == "arn:ingest" and ingest["Action"] == ["sqs:SendMessage"]
    assert ingest["Condition"] == {
        "ArnEquals": {"aws:PrincipalArn": ["arn:api", "arn:scan", "arn:sweeper"]}
    }
    # Unchanged from build step 5: one sender, written as a string.
    assert by_sid["TriggerQueuesAuthEvents"]["Condition"] == {
        "ArnEquals": {"aws:PrincipalArn": "arn:trigger"}
    }


def test_the_states_endpoint_lets_only_the_starter_start_the_pipeline() -> None:
    policy = json.loads(
        states_endpoint_policy(
            state_machine="arn:provision",
            callers=["arn:api", "arn:seed"],
            pipeline_state_machine="arn:pipeline",
            pipeline_callers=["arn:starter"],
        )
    )
    first, second = policy["Statement"]
    assert first["Sid"] == "StartTenantProvisioning" and first["Resource"] == "arn:provision"
    assert second == {
        "Sid": "StartDocumentIngestion",
        "Effect": "Allow",
        "Principal": "*",
        "Action": "states:StartExecution",
        "Resource": "arn:pipeline",
        "Condition": {"ArnEquals": {"aws:PrincipalArn": "arn:starter"}},
    }
