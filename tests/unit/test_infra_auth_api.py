"""Cognito and API components, using Pulumi mocks (no AWS calls, no JWKS fetch)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pulumi
import pytest

from assay_infra.api import ApiService, task_role_policy
from assay_infra.auth import Auth, canonical_jwks, jwks_url
from assay_infra.branding import Asset, asset_problems
from assay_infra.config import AuthConfig, build_dns, validate_auth
from assay_infra.database import Database
from assay_infra.network import Network
from tests.unit.conftest import ACCOUNT_ID, REGION, registered
from tests.unit.test_infra_data import DB, NET

REPO_ROOT = Path(__file__).resolve().parents[2]
DNS = build_dns("example.test", "example.test", "admin.example.test")
AUTH = AuthConfig(tenant_domain_prefix="t-login", platform_domain_prefix="p-login")
FAKE_JWKS = json.dumps({"keys": [{"kid": "k1", "kty": "RSA", "alg": "RS256"}]})


def _stack(prefix: str) -> tuple[Auth, ApiService, list[tuple[str, str]]]:
    fetched: list[tuple[str, str]] = []

    def fake_fetch(region: str, pool_id: str) -> str:
        fetched.append((region, pool_id))
        return FAKE_JWKS

    network = Network(prefix, cfg=NET, region=REGION, account_id=ACCOUNT_ID)
    database = Database(f"{prefix}-db", cfg=DB, network=network, account_id=ACCOUNT_ID)
    auth = Auth(
        f"{prefix}-auth",
        cfg=AUTH,
        dns=DNS,
        region=REGION,
        account_id=ACCOUNT_ID,
        repo_root=REPO_ROOT,
        jwks_fetcher=fake_fetch,
    )
    api = ApiService(
        f"{prefix}-api",
        repo_root=REPO_ROOT,
        region=REGION,
        account_id=ACCOUNT_ID,
        network=network,
        database=database,
        database_name=DB.database_name,
        auth=auth,
    )
    return auth, api, fetched


def _one(type_token: str, name: str) -> dict[str, Any]:
    matches = [inputs for n, inputs in registered(type_token) if n == name]
    assert len(matches) == 1, f"{type_token} {name}: {len(matches)} registered"
    return matches[0]


def _after_everything(auth: Auth, api: ApiService) -> pulumi.Output[list[Any]]:
    return pulumi.Output.all(
        auth.tenant.branding.id,
        auth.platform.branding.id,
        *[g.id for g in auth.tenant.groups + auth.platform.groups],
        auth.tenant_jwks,
        auth.platform_jwks,
        api.service.id,
        api.task_definition.container_definitions,
        api.task_role_policy.policy,
        *[r.id for r in api.security_group_rules],
    )


# --- Cognito ----------------------------------------------------------------------------


@pulumi.runtime.test
def test_tenant_pool() -> Any:
    auth, api, _ = _stack("c1")

    def check(_: list[Any]) -> None:
        pool = _one("aws:cognito/userPool:UserPool", "c1-auth-tenant-pool")
        assert pool["userPoolTier"] == "ESSENTIALS"
        assert pool["deletionProtection"] == "ACTIVE"
        assert pool["mfaConfiguration"] == "OPTIONAL"
        assert pool["softwareTokenMfaConfiguration"] == {"enabled": True}
        assert pool["usernameAttributes"] == ["email"]
        assert pool["adminCreateUserConfig"]["allowAdminCreateUserOnly"] is True
        assert pool["passwordPolicy"]["temporaryPasswordValidityDays"] == 7
        _assert_no_custom_templates(pool)
        assert pool["accountRecoverySetting"] == {
            "recoveryMechanisms": [{"name": "verified_email", "priority": 1}]
        }
        assert pool["emailConfiguration"] == {"emailSendingAccount": "COGNITO_DEFAULT"}
        [attribute] = pool["schemas"]
        assert attribute["name"] == "tenant_id"
        assert attribute["mutable"] is False
        assert attribute["developerOnlyAttribute"] is False
        config = pool["lambdaConfig"]["preTokenGenerationConfig"]
        assert config["lambdaVersion"] == "V2_0"
        assert config["lambdaArn"].endswith("c1-auth-pre-token-fn")
        assert "device_configuration" not in pool and "deviceConfiguration" not in pool
        groups = sorted(
            i["name"] for n, i in registered("aws:cognito/userGroup:UserGroup", "c1-auth-tenant-")
        )
        assert groups == ["tenant-admin", "tenant-user"]

    return _after_everything(auth, api).apply(check)


@pulumi.runtime.test
def test_platform_pool_requires_mfa_and_has_no_tenant_attribute() -> Any:
    auth, api, _ = _stack("c2")

    def check(_: list[Any]) -> None:
        pool = _one("aws:cognito/userPool:UserPool", "c2-auth-platform-pool")
        assert pool["userPoolTier"] == "ESSENTIALS"
        assert pool["mfaConfiguration"] == "ON"
        assert pool["softwareTokenMfaConfiguration"] == {"enabled": True}
        assert pool["adminCreateUserConfig"]["allowAdminCreateUserOnly"] is True
        assert pool["emailConfiguration"] == {"emailSendingAccount": "COGNITO_DEFAULT"}
        _assert_no_custom_templates(pool)
        assert not pool.get("schemas")
        assert not pool.get("lambdaConfig")
        groups = sorted(
            i["name"] for n, i in registered("aws:cognito/userGroup:UserGroup", "c2-auth-platform-")
        )
        assert groups == ["master-admin", "master-user"]

    return _after_everything(auth, api).apply(check)


@pulumi.runtime.test
def test_app_clients_are_public_pkce_clients_with_the_specified_lifetimes() -> Any:
    auth, api, _ = _stack("c3")

    def check(_: list[Any]) -> None:
        for kind, host in (("tenant", "example.test"), ("platform", "admin.example.test")):
            client = _one("aws:cognito/userPoolClient:UserPoolClient", f"c3-auth-{kind}-client")
            assert client["generateSecret"] is False
            assert client["allowedOauthFlows"] == ["code"]
            assert client["allowedOauthFlowsUserPoolClient"] is True
            assert client["supportedIdentityProviders"] == ["COGNITO"]
            assert client["explicitAuthFlows"] == ["ALLOW_REFRESH_TOKEN_AUTH"]
            assert client["callbackUrls"] == [f"https://{host}/auth/callback"]
            assert client["logoutUrls"] == [f"https://{host}/"]
            assert (client["accessTokenValidity"], client["refreshTokenValidity"]) == (60, 8)
            assert client["tokenValidityUnits"] == {
                "accessToken": "minutes",
                "idToken": "minutes",
                "refreshToken": "hours",
            }
            assert client["enableTokenRevocation"] is True
            assert client["preventUserExistenceErrors"] == "ENABLED"
            # Never empty (that would mean every attribute), never the tenant ID.
            assert client["writeAttributes"] == ["name"]
            assert "aws.cognito.signin.user.admin" in client["allowedOauthScopes"]
        tenant_reads = _one("aws:cognito/userPoolClient:UserPoolClient", "c3-auth-tenant-client")
        assert "custom:tenant_id" in tenant_reads["readAttributes"]

    return _after_everything(auth, api).apply(check)


@pulumi.runtime.test
def test_managed_login_domains_and_branding() -> Any:
    auth, api, _ = _stack("c4")

    def check(_: list[Any]) -> None:
        for kind, prefix in (("tenant", "t-login"), ("platform", "p-login")):
            domain = _one("aws:cognito/userPoolDomain:UserPoolDomain", f"c4-auth-{kind}-domain")
            assert domain["domain"] == prefix
            assert domain["managedLoginVersion"] == 2
            branding = _one(
                "aws:cognito/managedLoginBranding:ManagedLoginBranding", f"c4-auth-{kind}-branding"
            )
            # The provider takes settings or Cognito defaults, never both.
            assert "useCognitoProvidedValues" not in branding
            settings = json.loads(branding["settings"])
            assert settings["components"]["primaryButton"]["lightMode"]["defaults"] == {
                "backgroundColor": "14213dff",
                "textColor": "ffffffff",
            }
            categories = sorted(a["category"] for a in branding["assets"])
            assert categories == ["FAVICON_SVG", "FORM_LOGO"]
            for asset in branding["assets"]:
                assert (asset["extension"], asset["colorMode"]) == ("SVG", "LIGHT")
                # What is actually uploaded passes every check in assay_infra.branding.
                uploaded = Asset(
                    path="uploaded",
                    category=asset["category"],
                    extension=asset["extension"],
                    color_mode=asset["colorMode"],
                    content=base64.b64decode(asset["bytes"]),
                )
                assert asset_problems(uploaded) == []
            logo = next(a for a in branding["assets"] if a["category"] == "FORM_LOGO")
            assert 'viewBox="0 -10 520 140"' in base64.b64decode(logo["bytes"]).decode()

    return _after_everything(auth, api).apply(check)


@pulumi.runtime.test
def test_pre_token_trigger_outside_the_vpc_reads_only_its_pool() -> Any:
    auth, api, _ = _stack("c5")

    def check(_: list[Any]) -> None:
        function = _one("aws:lambda/function:Function", "c5-auth-pre-token-fn")
        assert function["architectures"] == ["x86_64"]
        assert function["packageType"] == "Image"
        assert function["timeout"] == 5
        assert "vpcConfig" not in function
        permission = _one("aws:lambda/permission:Permission", "c5-auth-pre-token-tenant-invoke")
        assert permission["principal"] == "cognito-idp.amazonaws.com"
        assert permission["sourceArn"].endswith("c5-auth-tenant-pool")
        assert permission["sourceAccount"] == ACCOUNT_ID
        policy = json.loads(
            _one("aws:iam/rolePolicy:RolePolicy", "c5-auth-pre-token-tenant-policy")["policy"]
        )
        cognito = [s for s in policy["Statement"] if s["Sid"] == "ReadMfaSettings"]
        assert cognito == [
            {
                "Sid": "ReadMfaSettings",
                "Effect": "Allow",
                "Action": "cognito-idp:AdminGetUser",
                "Resource": f"arn:aws:mock:{REGION}:{ACCOUNT_ID}:c5-auth-tenant-pool",
            }
        ]
        # Only the tenant pool has the trigger.
        assert [n for n, _ in registered("aws:lambda/permission:Permission", "c5-")] == [
            "c5-auth-pre-token-tenant-invoke"
        ]

    return _after_everything(auth, api).apply(check)


@pulumi.runtime.test
def test_jwks_is_fetched_for_each_pool_and_given_to_the_task() -> Any:
    auth, api, fetched = _stack("c6")

    def check(args: list[Any]) -> None:
        assert sorted(fetched) == [
            (REGION, "c6-auth-platform-pool-id"),
            (REGION, "c6-auth-tenant-pool-id"),
        ]
        task = _one("aws:ecs/taskDefinition:TaskDefinition", "c6-api-task")
        [container] = json.loads(task["containerDefinitions"])
        env = {e["name"]: e["value"] for e in container["environment"]}
        assert env["COGNITO_TENANT_JWKS"] == FAKE_JWKS
        assert env["COGNITO_PLATFORM_JWKS"] == FAKE_JWKS
        assert env["COGNITO_TENANT_POOL_ID"] == "c6-auth-tenant-pool-id"
        assert env["COGNITO_TENANT_CLIENT_ID"] == "c6-auth-tenant-client-id"
        assert env["COGNITO_PLATFORM_CLIENT_ID"] == "c6-auth-platform-client-id"
        assert env["DB_NAME"] == DB.database_name
        assert env["AWS_REGION"] == REGION

    return _after_everything(auth, api).apply(check)


# --- API --------------------------------------------------------------------------------


@pulumi.runtime.test
def test_api_task_is_x86_fargate_non_root_and_read_only() -> Any:
    auth, api, _ = _stack("a1")

    def check(_: list[Any]) -> None:
        task = _one("aws:ecs/taskDefinition:TaskDefinition", "a1-api-task")
        assert (task["cpu"], task["memory"]) == ("512", "1024")
        assert task["requiresCompatibilities"] == ["FARGATE"]
        assert task["networkMode"] == "awsvpc"
        assert task["runtimePlatform"] == {
            "cpuArchitecture": "X86_64",
            "operatingSystemFamily": "LINUX",
        }
        [container] = json.loads(task["containerDefinitions"])
        assert container["user"] == "10001:10001"
        assert container["readonlyRootFilesystem"] is True
        assert container["privileged"] is False
        assert container["linuxParameters"] == {"capabilities": {"drop": ["ALL"]}}
        assert container["image"].endswith("@sha256:" + "0" * 64)
        assert container["logConfiguration"]["logDriver"] == "awslogs"
        service = _one("aws:ecs/service:Service", "a1-api-service")
        assert service["desiredCount"] == 1
        assert service["launchType"] == "FARGATE"
        assert service["networkConfiguration"]["assignPublicIp"] is False
        assert service["networkConfiguration"]["subnets"] == ["a1-app-use1-az2-id"]
        assert service["enableExecuteCommand"] is False
        assert service["deploymentCircuitBreaker"] == {"enable": True, "rollback": True}

    return _after_everything(auth, api).apply(check)


@pulumi.runtime.test
def test_load_balancer_is_internal_and_unreachable_until_the_edge() -> Any:
    auth, api, _ = _stack("a2")

    def check(_: list[Any]) -> None:
        alb = _one("aws:lb/loadBalancer:LoadBalancer", "a2-api-alb")
        assert alb["internal"] is True
        assert alb["loadBalancerType"] == "application"
        assert sorted(alb["subnets"]) == ["a2-app-use1-az2-id", "a2-app-use1-az4-id"]
        assert alb["dropInvalidHeaderFields"] is True
        # No inbound rule on the load balancer's group: CloudFront's VPC origin adds it.
        ingress = registered("aws:vpc/securityGroupIngressRule:SecurityGroupIngressRule", "a2-")
        alb_ingress = [i for _, i in ingress if i["securityGroupId"] == "a2-api-alb-sg-id"]
        assert alb_ingress == []
        # The task's port is open to the load balancer only.
        task_in = [i for _, i in ingress if i["securityGroupId"] == "a2-api-task-sg-id"]
        assert [(i["fromPort"], i["referencedSecurityGroupId"]) for i in task_in] == [
            (8000, "a2-api-alb-sg-id")
        ]
        api_rules = registered(
            "aws:vpc/securityGroupIngressRule:SecurityGroupIngressRule", "a2-api-"
        )
        assert api_rules and all("cidrIpv4" not in i for _, i in api_rules)
        tg = _one("aws:lb/targetGroup:TargetGroup", "a2-api-tg")
        assert tg["targetType"] == "ip"
        assert tg["healthCheck"]["path"] == "/api/health"

    return _after_everything(auth, api).apply(check)


@pulumi.runtime.test
def test_task_role_may_only_sign_in_as_the_two_api_roles() -> Any:
    auth, api, _ = _stack("a3")

    def check(_: list[Any]) -> None:
        policy = json.loads(_one("aws:iam/rolePolicy:RolePolicy", "a3-api-task-policy")["policy"])
        [statement] = policy["Statement"]
        assert statement["Action"] == "rds-db:connect"
        prefix = f"arn:aws:rds-db:{REGION}:{ACCOUNT_ID}:dbuser:cluster-MOCKRESOURCEID/"
        assert statement["Resource"] == [f"{prefix}app_tenant", f"{prefix}platform_reader"]

    return _after_everything(auth, api).apply(check)


def test_task_role_policy_sorts_resources() -> None:
    policy = json.loads(task_role_policy(["b", "a"]))
    assert policy["Statement"][0]["Resource"] == ["a", "b"]


# --- Helpers ------------------------------------------------------------------------------

# Pool inputs that hold message text. Cognito validates them only at deploy time, and the
# provider sends every invitation template field, set or not ("" fails: the failed first
# `pulumi up` of step 3). With Cognito's default sender none of them may be set.
MESSAGE_INPUTS = (
    "emailVerificationMessage",
    "emailVerificationSubject",
    "smsAuthenticationMessage",
    "smsVerificationMessage",
    "verificationMessageTemplate",
)


def _assert_no_custom_templates(pool: dict[str, Any]) -> None:
    assert pool["emailConfiguration"]["emailSendingAccount"] == "COGNITO_DEFAULT"
    assert "inviteMessageTemplate" not in pool["adminCreateUserConfig"]
    assert pool["adminCreateUserConfig"] == {"allowAdminCreateUserOnly": True}
    for name in MESSAGE_INPUTS:
        assert name not in pool, name
    # Without an SMS configuration no SMS can be sent, so no SMS text is needed either.
    assert "smsConfiguration" not in pool


def test_canonical_jwks_keeps_only_public_signing_keys() -> None:
    document = {
        "keys": [
            {"kid": "b", "kty": "RSA", "alg": "RS256", "use": "sig", "n": "nb", "e": "AQAB"},
            {
                "kid": "a",
                "kty": "RSA",
                "alg": "RS256",
                "use": "sig",
                "n": "na",
                "e": "AQAB",
                "x": 1,
            },
            {"kid": "enc", "kty": "RSA", "alg": "RSA-OAEP", "use": "enc", "n": "n", "e": "AQAB"},
        ]
    }
    result = json.loads(canonical_jwks(document))
    assert [k["kid"] for k in result["keys"]] == ["a", "b"]
    assert set(result["keys"][0]) == {"alg", "e", "kid", "kty", "n", "use"}
    with pytest.raises(ValueError, match="no RS256 signing key"):
        canonical_jwks({"keys": []})


def test_jwks_url() -> None:
    assert jwks_url("us-east-1", "us-east-1_Abc") == (
        "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Abc/.well-known/jwks.json"
    )


@pytest.mark.parametrize(
    ("tenant", "platform", "message"),
    [
        ("same", "same", "must differ"),
        ("Upper", "ok", "lower-case"),
        ("my-cognito", "ok", "aws/amazon/cognito"),
        ("-dash", "ok", "lower-case"),
    ],
)
def test_auth_config_rejects(tenant: str, platform: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_auth(AuthConfig(tenant, platform))
