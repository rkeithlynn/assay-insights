"""scripts/check_deployment.py: a configured log that has received nothing fails the check,
and a Lambda concurrency limit below what the pipeline needs is warned about."""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from botocore.exceptions import ClientError

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_deployment.py"
ACCOUNT = "111122223333"
END = dt.datetime(2026, 11, 25, 12, 0, tzinfo=dt.UTC)
START = END - dt.timedelta(days=14)
DELIVERY_DESTINATION = f"arn:aws:logs:us-east-1:{ACCOUNT}:delivery-destination"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_deployment", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_deployment"] = module
    spec.loader.exec_module(module)
    return module


def not_found(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Operation")


class Fake:
    """One stand-in for every client: canned answers by operation name."""

    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, operation: str) -> Any:
        if operation not in self.answers:
            raise AttributeError(operation)

        def call(**kwargs: Any) -> Any:
            self.calls.append((operation, kwargs))
            answer = self.answers[operation]
            if isinstance(answer, Exception):
                raise answer
            return answer(**kwargs) if callable(answer) else answer

        return call


def clients(**by_name: Fake) -> Any:
    return lambda name: by_name[name]


# --- AI services: opt-out policy and model invocation logging ---------------------------------


def effective(services: Any) -> Fake:
    import json

    content = services if isinstance(services, str) else json.dumps({"services": services})
    return Fake(
        describe_effective_policy={
            "EffectivePolicy": {
                "PolicyContent": content,
                "LastUpdatedTimestamp": dt.datetime(2026, 11, 24, 4, 37, 58, tzinfo=dt.UTC),
                "PolicyType": "AISERVICES_OPT_OUT_POLICY",
            }
        }
    )


OUT = {"opt_out_policy": "optOut"}
IN = {"opt_out_policy": "optIn"}
LOGGING_OFF = Fake(get_model_invocation_logging_configuration={"ResponseMetadata": {}})


def test_a_default_of_opt_out_covers_textract_and_rekognition_by_name(script: ModuleType) -> None:
    """What the Development account has: one default, no service of its own."""
    organizations = effective({"default": OUT})
    passed, lines = script.opt_out_report(organizations)
    text = "\n".join(lines)
    assert passed
    assert "last updated 2026-11-24 04:37:58 UTC" in text
    assert "ok: default: optOut" in text
    assert "ok: textract: optOut (the default applies" in text
    assert "ok: rekognition: optOut (the default applies" in text
    assert organizations.calls == [
        ("describe_effective_policy", {"PolicyType": "AISERVICES_OPT_OUT_POLICY"})
    ]


@pytest.mark.parametrize(
    ("services", "wrong"),
    [
        # The service this stage sends content to is opted in.
        ({"default": OUT, "textract": IN}, "FAILED: textract: optIn"),
        # Any service opted in fails, whichever it is.
        ({"default": OUT, "lex": IN}, "FAILED: lex: optIn"),
        # The default is in, and Textract has no setting of its own.
        ({"default": IN}, "FAILED: textract: no setting of its own, and the default is optIn"),
        # No default at all: a service the policy does not name is in.
        ({"textract": OUT}, "FAILED: the policy sets no default"),
        ({"textract": OUT}, "FAILED: rekognition: no setting of its own"),
        # A value that is not the exact word.
        ({"default": {"opt_out_policy": "optout"}}, "FAILED: default: optout"),
    ],
)
def test_any_service_that_is_not_opted_out_fails(
    script: ModuleType, services: dict[str, Any], wrong: str
) -> None:
    passed, lines = script.opt_out_report(effective(services))
    assert not passed
    assert wrong in "\n".join(lines)


def test_a_service_with_its_own_opt_out_is_reported_by_that_setting(script: ModuleType) -> None:
    passed, lines = script.opt_out_report(effective({"default": OUT, "Textract": OUT}))
    text = "\n".join(lines)
    assert passed
    assert "ok: textract: optOut" in text
    assert "textract: optOut (the default applies" not in text


def test_no_policy_in_effect_fails(script: ModuleType) -> None:
    organizations = Fake(describe_effective_policy=not_found("EffectivePolicyNotFoundException"))
    passed, lines = script.opt_out_report(organizations)
    assert not passed
    assert "no AI services opt-out policy is in effect" in "\n".join(lines)


@pytest.mark.parametrize("content", ["not json", "{}", '{"services": {"default": "optOut"}}'])
def test_a_policy_the_check_cannot_read_fails(script: ModuleType, content: str) -> None:
    passed, lines = script.opt_out_report(effective(content))
    assert not passed
    assert "not in the form this check reads" in "\n".join(lines)


def test_an_error_reading_the_policy_is_not_taken_for_an_answer(script: ModuleType) -> None:
    organizations = Fake(describe_effective_policy=not_found("AccessDeniedException"))
    with pytest.raises(ClientError):
        script.opt_out_report(organizations)


def test_no_invocation_logging_configuration_is_off(script: ModuleType) -> None:
    passed, lines = script.invocation_logging_report(LOGGING_OFF, "us-east-1")
    assert passed
    assert lines[0] == "Bedrock model invocation logging (us-east-1):"
    assert "ok: off" in lines[1]


@pytest.mark.parametrize(
    "config",
    [
        {
            "cloudWatchConfig": {"logGroupName": "g", "roleArn": "r"},
            "textDataDeliveryEnabled": True,
        },
        {"s3Config": {"bucketName": "b"}},
        # Every kind of data switched off is still a configuration: one setting from logging.
        {"textDataDeliveryEnabled": False, "imageDataDeliveryEnabled": False},
    ],
)
def test_any_invocation_logging_configuration_fails_and_shows_no_value(
    script: ModuleType, config: dict[str, Any]
) -> None:
    bedrock = Fake(get_model_invocation_logging_configuration={"loggingConfig": config})
    passed, lines = script.invocation_logging_report(bedrock, "us-east-1")
    text = "\n".join(lines)
    assert not passed
    assert "FAILED: a logging configuration exists" in text
    for name in config:
        assert name in text
    # Names only: no bucket, log group or role is printed.
    assert "logGroupName" not in text and "bucketName" not in text


@pytest.mark.parametrize(
    ("services", "logging", "expected"),
    [
        ({"default": OUT}, {}, True),
        ({"default": IN}, {}, False),
        ({"default": OUT}, {"loggingConfig": {"s3Config": {}}}, False),
        ({"default": IN}, {"loggingConfig": {"s3Config": {}}}, False),
    ],
)
def test_both_settings_must_hold_and_both_are_always_reported(
    script: ModuleType, services: dict[str, Any], logging: dict[str, Any], expected: bool
) -> None:
    bedrock = Fake(get_model_invocation_logging_configuration=logging)
    passed, lines = script.ai_services_report(effective(services), bedrock, "us-east-1")
    text = "\n".join(lines)
    assert passed is expected
    # One failure never hides the other: both sections are there either way.
    assert "AI services opt-out policy" in text and "Bedrock model invocation logging" in text
    assert ("AI services: ok." in text) is expected
    assert ("No document content may go to Textract" in text) is not expected


def test_the_ai_services_check_runs_first_and_fails_every_mode() -> None:
    """It gates content going to an AI service, so no mode of the script may skip it or pass
    without it: `--ai-services`, `--before-deploy` and the full run."""
    source = SCRIPT.read_text(encoding="utf-8")
    main = source[source.index("def main() -> int:") :]
    check = main.index("ai_services_report(")
    assert check < main.index("concurrency_report(") < main.index("reservation_report(")
    assert "return 0 if ai_ok else 1" in main[check : main.index("outputs = json.loads")]
    assert main.count("return 0 if ai_ok else 1") == 2  # --ai-services and --before-deploy
    assert "return 0 if passed and reservations_ok and ai_ok else 1" in main


# --- Lambda concurrency -----------------------------------------------------------------------


def settings(limit: int, unreserved: int | None = None) -> Fake:
    account = {"ConcurrentExecutions": limit, "UnreservedConcurrentExecutions": unreserved}
    return Fake(get_account_settings={"AccountLimit": account})


def test_the_pipeline_needs_its_two_reservations_and_room_for_everything_else(
    script: ModuleType,
) -> None:
    """Owner decision, 2026-11-25: 50 for the scan function, 300 for the TSS. Beside them, a
    full batch's 200 pipeline steps and 200 for every other function stay unreserved."""
    assert script.DECIDED == {"scan": 50, "tss": 300}
    assert script.UNRESERVED_NEEDED == 400
    assert script.concurrency_needed({"scan": 50, "tss": 300}) == 750
    # A stack that sets neither is measured against the decided values.
    assert script.concurrency_needed({"scan": None, "tss": None}) == 750
    assert script.concurrency_needed({"scan": 100, "tss": 400}) == 900


def test_a_limit_below_the_reservations_says_to_leave_them_unset(script: ModuleType) -> None:
    """Staging and Production: the quota has to be raised before the settings can be set."""
    quotas = Fake(list_requested_service_quota_change_history_by_quota={"RequestedQuotas": []})
    enough, lines = script.concurrency_report(settings(10), quotas, {"scan": 50, "tss": 300})
    text = "\n".join(lines)
    assert not enough
    assert "needs 750" in text
    assert "unset until the quota is raised" in text
    assert "--desired-value 1000" in text
    enough, _ = script.concurrency_report(settings(749), quotas, {"scan": 50, "tss": 300})
    assert not enough
    enough, _ = script.concurrency_report(settings(750), quotas, {"scan": 50, "tss": 300})
    assert enough


def reserving(script: ModuleType, scan: int | None, tss: int | None, unreserved: int = 650) -> Fake:
    held = {"assay-x-pipeline-scan": scan, "assay-x-tss": tss}

    def concurrency(FunctionName: str) -> dict[str, int]:
        value = held[FunctionName]
        return {} if value is None else {"ReservedConcurrentExecutions": value}

    account = {"ConcurrentExecutions": 1000, "UnreservedConcurrentExecutions": unreserved}
    return Fake(
        get_function_concurrency=concurrency, get_account_settings={"AccountLimit": account}
    )


FUNCTIONS = {"scan": "assay-x-pipeline-scan", "tss": "assay-x-tss"}


def test_both_reservations_are_confirmed_against_the_stacks_settings(script: ModuleType) -> None:
    client = reserving(script, 50, 300)
    passed, lines = script.reservation_report(client, FUNCTIONS, {"scan": 50, "tss": 300})
    text = "\n".join(lines)
    assert passed
    assert "the scan function (assay-x-pipeline-scan) reserves 50, as the stack says" in text
    assert "the Tenant Storage Service (assay-x-tss) reserves 300, as the stack says" in text
    assert "650 left unreserved" in text
    asked = [kwargs["FunctionName"] for name, kwargs in client.calls if "function" in name]
    assert asked == ["assay-x-pipeline-scan", "assay-x-tss"]


@pytest.mark.parametrize(
    ("scan", "tss", "wrong"),
    [
        (None, 300, "the scan function (assay-x-pipeline-scan) reserves nothing"),
        (50, None, "the Tenant Storage Service (assay-x-tss) reserves nothing"),
        (10, 300, "the scan function (assay-x-pipeline-scan) reserves 10"),
        (50, 200, "the Tenant Storage Service (assay-x-tss) reserves 200"),
    ],
)
def test_a_reservation_that_differs_from_the_stack_fails(
    script: ModuleType, scan: int | None, tss: int | None, wrong: str
) -> None:
    passed, lines = script.reservation_report(
        reserving(script, scan, tss), FUNCTIONS, {"scan": 50, "tss": 300}
    )
    text = "\n".join(lines)
    assert not passed
    assert f"FAILED: {wrong}" in text


def test_a_reservation_the_stack_does_not_set_fails_too(script: ModuleType) -> None:
    """Set by hand in the console: the stack would remove it at the next deploy."""
    passed, lines = script.reservation_report(
        reserving(script, 50, 300), FUNCTIONS, {"scan": None, "tss": None}
    )
    assert not passed
    assert "the stack says nothing" in "\n".join(lines)


def test_a_stack_that_sets_neither_passes_with_a_warning(script: ModuleType) -> None:
    passed, lines = script.reservation_report(
        reserving(script, None, None, unreserved=10), FUNCTIONS, {"scan": None, "tss": None}
    )
    text = "\n".join(lines)
    assert passed
    assert text.count("WARNING") == 3  # both functions, and too little left unreserved
    assert "once the account's quota allows it" in text


def test_the_stacks_settings_are_read_from_its_config(script: ModuleType) -> None:
    values = {"scanReservedConcurrency": "50", "tssReservedConcurrency": "300", "other": "x"}
    assert script.configured_reservations(values) == {"scan": 50, "tss": 300}
    assert script.configured_reservations({"other": "x"}) == {"scan": None, "tss": None}


def test_a_limit_that_covers_the_pipeline_passes_without_asking_about_requests(
    script: ModuleType,
) -> None:
    enough, lines = script.concurrency_report(settings(1000), Fake())
    assert enough
    assert "WARNING" not in "\n".join(lines)


def test_a_new_accounts_limit_is_warned_about_with_the_request_to_make(
    script: ModuleType,
) -> None:
    quotas = Fake(list_requested_service_quota_change_history_by_quota={"RequestedQuotas": []})
    enough, lines = script.concurrency_report(settings(10), quotas)
    text = "\n".join(lines)
    assert not enough
    assert "WARNING" in text and "allows 10." in text
    assert "request-service-quota-increase" in text and "L-B99A9384" in text


def test_a_request_still_open_is_reported_instead(script: ModuleType) -> None:
    quotas = Fake(
        list_requested_service_quota_change_history_by_quota={
            "RequestedQuotas": [
                {"Status": "DENIED", "DesiredValue": 500.0, "Created": END - dt.timedelta(days=9)},
                {"Status": "CASE_OPENED", "DesiredValue": 1000.0, "Created": END},
            ]
        }
    )
    enough, lines = script.concurrency_report(settings(10), quotas)
    text = "\n".join(lines)
    assert not enough
    assert "An increase to 1,000 was requested on 2026-11-25 and is CASE_OPENED" in text
    assert "request-service-quota-increase" not in text


# --- Has anything arrived? ------------------------------------------------------------------


def streams(*stamps: int | None) -> Fake:
    found = [{"logStreamName": "s"} | ({"lastIngestionTime": s} if s else {}) for s in stamps]
    return Fake(describe_log_streams={"logStreams": found})


def metric(total: float) -> Fake:
    points = [{"Sum": total}] if total else []
    return Fake(get_metric_statistics={"Datapoints": points})


def group(script: ModuleType, *, activity: bool = True) -> Any:
    counted = (script.Metric("AWS/ApiGateway", "Count", (("ApiName", "tss"),)),)
    return script.Destination(
        "API access log", script.LOG_GROUP, "/aws/apigateway/tss", counted if activity else ()
    )


def test_a_log_group_that_received_an_event_is_ok(script: ModuleType) -> None:
    finding = script.judge(group(script), clients(logs=streams(1795600000000)), START, END)
    assert finding.verdict == script.OK
    assert "last received 2026-11-2" in finding.detail


def test_an_empty_log_group_whose_source_was_active_fails(script: ModuleType) -> None:
    """The TSS API's access log, 2026-11-25: 176 requests counted and no log stream."""
    cloudwatch = metric(176)
    finding = script.judge(
        group(script), clients(logs=streams(), cloudwatch=cloudwatch), START, END
    )
    assert finding.verdict == script.EMPTY
    assert "Count: 176" in finding.detail
    (_, asked), *_ = cloudwatch.calls
    assert (asked["StartTime"], asked["EndTime"]) == (START, END)
    assert asked["Dimensions"] == [{"Name": "ApiName", "Value": "tss"}]


def test_a_stream_that_never_received_an_event_does_not_count(script: ModuleType) -> None:
    finding = script.judge(
        group(script), clients(logs=streams(None), cloudwatch=metric(3)), START, END
    )
    assert finding.verdict == script.EMPTY


def test_an_empty_log_group_whose_source_was_idle_is_not_proven(script: ModuleType) -> None:
    finding = script.judge(group(script), clients(logs=streams(), cloudwatch=metric(0)), START, END)
    assert finding.verdict == script.NOT_PROVEN


def test_an_empty_log_group_with_no_way_to_tell_fails(script: ModuleType) -> None:
    finding = script.judge(group(script, activity=False), clients(logs=streams()), START, END)
    assert finding.verdict == script.EMPTY


def test_a_destination_that_does_not_exist_fails(script: ModuleType) -> None:
    logs = Fake(describe_log_streams=not_found("ResourceNotFoundException"))
    assert script.judge(group(script), clients(logs=logs), START, END).verdict == script.MISSING
    bucket = script.Destination("ALB", script.S3, "logs/alb/")
    s3 = Fake(list_objects_v2=not_found("NoSuchBucket"))
    assert script.judge(bucket, clients(s3=s3), START, END).verdict == script.MISSING


def test_any_other_error_is_not_swallowed(script: ModuleType) -> None:
    logs = Fake(describe_log_streams=not_found("AccessDeniedException"))
    with pytest.raises(ClientError):
        script.judge(group(script), clients(logs=logs), START, END)


def test_a_bucket_prefix_is_asked_for_one_key_only(script: ModuleType) -> None:
    s3 = Fake(list_objects_v2={"KeyCount": 1})
    bucket = script.Destination("ALB", script.S3, "logs/alb/AWSLogs/1/elasticloadbalancing/")
    assert script.judge(bucket, clients(s3=s3), START, END).verdict == script.OK
    assert s3.calls == [
        (
            "list_objects_v2",
            {"Bucket": "logs", "Prefix": "alb/AWSLogs/1/elasticloadbalancing/", "MaxKeys": 1},
        )
    ]
    empty = Fake(list_objects_v2={"KeyCount": 0})
    assert script.judge(bucket, clients(s3=empty), START, END).verdict == script.EMPTY


def test_a_destination_the_script_cannot_read_fails(script: ModuleType) -> None:
    firehose = script.Destination("WAF", script.UNKNOWN, "arn:aws:firehose:::deliverystream/x")
    assert script.judge(firehose, clients(), START, END).verdict == script.MISSING


# --- The report -------------------------------------------------------------------------------


def findings(script: ModuleType, *verdicts: str) -> list[Any]:
    return [script.Finding(group(script), verdict, "detail") for verdict in verdicts]


def test_one_empty_log_fails_the_report(script: ModuleType) -> None:
    passed, lines = script.log_report(findings(script, script.OK, script.EMPTY), strict=False)
    assert not passed
    assert lines[-1] == "  FAILED: 1 of 2 log destinations."


def test_a_log_not_proven_fails_only_when_strict(script: ModuleType) -> None:
    some = findings(script, script.OK, script.NOT_PROVEN)
    passed, lines = script.log_report(some, strict=False)
    assert passed and lines[-1] == "  ok: 1 receiving, 1 not proven."
    assert not script.log_report(some, strict=True)[0]


def test_finding_no_log_destination_at_all_fails(script: ModuleType) -> None:
    passed, lines = script.log_report([], strict=False)
    assert not passed and "none found" in lines[-1]


# --- Where each kind of resource sends its logs ---------------------------------------------


def test_an_api_stage_names_its_access_log_and_its_request_count(script: ModuleType) -> None:
    stage = "arn:aws:apigateway:us-east-1::/restapis/abc123/stages/v1"
    gateway = Fake(
        get_stage={
            "accessLogSettings": {
                "destinationArn": f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/apigateway/tss"
            }
        },
        get_rest_api={"name": "tss"},
    )
    api = "arn:aws:apigateway:us-east-1::/restapis/abc123"
    (found,) = script.api_stages(clients(apigateway=gateway), [api, stage])
    assert (found.kind, found.target) == (script.LOG_GROUP, "/aws/apigateway/tss")
    assert found.activity == (
        script.Metric("AWS/ApiGateway", "Count", (("ApiName", "tss"), ("Stage", "v1"))),
    )


def test_a_stage_without_access_logging_has_no_destination(script: ModuleType) -> None:
    gateway = Fake(get_stage={}, get_rest_api={"name": "tss"})
    stage = "arn:aws:apigateway:us-east-1::/restapis/abc123/stages/v1"
    assert script.api_stages(clients(apigateway=gateway), [stage]) == []


def test_a_function_logs_to_its_configured_group_or_the_default(script: ModuleType) -> None:
    arns = [f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:{name}" for name in ("a", "b")]
    answers = {"a": {"LoggingConfig": {"LogGroup": "/custom/a"}}, "b": {}}
    functions = Fake(get_function_configuration=lambda FunctionName: answers[FunctionName])
    found = script.lambda_functions(clients(**{"lambda": functions}), arns)
    assert [d.target for d in found] == ["/custom/a", "/aws/lambda/b"]


def test_every_revision_of_a_task_family_is_one_destination(script: ModuleType) -> None:
    arns = [f"arn:aws:ecs:us-east-1:{ACCOUNT}:task-definition/worker:{n}" for n in (1, 2, 3)]
    ecs = Fake(
        describe_task_definition={
            "taskDefinition": {
                "containerDefinitions": [
                    {
                        "name": "worker",
                        "logConfiguration": {
                            "logDriver": "awslogs",
                            "options": {"awslogs-group": "/aws/ecs/worker"},
                        },
                    }
                ]
            }
        }
    )
    (found,) = script.ecs_tasks(clients(ecs=ecs), arns)
    assert (found.target, found.activity) == ("/aws/ecs/worker", ())
    assert ecs.calls == [("describe_task_definition", {"taskDefinition": "worker"})]


@pytest.mark.parametrize(
    ("level", "counted"),
    [
        ("ERROR", ["ExecutionsFailed", "ExecutionsTimedOut", "ExecutionsAborted"]),
        ("ALL", ["ExecutionsStarted"]),
    ],
)
def test_a_state_machines_log_is_due_only_for_what_its_level_logs(
    script: ModuleType, level: str, counted: list[str]
) -> None:
    arn = f"arn:aws:states:us-east-1:{ACCOUNT}:stateMachine:pipeline"
    group_arn = f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/vendedlogs/states/pipeline:*"
    machines = Fake(
        describe_state_machine={
            "name": "pipeline",
            "loggingConfiguration": {
                "level": level,
                "destinations": [{"cloudWatchLogsLogGroup": {"logGroupArn": group_arn}}],
            },
        }
    )
    (found,) = script.state_machines(clients(stepfunctions=machines), [arn])
    assert found.target == "/aws/vendedlogs/states/pipeline"
    assert [m.name for m in found.activity] == counted


def test_a_state_machine_with_logging_off_has_no_destination(script: ModuleType) -> None:
    machines = Fake(describe_state_machine={"name": "p", "loggingConfiguration": {"level": "OFF"}})
    arn = f"arn:aws:states:us-east-1:{ACCOUNT}:stateMachine:p"
    assert script.state_machines(clients(stepfunctions=machines), [arn]) == []


def test_a_rule_that_writes_to_a_log_group_is_due_when_it_matched(script: ModuleType) -> None:
    arn = f"arn:aws:events:us-east-1:{ACCOUNT}:rule/break-glass-assumed"
    events = Fake(
        list_targets_by_rule={
            "Targets": [
                {"Arn": f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/events/break-glass"},
                {"Arn": f"arn:aws:sns:us-east-1:{ACCOUNT}:alarms"},
            ]
        }
    )
    (found,) = script.event_rules(clients(events=events), [arn])
    assert found.target == "/aws/events/break-glass"
    assert found.activity == (
        script.Metric("AWS/Events", "MatchedEvents", (("RuleName", "break-glass-assumed"),)),
    )


def test_a_load_balancers_test_file_is_not_taken_for_a_log(script: ModuleType) -> None:
    """The check looks in the folder real logs go to, below the test file's."""
    arn = f"arn:aws:elasticloadbalancing:us-east-1:{ACCOUNT}:loadbalancer/app/api/0123"
    balancers = Fake(
        describe_load_balancer_attributes={
            "Attributes": [
                {"Key": "access_logs.s3.enabled", "Value": "true"},
                {"Key": "access_logs.s3.bucket", "Value": "edge-logs"},
                {"Key": "access_logs.s3.prefix", "Value": "alb"},
                {"Key": "connection_logs.s3.enabled", "Value": "false"},
            ]
        }
    )
    (found,) = script.load_balancers(clients(elbv2=balancers), [arn], ACCOUNT)
    assert found.kind == script.S3
    assert found.target == f"edge-logs/alb/AWSLogs/{ACCOUNT}/elasticloadbalancing/"
    assert found.activity[0].dimensions == (("LoadBalancer", "app/api/0123"),)


def test_a_cloudfront_delivery_is_looked_for_under_its_fixed_path(script: ModuleType) -> None:
    distribution = f"arn:aws:cloudfront::{ACCOUNT}:distribution/E123"
    logs = Fake(
        describe_delivery_sources={
            "deliverySources": [
                {
                    "name": "cdn",
                    "resourceArns": [distribution],
                    "service": "cloudfront",
                    "logType": "ACCESS_LOGS",
                },
                {"name": "other", "resourceArns": ["arn:aws:x"], "service": "x"},
            ]
        },
        describe_deliveries={
            "deliveries": [
                {
                    "deliverySourceName": "cdn",
                    "deliveryDestinationArn": f"{DELIVERY_DESTINATION}:cdn-s3",
                    "s3DeliveryConfiguration": {
                        "suffixPath": "AWSLogs/{account-id}/CloudFront/{DistributionId}/"
                    },
                },
                {
                    "deliverySourceName": "other",
                    "deliveryDestinationArn": f"{DELIVERY_DESTINATION}:o",
                },
            ]
        },
        get_delivery_destination={
            "deliveryDestination": {
                "deliveryDestinationConfiguration": {
                    "destinationResourceArn": "arn:aws:s3:::edge-logs"
                }
            }
        },
    )
    (found,) = script.log_deliveries(clients(logs=logs), [distribution], ACCOUNT)
    assert (found.kind, found.target) == (script.S3, f"edge-logs/AWSLogs/{ACCOUNT}/CloudFront/")
    assert found.activity[0].dimensions == (("DistributionId", "E123"), ("Region", "Global"))


def test_a_web_acl_without_logging_has_no_destination(script: ModuleType) -> None:
    arn = f"arn:aws:wafv2:us-east-1:{ACCOUNT}:global/webacl/edge/0123"
    none = Fake(get_logging_configuration=not_found("WAFNonexistentItemException"))
    assert script.web_acls(clients(wafv2=none), [arn]) == []
    group_arn = f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:aws-waf-logs-edge"
    some = Fake(
        get_logging_configuration={"LoggingConfiguration": {"LogDestinationConfigs": [group_arn]}}
    )
    (found,) = script.web_acls(clients(wafv2=some), [arn])
    assert (found.source, found.target) == ("WAF web ACL edge", "aws-waf-logs-edge")


def test_only_resources_with_the_stacks_tags_are_asked_for(script: ModuleType) -> None:
    class Tagging:
        def __init__(self) -> None:
            self.asked: dict[str, Any] = {}

        def get_paginator(self, name: str) -> Tagging:
            assert name == "get_resources"
            return self

        def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
            self.asked = kwargs
            return [
                {"ResourceTagMappingList": [{"ResourceARN": "b"}, {"ResourceARN": "a"}]},
                {"ResourceTagMappingList": [{"ResourceARN": "a"}]},
            ]

    tagging = Tagging()
    arns = script.tagged_resources(tagging, {"env": "dev", "app": "assay-insights"})
    assert arns == ["a", "b"]
    assert tagging.asked["TagFilters"] == [
        {"Key": "app", "Values": ["assay-insights"]},
        {"Key": "env", "Values": ["dev"]},
    ]
