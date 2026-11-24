"""The ingestion pipeline's infrastructure, first half of build step 6 (Pulumi mocks).

What it must never get: an S3, KMS or AI-service permission of its own (tenant data is
reached only with TSS-issued credentials), a queue anyone may send to, a state machine that
logs execution data, or an alarm that notifies no one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pulumi
import pulumi_aws as aws

from assay_infra import pipeline
from assay_infra.api import api_task_role
from assay_infra.auth_events import sqs_endpoint_policy
from assay_infra.database import Database
from assay_infra.network import Network, states_endpoint_policy
from tests.unit.conftest import ACCOUNT_ID, REGION, registered
from tests.unit.test_infra_data import DB, NET

REPO_ROOT = Path(__file__).resolve().parents[2]
TOPIC = f"arn:aws:sns:{REGION}:{ACCOUNT_ID}:alarms"
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
        roles=pipeline.pipeline_roles(f"{prefix}-pipeline", ACCOUNT_ID),
        tss_endpoint_url=TSS,
        api_task_role=api_task_role(f"{prefix}-api", account_id=ACCOUNT_ID, region=REGION),
        alarm_topic_arn=TOPIC,
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
            actions = _actions(policy)
            for forbidden in FORBIDDEN_SERVICES:
                assert not any(a.startswith(forbidden) for a in actions), (function, forbidden)
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


@pulumi.runtime.test
def test_the_state_machine_never_logs_execution_data_and_only_runs_its_steps() -> Any:
    p = _stack("pl6")

    def check(_: list[Any]) -> None:
        sm = _one("aws:sfn/stateMachine:StateMachine", "pl6-pipeline-sfn")
        assert sm["type"] == "STANDARD"
        assert sm["loggingConfiguration"]["includeExecutionData"] is False
        assert sm["loggingConfiguration"]["level"] == "ERROR"
        definition = json.loads(sm["definition"])
        resources = {
            s.get("Resource") for s in definition["States"].values() if s["Type"] == "Task"
        }
        assert resources == {"arn:aws:states:::lambda:invoke"}
        assert definition["States"]["Fingerprint"]["Catch"][0]["Next"] == "Failed"
        policy = json.loads(
            _one("aws:iam/rolePolicy:RolePolicy", "pl6-pipeline-sfn-policy")["policy"]
        )
        assert _actions(policy) - {a for a in _actions(policy) if a.startswith("logs:")} == {
            "lambda:InvokeFunction"
        }

    return _ready(p).apply(check)


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
