"""VPC with private subnets only, VPC endpoints for every AWS API the code calls, flow logs.

BUILD_SPEC.md section 4, item 2. Workloads have no route to the internet: there is no NAT
gateway, and the internet gateway (attached only so CloudFront VPC origins can be used
later) has no route pointing at it.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Sequence
from dataclasses import dataclass

import pulumi
import pulumi_aws as aws

from assay_infra.config import NetworkConfig

# Interface endpoints, one per AWS API called from inside the VPC (section 4, item 2).
# Add a service here only when code inside the VPC calls it.
INTERFACE_ENDPOINT_SERVICES: tuple[str, ...] = (
    "bedrock-runtime",
    "textract",
    "kms",
    "sts",
    "s3vectors",
    "rekognition",
    "ecr.api",
    "ecr.dkr",
    "logs",
    "secretsmanager",
    "sqs",
)

FLOW_LOG_RETENTION_DAYS = 30


@dataclass(frozen=True)
class SubnetPlan:
    app: tuple[str, str]
    data: tuple[str, str]


def plan_subnets(vpc_cidr: str) -> SubnetPlan:
    """App subnets are the first two /22s; data subnets are /24s after the app range.

    For 10.40.0.0/16: app 10.40.0.0/22 and 10.40.4.0/22, data 10.40.16.0/24 and 10.40.17.0/24.
    """
    net = ipaddress.ip_network(vpc_cidr)
    app = list(net.subnets(new_prefix=22))[:2]
    data_slices = list(net.subnets(new_prefix=24))[16:18]
    return SubnetPlan(
        app=(str(app[0]), str(app[1])),
        data=(str(data_slices[0]), str(data_slices[1])),
    )


def _endpoint_slug(service: str) -> str:
    return service.replace(".", "-")


class Network(pulumi.ComponentResource):
    """One VPC, private app and data subnets in two zones, endpoints in the workload zone."""

    def __init__(
        self,
        name: str,
        *,
        cfg: NetworkConfig,
        region: str,
        account_id: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:network:Network", name, None, opts)
        child = pulumi.ResourceOptions(parent=self)
        plan = plan_subnets(cfg.vpc_cidr)
        zone_ids = (cfg.workload_az_id, cfg.second_az_id)

        self.vpc = aws.ec2.Vpc(
            f"{name}-vpc",
            cidr_block=cfg.vpc_cidr,
            enable_dns_support=True,
            enable_dns_hostnames=True,
            tags={"Name": name},
            opts=child,
        )

        # Attached for CloudFront VPC origins (section 4, item 2). No route table points at it.
        self.internet_gateway = aws.ec2.InternetGateway(
            f"{name}-igw",
            vpc_id=self.vpc.id,
            tags={"Name": f"{name}-igw"},
            opts=child,
        )

        # Take over the default security group and leave it with no rules at all.
        self.default_security_group = aws.ec2.DefaultSecurityGroup(
            f"{name}-default-sg",
            vpc_id=self.vpc.id,
            ingress=[],
            egress=[],
            tags={"Name": f"{name}-default-do-not-use"},
            opts=child,
        )

        self.app_subnets = self._subnets(name, "app", plan.app, zone_ids, child)
        self.data_subnets = self._subnets(name, "data", plan.data, zone_ids, child)
        # Workloads (Lambda, Fargate) and the interface endpoints run in the workload zone.
        self.workload_subnet = self.app_subnets[0]

        self.app_route_table = self._route_table(name, "app", self.app_subnets, child)
        self.data_route_table = self._route_table(name, "data", self.data_subnets, child)

        self.s3_gateway_endpoint = aws.ec2.VpcEndpoint(
            f"{name}-s3",
            vpc_id=self.vpc.id,
            service_name=f"com.amazonaws.{region}.s3",
            vpc_endpoint_type="Gateway",
            route_table_ids=[self.app_route_table.id],
            policy=pulumi.Output.from_input(account_id).apply(
                lambda acct: s3_gateway_policy(acct, region)
            ),
            tags={"Name": f"{name}-s3"},
            opts=child,
        )

        # Security groups. Rules are separate resources so the groups can reference each other.
        self.endpoint_security_group = aws.ec2.SecurityGroup(
            f"{name}-endpoints-sg",
            vpc_id=self.vpc.id,
            description="Interface VPC endpoints: HTTPS from the app subnets",
            tags={"Name": f"{name}-endpoints"},
            opts=child,
        )
        self.workload_security_group = aws.ec2.SecurityGroup(
            f"{name}-workload-sg",
            vpc_id=self.vpc.id,
            description="Workloads: HTTPS to VPC endpoints and S3, PostgreSQL to the database",
            tags={"Name": f"{name}-workload"},
            opts=child,
        )
        self.database_security_group = aws.ec2.SecurityGroup(
            f"{name}-db-sg",
            vpc_id=self.vpc.id,
            description="Aurora PostgreSQL: 5432 from workloads only",
            tags={"Name": f"{name}-db"},
            opts=child,
        )

        rules: list[pulumi.CustomResource] = []
        for i, cidr in enumerate(plan.app):
            rules.append(
                aws.vpc.SecurityGroupIngressRule(
                    f"{name}-endpoints-in-app-{i}",
                    security_group_id=self.endpoint_security_group.id,
                    description="HTTPS from app subnet",
                    ip_protocol="tcp",
                    from_port=443,
                    to_port=443,
                    cidr_ipv4=cidr,
                    opts=child,
                )
            )
        rules.append(
            aws.vpc.SecurityGroupEgressRule(
                f"{name}-workload-out-endpoints",
                security_group_id=self.workload_security_group.id,
                description="HTTPS to interface endpoints",
                ip_protocol="tcp",
                from_port=443,
                to_port=443,
                referenced_security_group_id=self.endpoint_security_group.id,
                opts=child,
            )
        )
        rules.append(
            aws.vpc.SecurityGroupEgressRule(
                f"{name}-workload-out-s3",
                security_group_id=self.workload_security_group.id,
                description="HTTPS to S3 through the gateway endpoint",
                ip_protocol="tcp",
                from_port=443,
                to_port=443,
                prefix_list_id=self.s3_gateway_endpoint.prefix_list_id,
                opts=child,
            )
        )
        rules.append(
            aws.vpc.SecurityGroupEgressRule(
                f"{name}-workload-out-db",
                security_group_id=self.workload_security_group.id,
                description="PostgreSQL to Aurora",
                ip_protocol="tcp",
                from_port=5432,
                to_port=5432,
                referenced_security_group_id=self.database_security_group.id,
                opts=child,
            )
        )
        rules.append(
            aws.vpc.SecurityGroupIngressRule(
                f"{name}-db-in-workload",
                security_group_id=self.database_security_group.id,
                description="PostgreSQL from workloads",
                ip_protocol="tcp",
                from_port=5432,
                to_port=5432,
                referenced_security_group_id=self.workload_security_group.id,
                opts=child,
            )
        )

        self.security_group_rules = rules

        self.interface_endpoints: dict[str, aws.ec2.VpcEndpoint] = {}
        for service in INTERFACE_ENDPOINT_SERVICES:
            slug = _endpoint_slug(service)
            self.interface_endpoints[service] = aws.ec2.VpcEndpoint(
                f"{name}-{slug}",
                vpc_id=self.vpc.id,
                service_name=f"com.amazonaws.{region}.{service}",
                vpc_endpoint_type="Interface",
                subnet_ids=[self.workload_subnet.id],
                security_group_ids=[self.endpoint_security_group.id],
                private_dns_enabled=True,
                tags={"Name": f"{name}-{slug}"},
                opts=child,
            )

        self._flow_logs(name, region, account_id, child)

        self.register_outputs(
            {
                "vpc_id": self.vpc.id,
                "workload_subnet_id": self.workload_subnet.id,
                "data_subnet_ids": [s.id for s in self.data_subnets],
            }
        )

    def _subnets(
        self,
        name: str,
        tier: str,
        cidrs: Sequence[str],
        zone_ids: Sequence[str],
        opts: pulumi.ResourceOptions,
    ) -> list[aws.ec2.Subnet]:
        return [
            aws.ec2.Subnet(
                f"{name}-{tier}-{zone_id}",
                vpc_id=self.vpc.id,
                cidr_block=cidr,
                availability_zone_id=zone_id,
                map_public_ip_on_launch=False,
                tags={"Name": f"{name}-{tier}-{zone_id}", "tier": tier},
                opts=opts,
            )
            for cidr, zone_id in zip(cidrs, zone_ids, strict=True)
        ]

    def _route_table(
        self,
        name: str,
        tier: str,
        subnets: Sequence[aws.ec2.Subnet],
        opts: pulumi.ResourceOptions,
    ) -> aws.ec2.RouteTable:
        # Only the implicit local route. No route to the internet gateway, no NAT.
        table = aws.ec2.RouteTable(
            f"{name}-{tier}-rt",
            vpc_id=self.vpc.id,
            tags={"Name": f"{name}-{tier}"},
            opts=opts,
        )
        for i, subnet in enumerate(subnets):
            aws.ec2.RouteTableAssociation(
                f"{name}-{tier}-rt-{i}",
                route_table_id=table.id,
                subnet_id=subnet.id,
                opts=opts,
            )
        return table

    def _flow_logs(
        self,
        name: str,
        region: str,
        account_id: pulumi.Input[str],
        opts: pulumi.ResourceOptions,
    ) -> None:
        # Kept in this account's CloudWatch for now; move to the Log Archive account before
        # production (see CLAUDE.md).
        self.flow_log_group = aws.cloudwatch.LogGroup(
            f"{name}-flow-logs",
            name=f"/aws/vpc/{name}/flow-logs",
            retention_in_days=FLOW_LOG_RETENTION_DAYS,
            opts=opts,
        )
        role = aws.iam.Role(
            f"{name}-flow-logs-role",
            description="VPC flow logs delivery to CloudWatch Logs",
            assume_role_policy=pulumi.Output.from_input(account_id).apply(
                lambda acct: json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "vpc-flow-logs.amazonaws.com"},
                                "Action": "sts:AssumeRole",
                                "Condition": {
                                    "StringEquals": {"aws:SourceAccount": acct},
                                    "ArnLike": {
                                        "aws:SourceArn": (
                                            f"arn:aws:ec2:{region}:{acct}:vpc-flow-log/*"
                                        )
                                    },
                                },
                            }
                        ],
                    }
                )
            ),
            opts=opts,
        )
        aws.iam.RolePolicy(
            f"{name}-flow-logs-policy",
            role=role.id,
            policy=self.flow_log_group.arn.apply(
                lambda arn: json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": [
                                    "logs:CreateLogStream",
                                    "logs:PutLogEvents",
                                    "logs:DescribeLogStreams",
                                ],
                                "Resource": [arn, f"{arn}:*"],
                            }
                        ],
                    }
                )
            ),
            opts=opts,
        )
        self.flow_log = aws.ec2.FlowLog(
            f"{name}-flow-log",
            vpc_id=self.vpc.id,
            traffic_type="ALL",
            log_destination_type="cloud-watch-logs",
            log_destination=self.flow_log_group.arn,
            iam_role_arn=role.arn,
            max_aggregation_interval=600,
            tags={"Name": f"{name}-flow-log"},
            opts=opts,
        )


def s3_gateway_policy(account_id: str, region: str) -> str:
    """Allow S3 only for buckets in this account, plus the bucket ECR serves image layers from."""
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "OwnAccountBuckets",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:*",
                    "Resource": "*",
                    "Condition": {"StringEquals": {"aws:ResourceAccount": account_id}},
                },
                {
                    "Sid": "EcrImageLayers",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": f"arn:aws:s3:::prod-{region}-starport-layer-bucket/*",
                },
            ],
        }
    )
