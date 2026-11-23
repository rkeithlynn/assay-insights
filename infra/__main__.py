"""assay-platform Pulumi program.

Build order follows BUILD_SPEC.md section 4. Implemented so far: 1 (DNS and certificate),
2 (network), 3 (database, with the migration Lambda), 4 (Cognito), 5 (the Tenant Storage
Service, with tenant provisioning) and 6 (the API).
"""

from pathlib import Path

import pulumi
import pulumi_aws as aws

from assay_infra.alerts import Alerts
from assay_infra.api import ApiService
from assay_infra.auth import Auth
from assay_infra.config import load_config
from assay_infra.database import Database
from assay_infra.dns import DnsAndCertificate
from assay_infra.isolation import SERVICE_NAME as ISOLATION_SERVICE
from assay_infra.isolation import IsolationRunner, runner_role
from assay_infra.migrations import MigrationRunner
from assay_infra.network import Network
from assay_infra.provisioning import Provisioning
from assay_infra.tss import TenantStorage

REPO_ROOT = Path(__file__).resolve().parent.parent

config = load_config()
account_id = aws.get_caller_identity_output().account_id
aws_profile = pulumi.Config("aws").get("profile")

edge_dns = DnsAndCertificate("edge", zone_name=config.dns.hosted_zone, hosts=config.dns.hosts)

network = Network(
    config.name_prefix, cfg=config.network, region=config.region, account_id=account_id
)

database = Database(
    f"{config.name_prefix}-db", cfg=config.database, network=network, account_id=account_id
)

migrations = MigrationRunner(
    f"{config.name_prefix}-db-migrate",
    repo_root=REPO_ROOT,
    region=config.region,
    account_id=account_id,
    network=network,
    database=database,
    database_name=config.database.database_name,
    aws_profile=aws_profile,
)

auth = Auth(
    f"{config.name_prefix}-auth",
    cfg=config.auth,
    dns=config.dns,
    region=config.region,
    account_id=account_id,
    repo_root=REPO_ROOT,
    aws_profile=aws_profile,
)

api = ApiService(
    f"{config.name_prefix}-api",
    repo_root=REPO_ROOT,
    region=config.region,
    account_id=account_id,
    network=network,
    database=database,
    database_name=config.database.database_name,
    auth=auth,
    aws_profile=aws_profile,
)

alerts = Alerts(
    f"{config.name_prefix}-alarms",
    email=config.alerts.email,
    region=config.region,
    account_id=account_id,
)

# The isolation test runner (development stacks only) is a TSS caller, so its role exists
# before the TSS does.
isolation_name = f"{config.name_prefix}-isolation-tests"
tss_callers: dict[str, aws.iam.Role] = {}
if config.tenancy.isolation_tests:
    tss_callers[ISOLATION_SERVICE] = runner_role(isolation_name, account_id)

storage = TenantStorage(
    f"{config.name_prefix}-tss",
    repo_root=REPO_ROOT,
    region=config.region,
    account_id=account_id,
    name_prefix=config.name_prefix,
    network=network,
    database=database,
    database_name=config.database.database_name,
    break_glass_principal_arn=config.tenancy.break_glass_principal_arn,
    alarm_topic_arn=alerts.topic.arn,
    callers=tss_callers,
    aws_profile=aws_profile,
)

provisioning = Provisioning(
    f"{config.name_prefix}-provision",
    repo_root=REPO_ROOT,
    region=config.region,
    account_id=account_id,
    name_prefix=config.name_prefix,
    network=network,
    database=database,
    database_name=config.database.database_name,
    auth=auth,
    storage=storage,
    vector_dimensions=config.tenancy.vector_dimensions,
    default_tags=config.tenancy.default_tags,
    # Every KMS key this program creates outside the provisioning component. Add a new
    # one here: the provisioning role is denied tagging the keys in this list.
    platform_key_arns=[database.key.arn, alerts.key.arn],
    alarm_topic_arn=alerts.topic.arn,
    aws_profile=aws_profile,
)

if config.tenancy.isolation_tests:
    isolation = IsolationRunner(
        isolation_name,
        role=tss_callers[ISOLATION_SERVICE],
        repo_root=REPO_ROOT,
        region=config.region,
        account_id=account_id,
        name_prefix=config.name_prefix,
        network=network,
        database=database,
        database_name=config.database.database_name,
        tss_endpoint_url=storage.endpoint_url,
        tenant_role_arn=storage.tenant_role.arn,
        invite_function_arn=provisioning.invite_function.arn,
        aws_profile=aws_profile,
    )
    pulumi.export("isolation_function_name", isolation.function.name)

pulumi.export("hosted_zone_id", edge_dns.zone.zone_id)
pulumi.export("name_servers", edge_dns.zone.name_servers)
pulumi.export("certificate_arn", edge_dns.certificate.arn)
pulumi.export("vpc_id", network.vpc.id)
pulumi.export("workload_subnet_id", network.workload_subnet.id)
pulumi.export("db_cluster_endpoint", database.endpoint)
pulumi.export("db_cluster_resource_id", database.cluster_resource_id)
pulumi.export("db_master_secret_arn", database.master_secret_arn)
pulumi.export("migrate_function_name", migrations.function.name)
pulumi.export("tenant_user_pool_id", auth.tenant.pool.id)
pulumi.export("tenant_app_client_id", auth.tenant.client.id)
pulumi.export("tenant_login_domain", auth.tenant.domain.domain)
pulumi.export("platform_user_pool_id", auth.platform.pool.id)
pulumi.export("platform_app_client_id", auth.platform.client.id)
pulumi.export("platform_login_domain", auth.platform.domain.domain)
pulumi.export("pre_token_function_name", auth.pre_token.function.name)
pulumi.export("api_load_balancer_dns_name", api.load_balancer.dns_name)
pulumi.export("api_target_group_arn", api.target_group.arn)
pulumi.export("api_cluster_name", api.cluster.name)
pulumi.export("api_service_name", api.service.name)
pulumi.export("alarm_topic_arn", alerts.topic.arn)
pulumi.export("tss_endpoint_url", storage.endpoint_url)
pulumi.export("tss_function_name", storage.function.name)
pulumi.export("tenant_role_arn", storage.tenant_role.arn)
pulumi.export("break_glass_role_arn", storage.break_glass.role.arn)
pulumi.export("provisioning_state_machine_arn", provisioning.state_machine.arn)
pulumi.export("provisioning_role_arn", provisioning.task_role.arn)
pulumi.export("tenant_state_bucket", provisioning.state_bucket.bucket)
pulumi.export("cognito_admin_function_name", provisioning.invite_function.name)
# Read by scripts/run_isolation.py, which runs the provisioning task's probe command.
pulumi.export("provisioning_cluster_name", provisioning.cluster.name)
pulumi.export("provisioning_task_definition_arn", provisioning.task_definition.arn)
pulumi.export("provisioning_log_group", provisioning.log_group.name)
pulumi.export("workload_security_group_id", network.workload_security_group.id)
