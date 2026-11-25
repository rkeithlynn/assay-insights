"""assay-platform Pulumi program.

Build order follows BUILD_SPEC.md section 4. Implemented so far: 1 (DNS and certificate),
2 (network), 3 (database, with the migration Lambda), 4 (Cognito, with the audit triggers),
5 (the Tenant Storage Service, with tenant provisioning), 6 (the API), 7 (the edge), 8 (the
pipeline, build step 6 first half: scan routing, the ingest queue, fingerprint) and the
alarms of 9.
"""

from pathlib import Path

import pulumi
import pulumi_aws as aws

from assay_infra.alerts import Alerts, SecurityNotices
from assay_infra.api import ApiService, api_task_role
from assay_infra.auth import Auth
from assay_infra.auth_events import AuthEvents
from assay_infra.cognito_admin import CognitoAdminApi
from assay_infra.config import load_config
from assay_infra.database import Database
from assay_infra.dns import DnsAndCertificate
from assay_infra.edge import Edge, EdgeLogs
from assay_infra.images import WORKSPACE_MANIFESTS, ContainerImage
from assay_infra.isolation import SERVICE_NAME as ISOLATION_SERVICE
from assay_infra.isolation import IsolationRunner, runner_role
from assay_infra.migrations import MigrationRunner
from assay_infra.network import Network, states_endpoint_policy
from assay_infra.pipeline import (
    SCAN_SERVICE,
    STEPS_SERVICE,
    SWEEPER_SERVICE,
    WORKER_SERVICE,
    Pipeline,
    pipeline_roles,
)
from assay_infra.provisioning import Provisioning
from assay_infra.seed import SeedFunction
from assay_infra.tss import TenantStorage, TssCaller

REPO_ROOT = Path(__file__).resolve().parent.parent

# The in-VPC Lambda image for the auth-event consumer and the seed function.
OPS_DOCKERFILE = "api/lambda.Dockerfile"
OPS_SOURCES = (
    "api/assay_api/__init__.py",
    "api/assay_api/auth_event_consumer.py",
    "api/assay_api/seed.py",
    "api/assay_api/services.py",
    "api/assay_api/tenants.py",
    "api/assay_api/users.py",
)

config = load_config()
prefix = config.name_prefix
account_id = aws.get_caller_identity_output().account_id
aws_profile = pulumi.Config("aws").get("profile")
default_models = (config.models.default_ingest_model, config.models.default_query_model)

edge_dns = DnsAndCertificate("edge", zone_name=config.dns.hosted_zone, hosts=config.dns.hosts)
certificate = edge_dns.validated()

network = Network(prefix, cfg=config.network, region=config.region, account_id=account_id)

database = Database(f"{prefix}-db", cfg=config.database, network=network, account_id=account_id)

migrations = MigrationRunner(
    f"{prefix}-db-migrate",
    repo_root=REPO_ROOT,
    region=config.region,
    account_id=account_id,
    network=network,
    database=database,
    database_name=config.database.database_name,
    aws_profile=aws_profile,
)

alerts = Alerts(
    f"{prefix}-alarms",
    email=config.alerts.email,
    region=config.region,
    account_id=account_id,
)

# Sign-ins and password resets (section 7a). The trigger exists before the pools name it.
auth_events = AuthEvents(
    f"{prefix}-auth-events",
    repo_root=REPO_ROOT,
    region=config.region,
    account_id=account_id,
    network=network,
    alarm_topic_arn=alerts.topic.arn,
    aws_profile=aws_profile,
)

auth = Auth(
    f"{prefix}-auth",
    cfg=config.auth,
    dns=config.dns,
    region=config.region,
    account_id=account_id,
    repo_root=REPO_ROOT,
    auth_event_lambda_arn=auth_events.trigger_function.arn,
    aws_profile=aws_profile,
)
auth_events.attach(f"{prefix}-auth-events-tenant", auth.tenant.pool, account_id)
auth_events.attach(f"{prefix}-auth-events-platform", auth.platform.pool, account_id)

ops_image = ContainerImage(
    f"{prefix}-ops",
    repo_root=REPO_ROOT,
    dockerfile=OPS_DOCKERFILE,
    files=(*WORKSPACE_MANIFESTS, *OPS_SOURCES),
    trees=("db/assay_db",),
    region=config.region,
    account_id=account_id,
    lambda_pull=True,
    aws_profile=aws_profile,
)
auth_events.add_consumer(
    image_uri=ops_image.image_uri,
    database=database,
    database_name=config.database.database_name,
    pool_ids=(auth.tenant.pool.id, auth.platform.pool.id),
)

# Every TSS caller's role exists before the TSS does, which names each one with the
# accesses it may ask for (build step 6). The API signs upload and view links; the pipeline's
# functions get pipeline access; the isolation test runner (development stacks only) tries
# all three.
api_name = f"{prefix}-api"
api_role = api_task_role(api_name, account_id=account_id, region=config.region)
pipeline_name = f"{prefix}-pipeline"
pipeline_role = pipeline_roles(pipeline_name, account_id, config.region)
PIPELINE_ACCESS = ("pipeline",)
tss_callers: dict[str, TssCaller] = {
    "api": TssCaller(role=api_role, access=("read", "upload")),  # assay_api.storage.SERVICE
    SCAN_SERVICE: TssCaller(role=pipeline_role.scan, access=PIPELINE_ACCESS),
    STEPS_SERVICE: TssCaller(role=pipeline_role.steps, access=PIPELINE_ACCESS),
    SWEEPER_SERVICE: TssCaller(role=pipeline_role.sweeper, access=PIPELINE_ACCESS),
    # The worker task (build step 6, second half): its own route, /resolve-worker, against
    # an attempt grant, for one document's keys. It can never name a tenant.
    WORKER_SERVICE: TssCaller(role=pipeline_role.worker, access=("worker",)),
}
isolation_name = f"{prefix}-isolation-tests"
if config.tenancy.isolation_tests:
    tss_callers[ISOLATION_SERVICE] = TssCaller(
        role=runner_role(isolation_name, account_id), access=("pipeline", "read", "upload")
    )

storage = TenantStorage(
    f"{prefix}-tss",
    repo_root=REPO_ROOT,
    region=config.region,
    account_id=account_id,
    name_prefix=prefix,
    network=network,
    database=database,
    database_name=config.database.database_name,
    break_glass_principal_arn=config.tenancy.break_glass_principal_arn,
    alarm_topic_arn=alerts.topic.arn,
    callers=tss_callers,
    aws_profile=aws_profile,
)

provisioning = Provisioning(
    f"{prefix}-provision",
    repo_root=REPO_ROOT,
    region=config.region,
    account_id=account_id,
    name_prefix=prefix,
    network=network,
    database=database,
    database_name=config.database.database_name,
    auth=auth,
    storage=storage,
    vector_dimensions=config.tenancy.vector_dimensions,
    default_tags=config.tenancy.default_tags,
    default_models=default_models,
    upload_origins=[f"https://{config.dns.tenant_host}"],
    # Every KMS key this program creates outside the provisioning component. Add a new
    # one here: the provisioning role is denied tagging the keys in this list.
    platform_key_arns=[database.key.arn, alerts.key.arn],
    alarm_topic_arn=alerts.topic.arn,
    aws_profile=aws_profile,
)

# The API's Cognito admin function: the step 4 image, its own role and operations.
cognito_admin_api = CognitoAdminApi(
    f"{prefix}-cognito-admin-api",
    image_uri=provisioning.invite_image.image_uri,
    auth=auth,
    account_id=account_id,
)

edge_logs = EdgeLogs(f"{prefix}-edge-logs", region=config.region, account_id=account_id)

# Security notices (a quarantined upload), apart from the alarms: their own topic, published
# to by the scan function only, through the VPC's SNS endpoint (build step 6, second half).
security_notices = SecurityNotices(
    f"{prefix}-security-notices",
    email=config.alerts.security_notice_email,
    key_id=alerts.key.key_id,
    publisher_role_arn=pipeline_role.scan.arn,
    sns_endpoint_id=network.interface_endpoints["sns"].id,
)

# The ingestion pipeline (section 8; build step 6, first half).
pipeline = Pipeline(
    pipeline_name,
    repo_root=REPO_ROOT,
    region=config.region,
    account_id=account_id,
    name_prefix=prefix,
    network=network,
    database=database,
    database_name=config.database.database_name,
    roles=pipeline_role,
    tss_endpoint_url=storage.endpoint_url,
    api_task_role=api_role,
    alarm_topic_arn=alerts.topic.arn,
    security_topic_arn=security_notices.topic.arn,
    notices_key_arn=alerts.key.arn,
    worker_max_per_tenant=config.pipeline.worker_max_per_tenant,
    aws_profile=aws_profile,
)

api = ApiService(
    api_name,
    repo_root=REPO_ROOT,
    region=config.region,
    account_id=account_id,
    network=network,
    database=database,
    database_name=config.database.database_name,
    auth=auth,
    cognito_admin_function_arn=cognito_admin_api.function.arn,
    state_machine_arn=provisioning.state_machine.arn,
    task_role=api_role,
    tss_endpoint_url=storage.endpoint_url,
    ingest_queue_arn=pipeline.ingest_queue.arn,
    ingest_queue_url=pipeline.ingest_queue.url,
    certificate_arn=certificate.certificate_arn,
    access_logs_bucket=edge_logs.bucket.bucket,
    access_logs_ready=edge_logs.policy,
    alarm_topic_arn=alerts.topic.arn,
    aws_profile=aws_profile,
)

seed = SeedFunction(
    f"{prefix}-seed",
    image_uri=ops_image.image_uri,
    region=config.region,
    account_id=account_id,
    network=network,
    database=database,
    database_name=config.database.database_name,
    cognito_admin_function=cognito_admin_api.function,
    state_machine=provisioning.state_machine,
    default_models=default_models,
)

# What may be called through the VPC's endpoints, by whom.
provisioning.lambda_endpoint_access(
    [
        (
            "ApiAndSeedManageTenantUsers",
            cognito_admin_api.function.arn,
            [api.task_role.arn, seed.role.arn],
        )
    ]
)
aws.ec2.VpcEndpointPolicy(
    f"{prefix}-states-endpoint-policy",
    vpc_endpoint_id=network.interface_endpoints["states"].id,
    policy=pulumi.Output.all(
        state_machine=provisioning.state_machine.arn,
        api=api.task_role.arn,
        seed=seed.role.arn,
        pipeline=pipeline.state_machine.arn,
        starter=pipeline_role.starter.arn,
    ).apply(
        lambda a: states_endpoint_policy(
            state_machine=a["state_machine"],
            callers=[a["api"], a["seed"]],
            pipeline_state_machine=a["pipeline"],
            pipeline_callers=[a["starter"]],
        )
    ),
)
# Every queue a workload in the VPC sends to (build step 6): the auth-event trigger's, and
# the ingest queue, for its three senders. The starter reads it through its event source
# mapping, which the Lambda service polls outside the VPC.
auth_events.sqs_endpoint_access(
    [
        (
            "PipelineQueuesIngestJobs",
            pipeline.ingest_queue.arn,
            [api_role.arn, pipeline_role.scan.arn, pipeline_role.sweeper.arn],
        )
    ]
)

edge = Edge(
    f"{prefix}-edge",
    repo_root=REPO_ROOT,
    region=config.region,
    dns=config.dns,
    zone_id=edge_dns.zone.zone_id,
    certificate_arn=certificate.certificate_arn,
    network=network,
    auth=auth,
    load_balancer=api.load_balancer,
    load_balancer_security_group=api.alb_security_group,
    logs=edge_logs,
    waf_rate_limit=config.edge.waf_rate_limit,
    portal_idle_minutes=config.edge.portal_idle_minutes,
    aws_profile=aws_profile,
)

if config.tenancy.isolation_tests:
    isolation = IsolationRunner(
        isolation_name,
        role=tss_callers[ISOLATION_SERVICE].role,
        repo_root=REPO_ROOT,
        region=config.region,
        account_id=account_id,
        name_prefix=prefix,
        network=network,
        database=database,
        database_name=config.database.database_name,
        tss_endpoint_url=storage.endpoint_url,
        tenant_role_arn=storage.tenant_role.arn,
        invite_function_arn=provisioning.invite_function.arn,
        api_cognito_function_arn=cognito_admin_api.function.arn,
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
pulumi.export("auth_events_queue_url", auth_events.queue.url)
pulumi.export("auth_events_dlq_url", auth_events.dead_letter_queue.url)
pulumi.export("api_load_balancer_dns_name", api.load_balancer.dns_name)
pulumi.export("api_target_group_arn", api.target_group.arn)
pulumi.export("api_cluster_name", api.cluster.name)
pulumi.export("api_service_name", api.service.name)
pulumi.export("alarm_topic_arn", alerts.topic.arn)
pulumi.export("security_notice_topic_arn", security_notices.topic.arn)
pulumi.export("tss_endpoint_url", storage.endpoint_url)
pulumi.export("tss_function_name", storage.function.name)
pulumi.export("tenant_role_arn", storage.tenant_role.arn)
pulumi.export("break_glass_role_arn", storage.break_glass.role.arn)
pulumi.export("provisioning_state_machine_arn", provisioning.state_machine.arn)
pulumi.export("provisioning_role_arn", provisioning.task_role.arn)
pulumi.export("tenant_state_bucket", provisioning.state_bucket.bucket)
pulumi.export("cognito_admin_function_name", provisioning.invite_function.name)
pulumi.export("cognito_admin_api_function_name", cognito_admin_api.function.name)
pulumi.export("seed_function_name", seed.function.name)
pulumi.export("portal_bucket", edge.portal_bucket.bucket)
pulumi.export("distribution_id", edge.distribution.id)
pulumi.export("distribution_domain", edge.distribution.domain_name)
# Read by scripts/run_isolation.py, which runs the provisioning task's probe command.
pulumi.export("provisioning_cluster_name", provisioning.cluster.name)
pulumi.export("provisioning_task_definition_arn", provisioning.task_definition.arn)
pulumi.export("provisioning_log_group", provisioning.log_group.name)
pulumi.export("workload_security_group_id", network.workload_security_group.id)
pulumi.export("ingest_queue_url", pipeline.ingest_queue.url)
pulumi.export("ingest_dlq_url", pipeline.ingest_dlq.url)
pulumi.export("scan_dlq_url", pipeline.scan_dlq.url)
pulumi.export("pipeline_state_machine_arn", pipeline.state_machine.arn)
pulumi.export("pipeline_function_names", {k: f.name for k, f in pipeline.functions.items()})
# Read by scripts/run_isolation.py, which runs the worker task's probe command.
pulumi.export("worker_cluster_name", pipeline.worker_cluster.name)
pulumi.export("worker_task_definition_arn", pipeline.worker_task_definition.arn)
pulumi.export("worker_log_group", pipeline.worker_log_group.name)
