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

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import pulumi
import pulumi_aws as aws

from assay_infra.database import Database, db_user_arn
from assay_infra.images import WORKSPACE_MANIFESTS, ContainerImage
from assay_infra.network import Network
from assay_tss import policies
from assay_tss.naming import tenant_pattern

TSS_DB_USER = "tss"  # assay_db.roles.TSS
DOCKERFILE = "tss/Dockerfile"
IMAGE_TREES = ("db/assay_db", "tss/assay_tss")

STAGE = "v1"
RESOLVE_PATH = "resolve"
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


def api_resource_policy(*, vpc_endpoint_id: str, callers: list[str]) -> str:
    """Only through the VPC's endpoint, and only the caller roles named here."""
    statements: list[dict[str, object]] = [
        {
            "Sid": "OnlyThroughTheVpcEndpoint",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "execute-api:Invoke",
            "Resource": "execute-api:/*",
            "Condition": {"StringNotEquals": {"aws:SourceVpce": vpc_endpoint_id}},
        }
    ]
    if callers:
        statements.append(
            {
                "Sid": "KnownCallers",
                "Effect": "Allow",
                "Principal": {"AWS": sorted(callers)},
                "Action": "execute-api:Invoke",
                "Resource": f"execute-api:/{STAGE}/POST/{RESOLVE_PATH}",
            }
        )
    return _json({"Version": "2012-10-17", "Statement": statements})


def invoke_policy(execution_arn: str) -> str:
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "CallTheTenantStorageService",
                    "Effect": "Allow",
                    "Action": "execute-api:Invoke",
                    "Resource": f"{execution_arn}/{STAGE}/POST/{RESOLVE_PATH}",
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
        # Service name -> the IAM role allowed to call `resolve` under that name.
        callers: Mapping[str, aws.iam.Role],
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
            opts=child,
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
        # IAM role name -> service name, as assay_tss.service.ServiceSettings reads it.
        caller_names: pulumi.Input[str] = "{}"
        if callers:
            caller_names = pulumi.Output.all(
                **{service: role.name for service, role in callers.items()}
            ).apply(lambda names: _json({n: service for service, n in sorted(names.items())}))
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
        resource_policy = pulumi.Output.all(
            endpoint.id, *[role.arn for role in callers.values()]
        ).apply(lambda v: api_resource_policy(vpc_endpoint_id=v[0], callers=list(v[1:])))
        self.api = aws.apigateway.RestApi(
            f"{name}-api",
            name=name,
            description="Tenant Storage Service (private, IAM authorization)",
            endpoint_configuration=aws.apigateway.RestApiEndpointConfigurationArgs(
                types="PRIVATE", vpc_endpoint_ids=[endpoint.id]
            ),
            policy=resource_policy,
            opts=child,
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

        # Account-wide (one per Region): the role API Gateway writes its logs with. It was
        # unset in this account before build step 4.
        logs_role = aws.iam.Role(
            f"{name}-api-logs-role",
            description="API Gateway: write access logs to CloudWatch Logs (account setting)",
            assume_role_policy=account.apply(
                lambda acct: _service_trust("apigateway.amazonaws.com", acct)
            ),
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
        redeploy = pulumi.Output.all(
            resource.id, method.id, integration.id, integration.uri, resource_policy
        ).apply(lambda parts: hashlib.sha256(_json(parts).encode()).hexdigest())
        deployment = aws.apigateway.Deployment(
            f"{name}-api-deployment",
            rest_api=self.api.id,
            triggers={"redeploy": redeploy},
            opts=pulumi.ResourceOptions(parent=self, depends_on=[method, integration]),
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

        for service, role in callers.items():
            aws.iam.RolePolicy(
                f"{name}-caller-{service}",
                role=role.id,
                policy=self.api.execution_arn.apply(invoke_policy),
                opts=child,
            )

        self.register_outputs(
            {
                "endpoint_url": self.endpoint_url,
                "tenant_role_arn": self.tenant_role.arn,
                "break_glass_role_arn": self.break_glass.role.arn,
            }
        )
