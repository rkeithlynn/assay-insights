"""The API: FastAPI on ECS Fargate behind an internal Application Load Balancer.

BUILD_SPEC.md section 4, item 6. One task (0.5 vCPU, 1 GB, X86_64) in the workload subnet,
running as a non-root user with a read-only root filesystem. The load balancer spans the two
app subnets (it needs two zones).

The load balancer's security group has no inbound rule yet: nothing can reach the API until
the edge (build step 5) adds CloudFront's VPC origin, which must then use HTTPS to the load
balancer. Until then the listener is plain HTTP and unreachable.

The task signs in to Aurora with IAM database authentication as `app_tenant` (tenant
routes) and `platform_reader` (admin routes), and has no other AWS permission.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pulumi
import pulumi_aws as aws

from assay_infra.auth import Auth
from assay_infra.database import Database, db_user_arn
from assay_infra.images import WORKSPACE_MANIFESTS, ContainerImage
from assay_infra.network import Network

APP_TENANT_DB_USER = "app_tenant"  # assay_db.roles.APP_TENANT
PLATFORM_READER_DB_USER = "platform_reader"  # assay_db.roles.PLATFORM_READER
API_DB_USERS = (APP_TENANT_DB_USER, PLATFORM_READER_DB_USER)

DOCKERFILE = "api/Dockerfile"
IMAGE_TREES = ("db/assay_db", "api/assay_api")

CONTAINER_NAME = "api"
CONTAINER_PORT = 8000  # assay_api.__main__.PORT
CONTAINER_USER = "10001:10001"  # api/Dockerfile
TASK_CPU = "512"
TASK_MEMORY = "1024"
HEALTH_PATH = "/api/health"
LOG_RETENTION_DAYS = 30


def _json(document: object) -> str:
    return json.dumps(document, separators=(",", ":"))


def _ecs_tasks_trust(account_id: str, region: str) -> str:
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                    "Condition": {
                        "StringEquals": {"aws:SourceAccount": account_id},
                        "ArnLike": {"aws:SourceArn": f"arn:aws:ecs:{region}:{account_id}:*"},
                    },
                }
            ],
        }
    )


def execution_role_policy(*, repository: str, log_group: str) -> str:
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    # GetAuthorizationToken does not support resource scoping.
                    "Sid": "EcrAuth",
                    "Effect": "Allow",
                    "Action": "ecr:GetAuthorizationToken",
                    "Resource": "*",
                },
                {
                    "Sid": "EcrPull",
                    "Effect": "Allow",
                    "Action": [
                        "ecr:BatchCheckLayerAvailability",
                        "ecr:BatchGetImage",
                        "ecr:GetDownloadUrlForLayer",
                    ],
                    "Resource": repository,
                },
                {
                    "Sid": "Logs",
                    "Effect": "Allow",
                    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                    "Resource": f"{log_group}:*",
                },
            ],
        }
    )


def task_role_policy(db_users: list[str]) -> str:
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "ConnectAsApiRoles",
                    "Effect": "Allow",
                    "Action": "rds-db:connect",
                    "Resource": sorted(db_users),
                }
            ],
        }
    )


def container_definitions(
    *, image: str, log_group: str, region: str, environment: Mapping[str, str]
) -> str:
    return json.dumps(
        [
            {
                "name": CONTAINER_NAME,
                "image": image,
                "essential": True,
                "user": CONTAINER_USER,
                "readonlyRootFilesystem": True,
                "privileged": False,
                "portMappings": [{"containerPort": CONTAINER_PORT, "protocol": "tcp"}],
                "environment": [{"name": k, "value": v} for k, v in sorted(environment.items())],
                "linuxParameters": {"capabilities": {"drop": ["ALL"]}},
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": log_group,
                        "awslogs-region": region,
                        "awslogs-stream-prefix": CONTAINER_NAME,
                        "mode": "non-blocking",
                    },
                },
            }
        ],
        sort_keys=True,
    )


class ApiService(pulumi.ComponentResource):
    def __init__(
        self,
        name: str,
        *,
        repo_root: Path,
        region: str,
        account_id: pulumi.Input[str],
        network: Network,
        database: Database,
        database_name: str,
        auth: Auth,
        aws_profile: str | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:compute:ApiService", name, None, opts)
        child = pulumi.ResourceOptions(parent=self)
        account = pulumi.Output.from_input(account_id)

        self.image = ContainerImage(
            name,
            repo_root=repo_root,
            dockerfile=DOCKERFILE,
            files=WORKSPACE_MANIFESTS,
            trees=IMAGE_TREES,
            region=region,
            account_id=account_id,
            aws_profile=aws_profile,
            opts=child,
        )

        self.log_group = aws.cloudwatch.LogGroup(
            f"{name}-logs",
            name=f"/aws/ecs/{name}",
            retention_in_days=LOG_RETENTION_DAYS,
            opts=child,
        )

        # --- IAM ------------------------------------------------------------------------
        trust = account.apply(lambda acct: _ecs_tasks_trust(acct, region))
        self.execution_role = aws.iam.Role(
            f"{name}-execution-role",
            description="API task execution: pull the image, write logs",
            assume_role_policy=trust,
            opts=child,
        )
        aws.iam.RolePolicy(
            f"{name}-execution-policy",
            role=self.execution_role.id,
            policy=pulumi.Output.all(
                repository=self.image.repository.arn, log_group=self.log_group.arn
            ).apply(lambda a: execution_role_policy(**a)),
            opts=child,
        )
        self.task_role = aws.iam.Role(
            f"{name}-task-role",
            description="API task: IAM database sign-in as app_tenant and platform_reader",
            assume_role_policy=trust,
            opts=child,
        )
        self.task_role_policy = aws.iam.RolePolicy(
            f"{name}-task-policy",
            role=self.task_role.id,
            policy=pulumi.Output.all(
                *[
                    db_user_arn(region, account_id, database.cluster_resource_id, user)
                    for user in API_DB_USERS
                ]
            ).apply(task_role_policy),
            opts=child,
        )

        # --- Security groups --------------------------------------------------------------
        self.alb_security_group = aws.ec2.SecurityGroup(
            f"{name}-alb-sg",
            vpc_id=network.vpc.id,
            description="API load balancer: no inbound until the CloudFront VPC origin exists",
            tags={"Name": f"{name}-alb"},
            opts=child,
        )
        self.task_security_group = aws.ec2.SecurityGroup(
            f"{name}-task-sg",
            vpc_id=network.vpc.id,
            description="API task: its port from the API load balancer only",
            tags={"Name": f"{name}-task"},
            opts=child,
        )
        self.security_group_rules: list[pulumi.CustomResource] = [
            aws.vpc.SecurityGroupEgressRule(
                f"{name}-alb-out-task",
                security_group_id=self.alb_security_group.id,
                description="To the API task",
                ip_protocol="tcp",
                from_port=CONTAINER_PORT,
                to_port=CONTAINER_PORT,
                referenced_security_group_id=self.task_security_group.id,
                opts=child,
            ),
            aws.vpc.SecurityGroupIngressRule(
                f"{name}-task-in-alb",
                security_group_id=self.task_security_group.id,
                description="From the API load balancer",
                ip_protocol="tcp",
                from_port=CONTAINER_PORT,
                to_port=CONTAINER_PORT,
                referenced_security_group_id=self.alb_security_group.id,
                opts=child,
            ),
        ]

        # --- Load balancer ----------------------------------------------------------------
        self.load_balancer = aws.lb.LoadBalancer(
            f"{name}-alb",
            name=name,
            internal=True,
            load_balancer_type="application",
            subnets=[s.id for s in network.app_subnets],
            security_groups=[self.alb_security_group.id],
            drop_invalid_header_fields=True,
            enable_deletion_protection=True,
            idle_timeout=120,
            opts=child,
        )
        self.target_group = aws.lb.TargetGroup(
            f"{name}-tg",
            name=name,
            target_type="ip",
            protocol="HTTP",
            port=CONTAINER_PORT,
            vpc_id=network.vpc.id,
            deregistration_delay=30,
            health_check=aws.lb.TargetGroupHealthCheckArgs(
                path=HEALTH_PATH,
                protocol="HTTP",
                matcher="200",
                interval=15,
                timeout=5,
                healthy_threshold=2,
                unhealthy_threshold=3,
            ),
            opts=child,
        )
        self.listener = aws.lb.Listener(
            f"{name}-http",
            load_balancer_arn=self.load_balancer.arn,
            port=80,
            protocol="HTTP",
            default_actions=[
                aws.lb.ListenerDefaultActionArgs(
                    type="forward", target_group_arn=self.target_group.arn
                )
            ],
            opts=child,
        )

        # --- Task and service -------------------------------------------------------------
        self.cluster = aws.ecs.Cluster(
            f"{name}-cluster",
            name=name,
            settings=[aws.ecs.ClusterSettingArgs(name="containerInsights", value="enabled")],
            opts=child,
        )
        environment = pulumi.Output.all(
            DB_HOST=database.endpoint,
            DB_PORT=database.port.apply(str),
            COGNITO_TENANT_POOL_ID=auth.tenant.pool.id,
            COGNITO_TENANT_CLIENT_ID=auth.tenant.client.id,
            COGNITO_TENANT_JWKS=auth.tenant_jwks,
            COGNITO_PLATFORM_POOL_ID=auth.platform.pool.id,
            COGNITO_PLATFORM_CLIENT_ID=auth.platform.client.id,
            COGNITO_PLATFORM_JWKS=auth.platform_jwks,
        ).apply(lambda env: {**env, "AWS_REGION": region, "DB_NAME": database_name})
        self.task_definition = aws.ecs.TaskDefinition(
            f"{name}-task",
            family=name,
            cpu=TASK_CPU,
            memory=TASK_MEMORY,
            network_mode="awsvpc",
            requires_compatibilities=["FARGATE"],
            runtime_platform=aws.ecs.TaskDefinitionRuntimePlatformArgs(
                cpu_architecture="X86_64", operating_system_family="LINUX"
            ),
            execution_role_arn=self.execution_role.arn,
            task_role_arn=self.task_role.arn,
            container_definitions=pulumi.Output.all(
                image=self.image.image_uri, log_group=self.log_group.name, environment=environment
            ).apply(lambda a: container_definitions(region=region, **a)),
            opts=child,
        )
        self.service = aws.ecs.Service(
            f"{name}-service",
            name=name,
            cluster=self.cluster.arn,
            task_definition=self.task_definition.arn,
            desired_count=1,
            launch_type="FARGATE",
            platform_version="LATEST",
            network_configuration=aws.ecs.ServiceNetworkConfigurationArgs(
                subnets=[network.workload_subnet.id],
                security_groups=[network.workload_security_group.id, self.task_security_group.id],
                assign_public_ip=False,
            ),
            load_balancers=[
                aws.ecs.ServiceLoadBalancerArgs(
                    target_group_arn=self.target_group.arn,
                    container_name=CONTAINER_NAME,
                    container_port=CONTAINER_PORT,
                )
            ],
            health_check_grace_period_seconds=60,
            deployment_minimum_healthy_percent=100,
            deployment_maximum_percent=200,
            deployment_circuit_breaker=aws.ecs.ServiceDeploymentCircuitBreakerArgs(
                enable=True, rollback=True
            ),
            enable_execute_command=False,
            propagate_tags="SERVICE",
            wait_for_steady_state=False,
            opts=pulumi.ResourceOptions(
                parent=self, depends_on=[self.listener, self.task_role_policy]
            ),
        )

        self.register_outputs(
            {
                "load_balancer_dns_name": self.load_balancer.dns_name,
                "cluster_name": self.cluster.name,
                "service_name": self.service.name,
            }
        )
