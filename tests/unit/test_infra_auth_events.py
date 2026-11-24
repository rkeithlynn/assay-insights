"""Auth-event queue, trigger and consumer; the seed Lambda; the states endpoint (Pulumi mocks)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pulumi

from assay_infra import auth_events, seed
from assay_infra.auth import Auth
from assay_infra.auth_events import AuthEvents
from assay_infra.database import Database
from assay_infra.network import Network, states_endpoint_policy
from tests.unit.conftest import ACCOUNT_ID, REGION, registered
from tests.unit.test_infra_auth_api import AUTH, DNS, FAKE_JWKS
from tests.unit.test_infra_data import DB, NET

REPO_ROOT = Path(__file__).resolve().parents[2]
TOPIC = f"arn:aws:sns:{REGION}:{ACCOUNT_ID}:alarms"
ROLE = f"arn:aws:iam::{ACCOUNT_ID}:role"


def _one(type_token: str, name: str) -> dict[str, Any]:
    matches = [inputs for n, inputs in registered(type_token) if n == name]
    assert len(matches) == 1, f"{type_token} {name}: {len(matches)} registered"
    return matches[0]


def _stack(prefix: str) -> AuthEvents:
    network = Network(prefix, cfg=NET, region=REGION, account_id=ACCOUNT_ID)
    database = Database(f"{prefix}-db", cfg=DB, network=network, account_id=ACCOUNT_ID)
    events = AuthEvents(
        f"{prefix}-auth-events",
        repo_root=REPO_ROOT,
        region=REGION,
        account_id=ACCOUNT_ID,
        network=network,
        alarm_topic_arn=TOPIC,
    )
    auth = Auth(
        f"{prefix}-auth",
        cfg=AUTH,
        dns=DNS,
        region=REGION,
        account_id=ACCOUNT_ID,
        repo_root=REPO_ROOT,
        auth_event_lambda_arn=events.trigger_function.arn,
        jwks_fetcher=lambda region, pool_id: FAKE_JWKS,
    )
    events.attach(f"{prefix}-auth-events-tenant", auth.tenant.pool, ACCOUNT_ID)
    events.attach(f"{prefix}-auth-events-platform", auth.platform.pool, ACCOUNT_ID)
    events.add_consumer(
        image_uri="repo@sha256:x",
        database=database,
        database_name=DB.database_name,
        pool_ids=(auth.tenant.pool.id, auth.platform.pool.id),
    )
    events.sqs_endpoint_access()
    return events


def _ready(events: AuthEvents) -> pulumi.Output[list[Any]]:
    return pulumi.Output.all(
        events.trigger_function.id,
        events.consumer_function.id,
        events.sqs_endpoint_policy.policy,
        events.trigger_error_filter.id,
        events.consumer_source.id,
        *[a.id for a in events.alarms],
    )


@pulumi.runtime.test
def test_trigger_only_queues_and_never_waits_for_the_database() -> Any:
    events = _stack("q1")

    def check(_: list[Any]) -> None:
        trigger = _one("aws:lambda/function:Function", "q1-auth-events-trigger-fn")
        assert trigger["timeout"] == 5
        assert trigger["vpcConfig"]["subnetIds"] == ["q1-app-use1-az2-id"]
        assert set(trigger["environment"]["variables"]) == {"QUEUE_URL"}
        policy = json.loads(
            _one("aws:iam/rolePolicy:RolePolicy", "q1-auth-events-trigger-policy")["policy"]
        )
        actions = {
            a
            for s in policy["Statement"]
            for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
        }
        assert "sqs:SendMessage" in actions
        assert not any(a.startswith(("rds-db:", "cognito-idp:")) for a in actions)

        invokes = [
            i
            for n, i in registered("aws:lambda/permission:Permission")
            if n.startswith("q1-auth-events-")
        ]
        assert sorted(i["sourceArn"] for i in invokes) == sorted(
            [
                f"arn:aws:mock:{REGION}:{ACCOUNT_ID}:q1-auth-tenant-pool",
                f"arn:aws:mock:{REGION}:{ACCOUNT_ID}:q1-auth-platform-pool",
            ]
        )
        assert {i["principal"] for i in invokes} == {"cognito-idp.amazonaws.com"}

    return _ready(events).apply(check)


@pulumi.runtime.test
def test_queue_dead_letters_and_admits_only_the_trigger() -> Any:
    events = _stack("q2")

    def check(_: list[Any]) -> None:
        queue = _one("aws:sqs/queue:Queue", "q2-auth-events-queue")
        assert queue["sqsManagedSseEnabled"] is True
        redrive = json.loads(queue["redrivePolicy"])
        assert redrive["maxReceiveCount"] == 5
        assert redrive["deadLetterTargetArn"].endswith("q2-auth-events-dlq")

        queue_policy = json.loads(
            _one("aws:sqs/queuePolicy:QueuePolicy", "q2-auth-events-queue-policy")["policy"]
        )
        deny = next(s for s in queue_policy["Statement"] if s["Sid"] == "OnlyTheTriggerSends")
        assert deny["Effect"] == "Deny"
        assert deny["Condition"]["ArnNotEquals"]["aws:PrincipalArn"].endswith(
            "q2-auth-events-trigger-role-a1b2c3d"
        )

        # Owner condition 4: the SQS endpoint exists (build step 2) and now has a policy.
        endpoint = _one(
            "aws:ec2/vpcEndpointPolicy:VpcEndpointPolicy", "q2-auth-events-sqs-endpoint-policy"
        )
        assert endpoint["vpcEndpointId"] == "q2-sqs-id"
        [statement] = json.loads(endpoint["policy"])["Statement"]
        assert statement["Action"] == ["sqs:SendMessage"]
        assert statement["Resource"].endswith("q2-auth-events-queue")
        assert statement["Condition"]["ArnEquals"]["aws:PrincipalArn"].endswith(
            "q2-auth-events-trigger-role-a1b2c3d"
        )

    return _ready(events).apply(check)


@pulumi.runtime.test
def test_consumer_signs_in_as_audit_writer_only_and_reports_failures() -> Any:
    events = _stack("q3")

    def check(_: list[Any]) -> None:
        consumer = _one("aws:lambda/function:Function", "q3-auth-events-consumer-fn")
        assert consumer["imageConfig"]["commands"] == ["assay_api.auth_event_consumer.handler"]
        assert consumer["vpcConfig"]["subnetIds"] == ["q3-app-use1-az2-id"]
        policy = json.loads(
            _one("aws:iam/rolePolicy:RolePolicy", "q3-auth-events-consumer-policy")["policy"]
        )
        connect = next(s for s in policy["Statement"] if s["Sid"] == "ConnectAsAuditWriter")
        assert connect["Resource"].endswith(":dbuser:cluster-MOCKRESOURCEID/audit_writer")
        source = _one(
            "aws:lambda/eventSourceMapping:EventSourceMapping", "q3-auth-events-consumer-source"
        )
        assert source["functionResponseTypes"] == ["ReportBatchItemFailures"]

        alarms = [
            (n, i)
            for n, i in registered("aws:cloudwatch/metricAlarm:MetricAlarm")
            if n.startswith("q3-")
        ]
        assert len(alarms) == 4
        assert all(i["alarmActions"] == [TOPIC] for _, i in alarms)
        dlq = _one("aws:cloudwatch/metricAlarm:MetricAlarm", "q3-auth-events-dlq-depth-alarm")
        assert dlq["metricName"] == "ApproximateNumberOfMessagesVisible"
        errors = _one(
            "aws:cloudwatch/logMetricFilter:LogMetricFilter", "q3-auth-events-trigger-errors"
        )
        assert errors["pattern"] == auth_events.ERROR_LINES

    return _ready(events).apply(check)


def test_states_endpoint_allows_only_starting_the_state_machine() -> None:
    policy = json.loads(
        states_endpoint_policy(state_machine="arn:sm", callers=[f"{ROLE}/seed", f"{ROLE}/api"])
    )
    [statement] = policy["Statement"]
    assert statement["Action"] == "states:StartExecution"
    assert statement["Resource"] == "arn:sm"
    assert statement["Condition"] == {
        "ArnEquals": {"aws:PrincipalArn": [f"{ROLE}/api", f"{ROLE}/seed"]}
    }


def test_seed_role_has_only_its_four_permissions() -> None:
    policy = json.loads(
        seed.seed_role_policy(
            log_group="arn:lg",
            db_user="arn:dbuser/platform_writer",
            cognito_admin_function="arn:fn",
            state_machine="arn:sm",
        )
    )
    sids = [s["Sid"] for s in policy["Statement"]]
    assert sids == [
        "Logs",
        "VpcNetworkInterfaces",
        "ConnectAsPlatformWriter",
        "InviteTenantUsers",
        "StartTenantProvisioning",
    ]
    by_sid = {s["Sid"]: s for s in policy["Statement"]}
    assert by_sid["InviteTenantUsers"]["Resource"] == "arn:fn"
    assert by_sid["StartTenantProvisioning"]["Action"] == "states:StartExecution"
    assert by_sid["ConnectAsPlatformWriter"]["Resource"] == "arn:dbuser/platform_writer"
