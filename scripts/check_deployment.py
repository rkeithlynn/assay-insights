"""Check a deployed stack: Lambda concurrency, and that every configured log is arriving.

Usage (PowerShell, from the repository root):

    uv run scripts/check_deployment.py --before-deploy   # a new account, before `pulumi up`
    uv run scripts/check_deployment.py                   # after the isolation run
    uv run scripts/check_deployment.py --strict          # also fail on a log not proven

Read-only: it describes resources and reads metrics, and changes nothing. It reads no log
event and no object, only whether one exists.

1. Lambda concurrency (a warning). The account's concurrent-executions limit must cover a
   full batch: every file's scan result runs the scan function, which needs the TSS's
   function at the same moment. A new account allows 10.

2. Log destinations (a failure). For every resource with the stack's tags, the script reads
   where its logs are configured to go, from the resource itself, and then whether anything
   has ever arrived there:

   - `ok`: the destination has received something.
   - `EMPTY`: nothing has arrived, although the source was active or cannot be idle. Fails.
   - `MISSING`: the destination does not exist. Fails.
   - `not proven`: nothing has arrived and the source's own metrics show it was never
     active (the break-glass role's log, for one: nobody has assumed the role). Reported,
     and a failure only with `--strict`.

   Covered: API Gateway access logs, Lambda functions, ECS task definitions, Step Functions,
   VPC flow logs, WAF, Aurora's log exports, EventBridge rules that write to a log group,
   load balancer access and connection logs, and every CloudWatch Logs delivery (CloudFront).
   A new kind of log in the stack needs a function here.

Run it after the stack has been used (the isolation run starts every task and calls the
TSS): a log whose source has no activity metric, an ECS task's for one, is `EMPTY` until the
task has run once.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil

# Runs the Pulumi CLI with fixed arguments and no shell.
import subprocess  # nosec B404
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

from assay_db.batches import MAX_COMPANIONS_PER_BATCH, MAX_PDFS_PER_BATCH

INFRA_DIR = Path(__file__).resolve().parent.parent / "infra"
DEFAULT_STACK = "pilartech/assay-platform/dev"

# One scan function per uploaded file, each needing the TSS's function while it runs, for a
# full batch of PDFs and companion files; the rest is for everything else that runs at the
# same time (pipeline steps, the Cognito triggers, the API's admin calls).
OTHER_FUNCTIONS = 200
CONCURRENCY_NEEDED = 2 * (MAX_PDFS_PER_BATCH + MAX_COMPANIONS_PER_BATCH) + OTHER_FUNCTIONS
LAMBDA_SERVICE_CODE = "lambda"
CONCURRENCY_QUOTA_CODE = "L-B99A9384"
OPEN_REQUEST = frozenset({"PENDING", "CASE_OPENED"})

# Activity is counted up to this long ago, so a log still on its way is not called missing.
SETTLE = dt.timedelta(minutes=15)
DEFAULT_DAYS = 14

LOG_GROUP = "log group"
S3 = "S3"
UNKNOWN = "unknown"

OK = "ok"
EMPTY = "EMPTY"
MISSING = "MISSING"
NOT_PROVEN = "not proven"
FAILURES = frozenset({EMPTY, MISSING})

RESOURCE_TYPES = (
    "apigateway",
    "lambda:function",
    "ecs:task-definition",
    "states:stateMachine",
    "ec2:vpc-flow-log",
    "wafv2",
    "rds:cluster",
    "events:rule",
    "elasticloadbalancing:loadbalancer",
    "cloudfront:distribution",
)

Client = Callable[[str], Any]


@dataclass(frozen=True)
class Metric:
    namespace: str
    name: str
    dimensions: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Destination:
    """Where one resource's logs are configured to go."""

    source: str
    kind: str
    target: str
    # Metrics that count the source's activity. Empty: nothing says whether it was active,
    # so an empty destination is a failure.
    activity: tuple[Metric, ...] = ()


@dataclass(frozen=True)
class Finding:
    destination: Destination
    verdict: str
    detail: str


def pulumi(*args: str, stack: str) -> str:
    exe = shutil.which("pulumi")
    if exe is None:
        sys.exit("pulumi CLI not found on PATH")
    out = subprocess.run(  # noqa: S603  # nosec B603
        [exe, *args, "--stack", stack, "--cwd", INFRA_DIR.as_posix()],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


# --- Lambda concurrency -----------------------------------------------------------------------


def concurrency_report(lambda_client: Any, quotas: Any) -> tuple[bool, list[str]]:
    """Whether the account's limit covers the pipeline, and the lines that say so."""
    limit = int(lambda_client.get_account_settings()["AccountLimit"]["ConcurrentExecutions"])
    lines = [f"Lambda concurrent executions: the account allows {limit:,}."]
    if limit >= CONCURRENCY_NEEDED:
        lines.append(f"  ok: the pipeline needs {CONCURRENCY_NEEDED:,}.")
        return True, lines
    lines.append(
        f"  WARNING: the pipeline needs {CONCURRENCY_NEEDED:,} (a full batch is "
        f"{MAX_PDFS_PER_BATCH} PDFs and {MAX_COMPANIONS_PER_BATCH} companion files; each "
        "file's scan result runs the scan function and the TSS's function together). Below "
        "that, Lambda throttles calls and good documents can end on hold."
    )
    history = quotas.list_requested_service_quota_change_history_by_quota(
        ServiceCode=LAMBDA_SERVICE_CODE, QuotaCode=CONCURRENCY_QUOTA_CODE
    )
    requests = sorted(history.get("RequestedQuotas", []), key=lambda r: r["Created"], reverse=True)
    waiting = [r for r in requests if r.get("Status") in OPEN_REQUEST]
    if waiting:
        request = waiting[0]
        lines.append(
            f"  An increase to {int(request['DesiredValue']):,} was requested on "
            f"{request['Created']:%Y-%m-%d} and is {request['Status']}. Wait until it is granted."
        )
    else:
        lines.append(
            "  Request the increase, and wait until it is granted: aws service-quotas "
            f"request-service-quota-increase --service-code {LAMBDA_SERVICE_CODE} "
            f"--quota-code {CONCURRENCY_QUOTA_CODE} --desired-value {CONCURRENCY_NEEDED}"
        )
    return False, lines


# --- Which resources belong to the stack ----------------------------------------------------


def tagged_resources(tagging: Any, tags: dict[str, str]) -> list[str]:
    arns: list[str] = []
    pages = tagging.get_paginator("get_resources").paginate(
        TagFilters=[{"Key": key, "Values": [value]} for key, value in sorted(tags.items())],
        ResourceTypeFilters=list(RESOURCE_TYPES),
    )
    for page in pages:
        arns += [entry["ResourceARN"] for entry in page["ResourceTagMappingList"]]
    return sorted(set(arns))


def log_group_name(arn: str) -> str:
    """The log group an ARN names, with or without the trailing `:*`."""
    return arn.split(":log-group:", 1)[1].removesuffix(":*")


def _arn_destination(source: str, arn: str, activity: tuple[Metric, ...] = ()) -> Destination:
    if ":log-group:" in arn:
        return Destination(source, LOG_GROUP, log_group_name(arn), activity)
    if arn.startswith("arn:aws:s3:::"):
        return Destination(source, S3, arn.removeprefix("arn:aws:s3:::"), activity)
    return Destination(source, UNKNOWN, arn, activity)


# --- Where each kind of resource sends its logs ---------------------------------------------


def api_stages(client: Client, arns: Iterable[str]) -> list[Destination]:
    found = []
    gateway = client("apigateway")
    for arn in arns:
        parts = arn.split("/")
        if ":apigateway:" not in arn or len(parts) != 5 or parts[3] != "stages":
            continue
        api_id, stage_name = parts[2], parts[4]
        stage = gateway.get_stage(restApiId=api_id, stageName=stage_name)
        target = (stage.get("accessLogSettings") or {}).get("destinationArn")
        if not target:
            continue
        api_name = gateway.get_rest_api(restApiId=api_id)["name"]
        requests = Metric("AWS/ApiGateway", "Count", (("ApiName", api_name), ("Stage", stage_name)))
        source = f"API Gateway {api_name}/{stage_name} access log"
        found.append(_arn_destination(source, target, (requests,)))
    return found


def lambda_functions(client: Client, arns: Iterable[str]) -> list[Destination]:
    found = []
    for arn in arns:
        if ":lambda:" not in arn or ":function:" not in arn:
            continue
        name = arn.split(":function:", 1)[1]
        config = client("lambda").get_function_configuration(FunctionName=name)
        group = (config.get("LoggingConfig") or {}).get("LogGroup") or f"/aws/lambda/{name}"
        invocations = Metric("AWS/Lambda", "Invocations", (("FunctionName", name),))
        found.append(Destination(f"Lambda function {name}", LOG_GROUP, group, (invocations,)))
    return found


def ecs_tasks(client: Client, arns: Iterable[str]) -> list[Destination]:
    families = sorted(
        {arn.split(":task-definition/", 1)[1].rsplit(":", 1)[0] for arn in arns if ":ecs:" in arn}
    )
    found = []
    for family in families:
        try:
            # The family's latest active revision.
            task = client("ecs").describe_task_definition(taskDefinition=family)["taskDefinition"]
        except ClientError:
            continue  # every revision is deregistered
        for container in task["containerDefinitions"]:
            log = container.get("logConfiguration") or {}
            group = (log.get("options") or {}).get("awslogs-group")
            if log.get("logDriver") == "awslogs" and group:
                source = f"ECS task {family}, container {container['name']}"
                found.append(Destination(source, LOG_GROUP, group))
    return found


def state_machines(client: Client, arns: Iterable[str]) -> list[Destination]:
    found = []
    for arn in arns:
        if ":states:" not in arn or ":stateMachine:" not in arn:
            continue
        machine = client("stepfunctions").describe_state_machine(stateMachineArn=arn)
        logging = machine.get("loggingConfiguration") or {}
        level = logging.get("level", "OFF")
        if level == "OFF":
            continue
        dimensions = (("StateMachineArn", arn),)
        names: tuple[str, ...]
        if level == "ALL":
            names = ("ExecutionsStarted",)
        else:
            # At ERROR or FATAL only failures are logged: a failed execution must have left one.
            names = ("ExecutionsFailed", "ExecutionsTimedOut", "ExecutionsAborted")
        activity = tuple(Metric("AWS/States", name, dimensions) for name in names)
        for entry in logging.get("destinations", []):
            group_arn = (entry.get("cloudWatchLogsLogGroup") or {}).get("logGroupArn")
            if group_arn:
                source = f"State machine {machine['name']} ({level})"
                found.append(_arn_destination(source, group_arn, activity))
    return found


def flow_logs(client: Client, arns: Iterable[str]) -> list[Destination]:
    ids = [arn.rsplit("/", 1)[1] for arn in arns if ":vpc-flow-log/" in arn]
    if not ids:
        return []
    found = []
    for flow in client("ec2").describe_flow_logs(FlowLogIds=ids)["FlowLogs"]:
        source = f"VPC flow log {flow['FlowLogId']} ({flow.get('ResourceId', '')})"
        if flow.get("LogGroupName"):
            found.append(Destination(source, LOG_GROUP, flow["LogGroupName"]))
        elif flow.get("LogDestination"):
            found.append(_arn_destination(source, flow["LogDestination"]))
    return found


def web_acls(client: Client, arns: Iterable[str]) -> list[Destination]:
    found = []
    for arn in arns:
        if ":wafv2:" not in arn or "/webacl/" not in arn:
            continue
        try:
            config = client("wafv2").get_logging_configuration(ResourceArn=arn)
        except ClientError as error:
            if error.response["Error"]["Code"] == "WAFNonexistentItemException":
                continue  # no logging configured
            raise
        name = arn.split("/webacl/", 1)[1].split("/", 1)[0]
        for target in config["LoggingConfiguration"]["LogDestinationConfigs"]:
            found.append(_arn_destination(f"WAF web ACL {name}", target))
    return found


def database_clusters(client: Client, arns: Iterable[str]) -> list[Destination]:
    wanted = {arn for arn in arns if ":rds:" in arn}
    if not wanted:
        return []
    found = []
    for page in client("rds").get_paginator("describe_db_clusters").paginate():
        for cluster in page["DBClusters"]:
            if cluster["DBClusterArn"] not in wanted:
                continue
            identifier = cluster["DBClusterIdentifier"]
            for export in cluster.get("EnabledCloudwatchLogsExports", []):
                source = f"Aurora cluster {identifier}, {export} log"
                group = f"/aws/rds/cluster/{identifier}/{export}"
                found.append(Destination(source, LOG_GROUP, group))
    return found


def event_rules(client: Client, arns: Iterable[str]) -> list[Destination]:
    found = []
    for arn in arns:
        if ":events:" not in arn or ":rule/" not in arn:
            continue
        name = arn.split(":rule/", 1)[1]
        if "/" in name:
            continue  # a rule on another event bus
        matched = Metric("AWS/Events", "MatchedEvents", (("RuleName", name),))
        for target in client("events").list_targets_by_rule(Rule=name)["Targets"]:
            if ":log-group:" in target["Arn"]:
                source = f"EventBridge rule {name}"
                found.append(_arn_destination(source, target["Arn"], (matched,)))
    return found


def load_balancers(client: Client, arns: Iterable[str], account_id: str) -> list[Destination]:
    found = []
    for arn in arns:
        if ":loadbalancer/" not in arn:
            continue
        label = arn.split(":loadbalancer/", 1)[1]
        attributes = {
            entry["Key"]: entry["Value"]
            for entry in client("elbv2").describe_load_balancer_attributes(LoadBalancerArn=arn)[
                "Attributes"
            ]
        }
        requests = Metric("AWS/ApplicationELB", "RequestCount", (("LoadBalancer", label),))
        # The folder is where real logs go. The load balancer's test file, written when
        # logging is switched on, sits one level above it and proves nothing. Access and
        # connection logs with the same bucket and prefix share the folder, and this check
        # cannot tell them apart.
        for kind in ("access_logs", "connection_logs"):
            if attributes.get(f"{kind}.s3.enabled") != "true":
                continue
            prefix = attributes.get(f"{kind}.s3.prefix", "").strip("/")
            parts = (prefix, "AWSLogs", account_id, "elasticloadbalancing")
            path = "/".join(part for part in parts if part)
            target = f"{attributes[f'{kind}.s3.bucket']}/{path}/"
            source = f"Load balancer {label.split('/')[1]}, {kind.replace('_', ' ')}"
            found.append(Destination(source, S3, target, (requests,)))
    return found


def log_deliveries(client: Client, arns: Iterable[str], account_id: str) -> list[Destination]:
    """CloudWatch Logs deliveries (CloudFront's access logs are one) from tagged resources."""
    wanted = set(arns)
    logs = client("logs")
    sources = {
        source["name"]: source
        for source in logs.describe_delivery_sources().get("deliverySources", [])
        if wanted & set(source.get("resourceArns", []))
    }
    found = []
    for delivery in logs.describe_deliveries().get("deliveries", []):
        source = sources.get(delivery["deliverySourceName"])
        if source is None:
            continue
        name = delivery["deliveryDestinationArn"].rsplit(":", 1)[1]
        destination = logs.get_delivery_destination(name=name)["deliveryDestination"]
        target = destination["deliveryDestinationConfiguration"]["destinationResourceArn"]
        resource = source["resourceArns"][0]
        activity: tuple[Metric, ...] = ()
        if source.get("service") == "cloudfront":
            distribution = resource.rsplit("/", 1)[1]
            activity = (
                Metric(
                    "AWS/CloudFront",
                    "Requests",
                    (("DistributionId", distribution), ("Region", "Global")),
                ),
            )
        label = f"{source.get('service', 'log')} {resource.rsplit('/', 1)[-1]}, "
        label += f"{source.get('logType', 'logs').lower().replace('_', ' ')}"
        found_one = _arn_destination(label, target, activity)
        if found_one.kind == S3:
            suffix = (delivery.get("s3DeliveryConfiguration") or {}).get("suffixPath", "")
            # The fixed start of the path; anything after another variable differs per object.
            fixed = suffix.replace("{account-id}", account_id).split("{", 1)[0]
            bucket_and_prefix = found_one.target.rstrip("/") + "/" + fixed.lstrip("/")
            found_one = Destination(label, S3, bucket_and_prefix, activity)
        found.append(found_one)
    return found


def discover(client: Client, arns: list[str], account_id: str) -> list[Destination]:
    found = [
        *api_stages(client, arns),
        *lambda_functions(client, arns),
        *ecs_tasks(client, arns),
        *state_machines(client, arns),
        *flow_logs(client, arns),
        *web_acls(client, arns),
        *database_clusters(client, arns),
        *event_rules(client, arns),
        *load_balancers(client, arns, account_id),
        *log_deliveries(client, arns, account_id),
    ]
    return sorted(found, key=lambda d: d.source)


# --- Has anything arrived? ------------------------------------------------------------------


def log_group_last_event(logs: Any, group: str) -> dt.datetime | None:
    """When the log group last received an event; None if it never has. Raises if missing."""
    streams = logs.describe_log_streams(
        logGroupName=group, orderBy="LastEventTime", descending=True, limit=1
    )["logStreams"]
    for stream in streams:
        stamp = stream.get("lastIngestionTime") or stream.get("lastEventTimestamp")
        if stamp:
            return dt.datetime.fromtimestamp(stamp / 1000, dt.UTC)
    return None


def s3_has_object(s3: Any, target: str) -> bool:
    bucket, _, prefix = target.partition("/")
    return bool(s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1).get("KeyCount"))


def activity_total(
    cloudwatch: Any, metrics: tuple[Metric, ...], start: dt.datetime, end: dt.datetime
) -> float:
    total = 0.0
    for metric in metrics:
        points = cloudwatch.get_metric_statistics(
            Namespace=metric.namespace,
            MetricName=metric.name,
            Dimensions=[{"Name": name, "Value": value} for name, value in metric.dimensions],
            StartTime=start,
            EndTime=end,
            Period=86400,
            Statistics=["Sum"],
        )["Datapoints"]
        total += sum(point["Sum"] for point in points)
    return total


def judge(
    destination: Destination, client: Client, start: dt.datetime, end: dt.datetime
) -> Finding:
    if destination.kind == UNKNOWN:
        return Finding(destination, MISSING, "a kind of destination this script cannot read")
    try:
        if destination.kind == LOG_GROUP:
            last = log_group_last_event(client("logs"), destination.target)
            if last is not None:
                return Finding(destination, OK, f"last received {last:%Y-%m-%d %H:%M} UTC")
        elif s3_has_object(client("s3"), destination.target):
            return Finding(destination, OK, "has objects")
    except ClientError as error:
        code = error.response["Error"]["Code"]
        if code in {"ResourceNotFoundException", "NoSuchBucket"}:
            return Finding(destination, MISSING, "the destination does not exist")
        raise
    if not destination.activity:
        return Finding(destination, EMPTY, "nothing has ever arrived")
    count = activity_total(client("cloudwatch"), destination.activity, start, end)
    names = ", ".join(sorted({metric.name for metric in destination.activity}))
    if count > 0:
        detail = f"nothing has ever arrived, and the source was active ({names}: {count:,.0f})"
        return Finding(destination, EMPTY, detail)
    return Finding(
        destination, NOT_PROVEN, f"nothing has arrived; the source was idle ({names}: 0)"
    )


def log_report(findings: list[Finding], *, strict: bool) -> tuple[bool, list[str]]:
    lines = [f"Log destinations: {len(findings)} configured."]
    if not findings:
        lines.append("  FAILED: none found. Check the stack's tags, or use --before-deploy.")
        return False, lines
    width = max(len(finding.destination.source) for finding in findings)
    for finding in findings:
        target = f"{finding.destination.kind} {finding.destination.target}"
        lines.append(
            f"  {finding.verdict:<10} {finding.destination.source:<{width}}  {target}: "
            f"{finding.detail}"
        )
    failed = [f for f in findings if f.verdict in FAILURES or (strict and f.verdict == NOT_PROVEN)]
    unproven = sum(finding.verdict == NOT_PROVEN for finding in findings)
    if failed:
        lines.append(f"  FAILED: {len(failed)} of {len(findings)} log destinations.")
    else:
        lines.append(f"  ok: {len(findings) - unproven} receiving, {unproven} not proven.")
    return not failed, lines


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--stack", default=DEFAULT_STACK)
    parser.add_argument(
        "--before-deploy",
        action="store_true",
        help="only the Lambda concurrency check (nothing is deployed yet)",
    )
    parser.add_argument(
        "--strict", action="store_true", help="fail on a log that is empty with an idle source"
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS, help="how far back to count a source's activity"
    )
    args = parser.parse_args()

    session = boto3.Session(
        profile_name=pulumi("config", "get", "aws:profile", stack=args.stack),
        region_name=pulumi("config", "get", "aws:region", stack=args.stack),
    )
    clients: dict[str, Any] = {}

    def client(name: str) -> Any:
        if name not in clients:
            # The name is one of this script's own; boto3's stubs want a literal.
            clients[name] = session.client(name)  # type: ignore[call-overload]
        return clients[name]

    enough, lines = concurrency_report(client("lambda"), client("service-quotas"))
    print("\n".join(lines), flush=True)
    if args.before_deploy:
        return 0

    tags = json.loads(pulumi("config", "get", "aws:defaultTags", stack=args.stack))["tags"]
    account_id = client("sts").get_caller_identity()["Account"]
    arns = tagged_resources(client("resourcegroupstaggingapi"), tags)
    end = dt.datetime.now(dt.UTC) - SETTLE
    start = end - dt.timedelta(days=args.days)
    findings = [judge(d, client, start, end) for d in discover(client, arns, account_id)]
    passed, lines = log_report(findings, strict=args.strict)
    print("\n" + "\n".join(lines))
    if not enough:
        print("\nThe Lambda concurrency warning above still stands.")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
