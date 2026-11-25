"""The Tenant Storage Service and the roles around it (BUILD_SPEC.md section 6).

- **The tenant role.** One role for every tenant. Its policy is the one template built from
  `${aws:PrincipalTag/...}` (`assay_tss.policies.tenant_role_policy`), and its trust policy
  names only the TSS Lambda's role. Nothing else can assume it.
- **The TSS.** A Lambda in the VPC behind a private REST API with IAM authorization,
  reached through the `execute-api` interface endpoint. It reads `tenant_storage` as
  database role `tss` and assumes the tenant role for 15 minutes at a time.
- **The break-glass role.** For an emergency read of tenant data or to repair a bucket or
  key policy. Assumable by one configured principal; every AssumeRole of it matches an
  EventBridge rule, is written to a log group and raises an alarm that notifies the
  alarm topic. Production needs a
  separate, approval-gated role in its place.

The policy documents live in `assay_tss.policies`, next to the session, bucket and key
policies they have to agree with.
"""

from __future__ import annotations

import functools
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pulumi
import pulumi_aws as aws

from assay_infra.database import Database, db_user_arn
from assay_infra.images import WORKSPACE_MANIFESTS, ContainerImage
from assay_infra.network import Network
from assay_infra.propagation import iam_propagation
from assay_tss import policies
from assay_tss.naming import tenant_pattern

TSS_DB_USER = "tss"  # assay_db.roles.TSS


@dataclass(frozen=True)
class TssCaller:
    """A role that may call `resolve`, and the accesses it may ask for (build step 6).

    The TSS refuses any other access (assay_tss.service.AccessNotAllowed): the API, for
    example, never gets pipeline credentials, so it can never read inbox/ or quarantine/.
    """

    role: aws.iam.Role
    access: Sequence[str]

    @property
    def is_worker(self) -> bool:
        """The pipeline's worker task: `/resolve-worker` only, against an attempt grant."""
        return WORKER_ACCESS in self.access

    @property
    def path(self) -> str:
        return WORKER_PATH if self.is_worker else RESOLVE_PATH


# assay_tss.models.StorageAccess.WORKER: never combined with another access.
WORKER_ACCESS = "worker"


def check_callers(callers: Mapping[str, TssCaller]) -> None:
    """A worker caller has worker access and nothing else: it must never be able to name a
    tenant on `/resolve`, and no other caller may use `/resolve-worker`."""
    for service, caller in callers.items():
        if caller.is_worker and set(caller.access) != {WORKER_ACCESS}:
            raise ValueError(f"TSS caller {service!r}: worker access cannot be combined")


def caller_grants(names: Mapping[str, str], callers: Mapping[str, TssCaller]) -> str:
    """`TSS_CALLERS` (assay_tss.service.parse_callers): role name -> service and accesses.

    `names` maps each service to its role's name (an Output resolved by the caller).
    """
    return _json(
        {
            names[service]: {"service": service, "access": sorted(set(caller.access))}
            for service, caller in sorted(callers.items())
        }
    )


DOCKERFILE = "tss/Dockerfile"
IMAGE_TREES = ("db/assay_db", "tss/assay_tss")

STAGE = "v1"
RESOLVE_PATH = "resolve"
# The worker task's own route (build step 6, second half): by document, attempt and token.
WORKER_PATH = "resolve-worker"
# API Gateway waits at most 29 seconds for an integration. A paused database takes about
# 15 seconds to resume, so the function gets the whole window.
FUNCTION_TIMEOUT_SECONDS = 29
LOG_RETENTION_DAYS = 30
# AWS's managed policy for API Gateway's account-level CloudWatch Logs role.
API_GATEWAY_LOGS_POLICY = (
    "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
)
ACCESS_LOG_FORMAT = {
    "requestId": "$context.requestId",
    "requestTime": "$context.requestTime",
    "caller": "$context.identity.userArn",
    "vpceId": "$context.identity.vpceId",
    "method": "$context.httpMethod",
    "path": "$context.resourcePath",
    "status": "$context.status",
    "integrationStatus": "$context.integration.status",
    "latencyMs": "$context.responseLatency",
    "error": "$context.error.message",
}

# The break-glass role reads objects and repairs policies. It never writes or deletes data.
BREAK_GLASS_READ_ACTIONS = (
    "s3:GetObject",
    "s3:GetObjectVersion",
    "s3:GetObjectTagging",
    "s3:ListBucket",
    "s3:ListBucketVersions",
    "s3:GetBucketLocation",
)
BREAK_GLASS_REPAIR_ACTIONS = ("s3:GetBucketPolicy", "s3:PutBucketPolicy")
BREAK_GLASS_SESSION_SECONDS = 3600


def _json(document: object) -> str:
    return json.dumps(document, separators=(",", ":"))


def _service_trust(service: str, account_id: str) -> str:
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": service},
                    "Action": "sts:AssumeRole",
                    "Condition": {"StringEquals": {"aws:SourceAccount": account_id}},
                }
            ],
        }
    )


def api_logs_trust_policy(account_id: str) -> str:
    """The trust policy of the role API Gateway writes its logs with (the account setting).

    A deliberate, narrow widening (owner, 2026-11-25; an experiment, see CLAUDE.md): the
    source account is checked only if the call carries one. With `StringEquals` the access
    log never received an entry, and the suspicion is that the part of API Gateway that
    writes logs sends no source account. A call that carries another account's is still
    refused. Every other service role keeps `_service_trust`.
    """
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "apigateway.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                    "Condition": {"StringEqualsIfExists": {"aws:SourceAccount": account_id}},
                }
            ],
        }
    )


# Lambda VPC networking. These actions do not support resource scoping.
VPC_NETWORK_INTERFACE_STATEMENT = {
    "Sid": "VpcNetworkInterfaces",
    "Effect": "Allow",
    "Action": [
        "ec2:CreateNetworkInterface",
        "ec2:DescribeNetworkInterfaces",
        "ec2:DescribeSubnets",
        "ec2:DeleteNetworkInterface",
        "ec2:AssignPrivateIpAddresses",
        "ec2:UnassignPrivateIpAddresses",
    ],
    "Resource": "*",
}


def tss_role_policy(*, log_group: str, db_user: str, tenant_role: str) -> str:
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
                VPC_NETWORK_INTERFACE_STATEMENT,
                {
                    "Sid": "ConnectAsTss",
                    "Effect": "Allow",
                    "Action": "rds-db:connect",
                    "Resource": db_user,
                },
                {
                    "Sid": "AssumeTheTenantRole",
                    "Effect": "Allow",
                    "Action": ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"],
                    "Resource": tenant_role,
                },
            ],
        }
    )


def break_glass_trust_policy(principal_arn: str) -> str:
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "OneConfiguredPrincipal",
                    "Effect": "Allow",
                    "Principal": {"AWS": principal_arn},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    )


def break_glass_role_policy(name_prefix: str) -> str:
    """S3 only. What it may do with a tenant key is in that key's policy."""
    bucket = f"arn:aws:s3:::{tenant_pattern(name_prefix)}"
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "ReadTenantObjects",
                    "Effect": "Allow",
                    "Action": list(BREAK_GLASS_READ_ACTIONS),
                    "Resource": [bucket, f"{bucket}/*"],
                },
                {
                    "Sid": "RepairBucketPolicy",
                    "Effect": "Allow",
                    "Action": list(BREAK_GLASS_REPAIR_ACTIONS),
                    "Resource": bucket,
                },
            ],
        }
    )


def break_glass_event_pattern(role_arn: str) -> str:
    """Every AssumeRole that names the break-glass role, whether it succeeds or not."""
    return _json(
        {
            "source": ["aws.sts"],
            "detail-type": ["AWS API Call via CloudTrail"],
            "detail": {
                "eventSource": ["sts.amazonaws.com"],
                "eventName": ["AssumeRole"],
                "requestParameters": {"roleArn": [role_arn]},
            },
        }
    )


def events_to_logs_policy(log_group_arn: str) -> str:
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "EventBridgeWritesBreakGlassEvents",
                    "Effect": "Allow",
                    "Principal": {
                        "Service": ["events.amazonaws.com", "delivery.logs.amazonaws.com"]
                    },
                    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                    "Resource": f"{log_group_arn}:*",
                }
            ],
        }
    )


# API Gateway's shorthand for "this API", which it replaces with the API's ARN on save.
THIS_API = "execute-api:"


def _principals(callers: Sequence[str], *, as_stored: bool) -> str | list[str]:
    names = sorted(callers)
    # API Gateway stores a one-item principal list as a string.
    return names[0] if as_stored and len(names) == 1 else names


def api_resource_policy(
    *,
    vpc_endpoint_id: str,
    callers: list[str],
    worker_callers: Sequence[str] = (),
    execution_arn: str | None = None,
) -> str:
    """Only through the VPC's endpoint, and only the caller roles named here, each on its own
    route: `callers` on `/resolve`, `worker_callers` (the pipeline's worker task) on
    `/resolve-worker` and nowhere else.

    With `execution_arn`, the policy is written exactly as API Gateway stores it: every
    resource is a full ARN under the API's own, and a single principal is a string, not a
    one-item list. That is the form sent to AWS, so the next preview finds no difference.

    Without it, the result is the portable form, which names no API. It is never sent to
    AWS; it is what the redeploy fingerprint is taken from.
    """
    api = execution_arn if execution_arn is not None else THIS_API
    as_stored = execution_arn is not None
    statements: list[dict[str, object]] = [
        {
            "Sid": "OnlyThroughTheVpcEndpoint",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "execute-api:Invoke",
            "Resource": f"{api}/*",
            "Condition": {"StringNotEquals": {"aws:SourceVpce": vpc_endpoint_id}},
        }
    ]
    if callers:
        statements.append(
            {
                "Sid": "KnownCallers",
                "Effect": "Allow",
                "Principal": {"AWS": _principals(callers, as_stored=as_stored)},
                "Action": "execute-api:Invoke",
                "Resource": f"{api}/{STAGE}/POST/{RESOLVE_PATH}",
            }
        )
    if worker_callers:
        statements.append(
            {
                "Sid": "WorkerTask",
                "Effect": "Allow",
                "Principal": {"AWS": _principals(worker_callers, as_stored=as_stored)},
                "Action": "execute-api:Invoke",
                "Resource": f"{api}/{STAGE}/POST/{WORKER_PATH}",
            }
        )
    return _json({"Version": "2012-10-17", "Statement": statements})


def _caller_policy(path: str, execution_arn: str) -> str:
    return invoke_policy(execution_arn, path)


def invoke_policy(execution_arn: str, path: str = RESOLVE_PATH) -> str:
    """A caller's own permission: its one route of the private API."""
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "CallTheTenantStorageService",
                    "Effect": "Allow",
                    "Action": "execute-api:Invoke",
                    "Resource": f"{execution_arn}/{STAGE}/POST/{path}",
                }
            ],
        }
    )


class BreakGlass(pulumi.ComponentResource):
    """The break-glass role, and the rule, log and alarm that fire whenever it is assumed."""

    def __init__(
        self,
        name: str,
        *,
        name_prefix: str,
        principal_arn: str,
        alarm_topic_arn: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:tenancy:BreakGlass", name, None, opts)
        child = pulumi.ResourceOptions(parent=self)

        self.role = aws.iam.Role(
            f"{name}-role",
            name=name,
            description="Break glass: emergency read of tenant data and policy repair. Alarmed.",
            assume_role_policy=break_glass_trust_policy(principal_arn),
            max_session_duration=BREAK_GLASS_SESSION_SECONDS,
            opts=child,
        )
        aws.iam.RolePolicy(
            f"{name}-policy",
            role=self.role.id,
            policy=break_glass_role_policy(name_prefix),
            opts=child,
        )

        # CloudTrail records AssumeRole as a read-only management event. A rule sees those
        # only in this state.
        self.rule = aws.cloudwatch.EventRule(
            f"{name}-assumed",
            name=f"{name}-assumed",
            description="AssumeRole of the break-glass role",
            event_pattern=self.role.arn.apply(break_glass_event_pattern),
            state="ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS",
            opts=child,
        )
        self.log_group = aws.cloudwatch.LogGroup(
            f"{name}-events",
            name=f"/aws/events/{name}",
            retention_in_days=400,
            opts=child,
        )
        log_policy = aws.cloudwatch.LogResourcePolicy(
            f"{name}-events-policy",
            policy_name=f"{name}-events",
            policy_document=self.log_group.arn.apply(events_to_logs_policy),
            opts=child,
        )
        aws.cloudwatch.EventTarget(
            f"{name}-assumed-log",
            rule=self.rule.name,
            target_id="log",
            arn=self.log_group.arn,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[log_policy]),
        )
        self.alarm = aws.cloudwatch.MetricAlarm(
            f"{name}-assumed-alarm",
            name=f"{name}-assumed",
            alarm_description="The break-glass role was assumed (or an attempt was made).",
            namespace="AWS/Events",
            metric_name="MatchedEvents",
            dimensions={"RuleName": self.rule.name},
            alarm_actions=[alarm_topic_arn],
            statistic="Sum",
            period=60,
            evaluation_periods=1,
            threshold=1,
            comparison_operator="GreaterThanOrEqualToThreshold",
            treat_missing_data="notBreaching",
            opts=child,
        )
        self.register_outputs({"role_arn": self.role.arn})


class TenantStorage(pulumi.ComponentResource):
    """The tenant role, the TSS Lambda and its private API."""

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
        break_glass_principal_arn: str,
        # The SNS topic alarms notify (assay_infra.alerts).
        alarm_topic_arn: pulumi.Input[str],
        # Service name -> the IAM role allowed to call `resolve` under that name, and the
        # accesses it may ask for.
        callers: Mapping[str, TssCaller],
        aws_profile: str | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:tenancy:TenantStorage", name, None, opts)
        child = pulumi.ResourceOptions(parent=self)
        account = pulumi.Output.from_input(account_id)

        self.break_glass = BreakGlass(
            f"{name_prefix}-break-glass",
            name_prefix=name_prefix,
            principal_arn=break_glass_principal_arn,
            alarm_topic_arn=alarm_topic_arn,
            opts=child,
        )

        # --- Roles -----------------------------------------------------------------------
        self.log_group = aws.cloudwatch.LogGroup(
            f"{name}-logs",
            name=f"/aws/lambda/{name}",
            retention_in_days=LOG_RETENTION_DAYS,
            opts=child,
        )
        self.role = aws.iam.Role(
            f"{name}-role",
            description="Tenant Storage Service: reads tenant_storage, assumes the tenant role",
            assume_role_policy=account.apply(
                lambda acct: _service_trust("lambda.amazonaws.com", acct)
            ),
            opts=child,
        )
        # The policies below name roles created in this same program as principals: the
        # tenant role's trust policy (the TSS's role) and the API's resource policy (every
        # caller's role). Each waits until IAM has had time to make those roles known.
        self.iam_ready = iam_propagation(
            f"{name}-iam-propagation",
            roles=[self.role.arn, *[caller.role.arn for caller in callers.values()]],
            repo_root=repo_root,
            opts=child,
        )
        self.tenant_role = aws.iam.Role(
            f"{name_prefix}-tenant-role",
            name=f"{name_prefix}-tenant",
            description="Tenant data. Assumable only by the Tenant Storage Service, per tenant.",
            assume_role_policy=self.role.arn.apply(
                lambda arn: _json(
                    policies.tenant_role_trust_policy(tss_role_arn=arn, name_prefix=name_prefix)
                )
            ),
            # The shortest maximum IAM allows. The TSS asks for 900 seconds.
            max_session_duration=3600,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self.iam_ready]),
        )
        aws.iam.RolePolicy(
            f"{name_prefix}-tenant-policy",
            role=self.tenant_role.id,
            policy=account.apply(
                lambda acct: _json(policies.tenant_role_policy(region=region, account_id=acct))
            ),
            opts=child,
        )
        role_policy = aws.iam.RolePolicy(
            f"{name}-role-policy",
            role=self.role.id,
            policy=pulumi.Output.all(
                log_group=self.log_group.arn,
                db_user=db_user_arn(region, account_id, database.cluster_resource_id, TSS_DB_USER),
                tenant_role=self.tenant_role.arn,
            ).apply(lambda a: tss_role_policy(**a)),
            opts=child,
        )

        # --- Function --------------------------------------------------------------------
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
        # IAM role name -> service name and accesses, as assay_tss.service reads it.
        caller_names: pulumi.Input[str] = "{}"
        if callers:
            caller_names = pulumi.Output.all(
                **{service: caller.role.name for service, caller in callers.items()}
            ).apply(lambda names: caller_grants(names, callers))
        self.function = aws.lambda_.Function(
            f"{name}-fn",
            name=name,
            description="Tenant Storage Service: 15-minute storage contexts, one tenant each",
            role=self.role.arn,
            package_type="Image",
            image_uri=self.image.image_uri,
            architectures=["x86_64"],
            memory_size=512,
            timeout=FUNCTION_TIMEOUT_SECONDS,
            vpc_config=aws.lambda_.FunctionVpcConfigArgs(
                subnet_ids=[network.workload_subnet.id],
                security_group_ids=[network.workload_security_group.id],
            ),
            environment=aws.lambda_.FunctionEnvironmentArgs(
                variables={
                    "DB_HOST": database.endpoint,
                    "DB_PORT": database.port.apply(str),
                    "DB_NAME": database_name,
                    "NAME_PREFIX": name_prefix,
                    "TENANT_ROLE_ARN": self.tenant_role.arn,
                    "TSS_CALLERS": caller_names,
                    # The VPC's STS endpoint serves only the regional name.
                    "AWS_STS_REGIONAL_ENDPOINTS": "regional",
                }
            ),
            logging_config=aws.lambda_.FunctionLoggingConfigArgs(
                log_format="JSON",
                log_group=self.log_group.name,
                application_log_level="INFO",
                system_log_level="WARN",
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[role_policy]),
        )

        # --- Private API -----------------------------------------------------------------
        endpoint = network.interface_endpoints["execute-api"]
        check_callers(callers)
        caller_arns = [caller.role.arn for caller in callers.values() if not caller.is_worker]
        worker_arns = [caller.role.arn for caller in callers.values() if caller.is_worker]
        resolvers = len(caller_arns)
        self.api = aws.apigateway.RestApi(
            f"{name}-api",
            name=name,
            description="Tenant Storage Service (private, IAM authorization)",
            endpoint_configuration=aws.apigateway.RestApiEndpointConfigurationArgs(
                types="PRIVATE", vpc_endpoint_ids=[endpoint.id]
            ),
            opts=child,
        )
        # The policy is its own resource, not the API's `policy` argument, because API
        # Gateway stores it with the API's own ARN in every resource. Only a separate
        # resource can name that ARN; the shorthand form shows a difference on every preview.
        self.api_policy = aws.apigateway.RestApiPolicy(
            f"{name}-api-policy",
            rest_api_id=self.api.id,
            policy=pulumi.Output.all(
                self.api.execution_arn, endpoint.id, *caller_arns, *worker_arns
            ).apply(
                lambda v: api_resource_policy(
                    execution_arn=v[0],
                    vpc_endpoint_id=v[1],
                    callers=list(v[2 : 2 + resolvers]),
                    worker_callers=list(v[2 + resolvers :]),
                )
            ),
            # API Gateway refuses a policy that names a role IAM has not yet made known to
            # it, and the provider does not retry (the failed run of 2026-11-25).
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self.iam_ready]),
        )
        resource = aws.apigateway.Resource(
            f"{name}-api-resolve",
            rest_api=self.api.id,
            parent_id=self.api.root_resource_id,
            path_part=RESOLVE_PATH,
            opts=child,
        )
        method = aws.apigateway.Method(
            f"{name}-api-resolve-post",
            rest_api=self.api.id,
            resource_id=resource.id,
            http_method="POST",
            authorization="AWS_IAM",
            opts=child,
        )
        integration = aws.apigateway.Integration(
            f"{name}-api-resolve-lambda",
            rest_api=self.api.id,
            resource_id=resource.id,
            http_method=method.http_method,
            type="AWS_PROXY",
            integration_http_method="POST",
            uri=self.function.invoke_arn,
            timeout_milliseconds=FUNCTION_TIMEOUT_SECONDS * 1000,
            opts=child,
        )
        aws.lambda_.Permission(
            f"{name}-api-invoke",
            action="lambda:InvokeFunction",
            function=self.function.name,
            principal="apigateway.amazonaws.com",
            source_arn=pulumi.Output.concat(self.api.execution_arn, f"/*/POST/{RESOLVE_PATH}"),
            opts=child,
        )
        # The worker task's route (build step 6, second half): the same function, which
        # tells the two apart by the request's resource.
        worker_resource = aws.apigateway.Resource(
            f"{name}-api-resolve-worker",
            rest_api=self.api.id,
            parent_id=self.api.root_resource_id,
            path_part=WORKER_PATH,
            opts=child,
        )
        worker_method = aws.apigateway.Method(
            f"{name}-api-resolve-worker-post",
            rest_api=self.api.id,
            resource_id=worker_resource.id,
            http_method="POST",
            authorization="AWS_IAM",
            opts=child,
        )
        worker_integration = aws.apigateway.Integration(
            f"{name}-api-resolve-worker-lambda",
            rest_api=self.api.id,
            resource_id=worker_resource.id,
            http_method=worker_method.http_method,
            type="AWS_PROXY",
            integration_http_method="POST",
            uri=self.function.invoke_arn,
            timeout_milliseconds=FUNCTION_TIMEOUT_SECONDS * 1000,
            opts=child,
        )
        aws.lambda_.Permission(
            f"{name}-api-invoke-worker",
            action="lambda:InvokeFunction",
            function=self.function.name,
            principal="apigateway.amazonaws.com",
            source_arn=pulumi.Output.concat(self.api.execution_arn, f"/*/POST/{WORKER_PATH}"),
            opts=child,
        )

        # Account-wide (one per Region): the role API Gateway writes its logs with. It was
        # unset in this account before build step 4.
        logs_role = aws.iam.Role(
            f"{name}-api-logs-role",
            description="API Gateway: write access logs to CloudWatch Logs (account setting)",
            assume_role_policy=account.apply(api_logs_trust_policy),
            opts=child,
        )
        logs_attachment = aws.iam.RolePolicyAttachment(
            f"{name}-api-logs-policy",
            role=logs_role.name,
            policy_arn=API_GATEWAY_LOGS_POLICY,
            opts=child,
        )
        api_account = aws.apigateway.Account(
            f"{name}-api-account",
            cloudwatch_role_arn=logs_role.arn,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[logs_attachment]),
        )
        self.access_log_group = aws.cloudwatch.LogGroup(
            f"{name}-api-logs",
            name=f"/aws/apigateway/{name}",
            retention_in_days=LOG_RETENTION_DAYS,
            opts=child,
        )
        # A new deployment whenever the route, its integration or the resource policy changes.
        # The policy enters the fingerprint in its portable form (endpoint and callers).
        portable_policy = pulumi.Output.all(endpoint.id, *caller_arns, *worker_arns).apply(
            lambda v: api_resource_policy(
                vpc_endpoint_id=v[0],
                callers=list(v[1 : 1 + resolvers]),
                worker_callers=list(v[1 + resolvers :]),
            )
        )
        redeploy = pulumi.Output.all(
            resource.id,
            method.id,
            integration.id,
            integration.uri,
            portable_policy,
            worker_resource.id,
            worker_method.id,
            worker_integration.id,
        ).apply(lambda parts: hashlib.sha256(_json(parts).encode()).hexdigest())
        # A private API cannot be deployed before it has its policy.
        deployment = aws.apigateway.Deployment(
            f"{name}-api-deployment",
            rest_api=self.api.id,
            triggers={"redeploy": redeploy},
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[
                    method,
                    integration,
                    worker_method,
                    worker_integration,
                    self.api_policy,
                ],
            ),
        )
        self.stage = aws.apigateway.Stage(
            f"{name}-api-stage",
            rest_api=self.api.id,
            deployment=deployment.id,
            stage_name=STAGE,
            access_log_settings=aws.apigateway.StageAccessLogSettingsArgs(
                destination_arn=self.access_log_group.arn,
                format=_json(ACCESS_LOG_FORMAT),
            ),
            xray_tracing_enabled=False,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[api_account]),
        )
        # Resolved privately by the execute-api endpoint's DNS.
        self.endpoint_url = pulumi.Output.concat(
            "https://", self.api.id, f".execute-api.{region}.amazonaws.com/", self.stage.stage_name
        )

        for service, caller in callers.items():
            aws.iam.RolePolicy(
                f"{name}-caller-{service}",
                role=caller.role.id,
                # Its own route only: `/resolve`, or for the worker task `/resolve-worker`.
                policy=self.api.execution_arn.apply(functools.partial(_caller_policy, caller.path)),
                opts=child,
            )

        self.register_outputs(
            {
                "endpoint_url": self.endpoint_url,
                "tenant_role_arn": self.tenant_role.arn,
                "break_glass_role_arn": self.break_glass.role.arn,
            }
        )
