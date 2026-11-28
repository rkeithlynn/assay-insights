"""Shared Pulumi mocks for the infra unit tests (no AWS calls).

Set before any test module imports a component, because Pulumi reads the mocks when a
resource is registered.

**A check must wait for `settled()`** (or use `after_settled(...)` where it used
`pulumi.Output.all(...)`). A resource is registered with the mocks some time after its
constructor returns, when its inputs have resolved. A check that runs as soon as a few chosen
outputs resolve can run before a resource it reads is registered: it then fails by chance
(CI, 2026-11-27: "`c5-auth-pre-token-tenant-policy`: 0 registered"), or, worse, a check of
the form "every registered X has ..." passes over a stack with resources missing. `settled()`
resolves only once every resource the test constructed is registered, and `registered()`
refuses to answer before that, so the race cannot come back unseen.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pulumi
import pytest

# Python 3.14+ no longer creates a default event loop; Pulumi's mock runtime needs one.
asyncio.set_event_loop(asyncio.new_event_loop())

ACCOUNT_ID = "000000000000"
REGION = "us-east-1"

# Every resource registered with the mocks, in order: (type token, name, inputs).
REGISTERED: list[tuple[str, str, dict[str, Any]]] = []
# Data-source lookups the tests inspect: (token, arguments).
CALLS: list[tuple[str, dict[str, Any]]] = []
# What the mocks answer for CloudFront's VPC origin security group and the ELB log account.
CLOUDFRONT_VPC_ORIGIN_SG = "sg-cloudfront-vpc-origins"
ELB_ACCOUNT_ID = "111111111111"


# Every resource the current test has constructed, in order (emptied before each test).
CREATED: list[pulumi.Resource] = []
# Where the current test's registrations begin in REGISTERED.
_TEST_START = [0]
# The engine's own root resource: the mocks are never asked to register it.
STACK_TYPE = "pulumi:pulumi:Stack"
# Far longer than any registration takes: a resource that is never registered is an error
# with its name, not a test that hangs.
SETTLE_TIMEOUT_SECONDS = 120.0

_resource_init = pulumi.Resource.__init__


def _recording_init(self: pulumi.Resource, *args: Any, **kwargs: Any) -> None:
    _resource_init(self, *args, **kwargs)
    CREATED.append(self)


pulumi.Resource.__init__ = _recording_init  # type: ignore[method-assign]


def unregistered() -> list[str]:
    """The resources this test constructed that the mocks have not been given yet."""
    seen = {(t, n) for t, n, _ in REGISTERED[_TEST_START[0] :]}
    return [
        f"{r.pulumi_resource_type} {r.pulumi_resource_name}"
        for r in CREATED
        if r.pulumi_resource_type != STACK_TYPE
        and (r.pulumi_resource_type, r.pulumi_resource_name) not in seen
    ]


def settled() -> pulumi.Output[int]:
    """Resolves once every resource constructed in this test is registered with the mocks:
    the ones constructed before this call, and any constructed while it waits. Its value is
    how many there were."""

    async def wait() -> int:
        done = 0
        while True:
            while done < len(CREATED):
                resource = CREATED[done]
                try:
                    # A resource's URN resolves when its registration has returned.
                    await asyncio.wait_for(resource.urn.future(), SETTLE_TIMEOUT_SECONDS)
                except TimeoutError:
                    raise AssertionError(
                        f"{resource.pulumi_resource_type} {resource.pulumi_resource_name} "
                        "was never registered with the mocks"
                    ) from None
                done += 1
            # A callback that constructs a resource may be waiting for its turn: let it run.
            for _ in range(5):
                await asyncio.sleep(0)
            if done == len(CREATED):
                return done

    return pulumi.Output.from_input(asyncio.ensure_future(wait()))


def after_settled(*outputs: Any) -> pulumi.Output[list[Any]]:
    """`pulumi.Output.all(*outputs)`, with the same values in the same order, resolved only
    once every resource of the test is registered (`settled`). What a check waits for."""
    return pulumi.Output.all(pulumi.Output.all(*outputs), settled()).apply(lambda both: both[0])


def require_settled() -> None:
    """Fails if a resource of this test is not registered yet: the check came too early."""
    missing = unregistered()
    assert not missing, (
        f"the check ran before {len(missing)} of this test's resources were registered with "
        f"the mocks (the first: {missing[0]}). Wait for `settled()`, or use "
        "`after_settled(...)` in place of `pulumi.Output.all(...)`."
    )


def registered(type_token: str, name_prefix: str = "") -> list[tuple[str, dict[str, Any]]]:
    """The resources of a type the mocks were given, by name prefix. Refuses to answer while
    a resource of the current test is still on its way (`require_settled`)."""
    require_settled()
    return [(n, i) for t, n, i in REGISTERED if t == type_token and n.startswith(name_prefix)]


@pytest.fixture(autouse=True)
def _mock_registrations_of_this_test() -> Iterator[None]:
    """Each test starts with no resources of its own."""
    CREATED.clear()
    _TEST_START[0] = len(REGISTERED)
    yield


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str, dict[str, Any]]:
        REGISTERED.append((args.typ, args.name, dict(args.inputs)))
        outputs = dict(args.inputs)
        arn = f"arn:aws:mock:{REGION}:{ACCOUNT_ID}:{args.name}"
        match args.typ:
            case "aws:route53/zone:Zone":
                outputs["zoneId"] = "Z123"
                outputs["nameServers"] = [f"ns-{i}.awsdns.test" for i in range(4)]
            case "aws:acm/certificate:Certificate":
                names = [args.inputs["domainName"], *args.inputs.get("subjectAlternativeNames", [])]
                outputs["arn"] = f"arn:aws:acm:{REGION}:{ACCOUNT_ID}:certificate/mock"
                outputs["domainValidationOptions"] = [
                    {
                        "domainName": n,
                        "resourceRecordName": f"_token.{n}.",
                        "resourceRecordType": "CNAME",
                        "resourceRecordValue": f"_value.{n}.acm-validations.aws.",
                    }
                    for n in names
                ]
            case "aws:ec2/subnet:Subnet":
                outputs["availabilityZone"] = f"zone-of-{args.inputs['availabilityZoneId']}"
            case "aws:ec2/vpcEndpoint:VpcEndpoint":
                outputs["prefixListId"] = "pl-mock"
            case "aws:rds/cluster:Cluster":
                outputs["endpoint"] = "cluster.mock.rds.test"
                outputs["clusterResourceId"] = "cluster-MOCKRESOURCEID"
                outputs["masterUserSecrets"] = [
                    {"secretArn": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:rds-mock"}
                ]
            case "aws:ecr/repository:Repository":
                outputs["repositoryUrl"] = (
                    f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{args.inputs['name']}"
                )
                outputs["registryId"] = ACCOUNT_ID
            case "command:local:Command":
                outputs["stdout"] = "sha256:" + "0" * 64
            case "aws:iam/role:Role":
                # An auto-named role: AWS adds a random suffix to the logical name.
                outputs.setdefault("name", f"{args.name}-a1b2c3d")
                arn = f"arn:aws:iam::{ACCOUNT_ID}:role/{outputs['name']}"
            case "aws:s3/bucket:Bucket":
                outputs.setdefault("bucket", f"{args.name}-a1b2c3d")
                arn = f"arn:aws:s3:::{outputs['bucket']}"
            case "aws:kms/key:Key":
                outputs["keyId"] = f"{args.name}-key-id"
                arn = f"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/{args.name}-key-id"
            case "aws:lambda/function:Function":
                outputs["invokeArn"] = f"arn:aws:apigateway:{REGION}:lambda:path/{arn}/invocations"
            case "aws:apigateway/restApi:RestApi":
                outputs["rootResourceId"] = "root"
                outputs["executionArn"] = (
                    f"arn:aws:execute-api:{REGION}:{ACCOUNT_ID}:{args.name}-id"
                )
            case "aws:sqs/queue:Queue":
                outputs["url"] = f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT_ID}/{args.name}"
                outputs.setdefault("name", args.name)
            case "aws:ecs/cluster:Cluster":
                arn = f"arn:aws:ecs:{REGION}:{ACCOUNT_ID}:cluster/{args.inputs['name']}"
            case "aws:ecs/taskDefinition:TaskDefinition":
                arn = f"arn:aws:ecs:{REGION}:{ACCOUNT_ID}:task-definition/{args.inputs['family']}:1"
        outputs.setdefault("arn", arn)
        return f"{args.name}-id", outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[Any]]:
        match args.token:
            case "aws:index/getCallerIdentity:getCallerIdentity":
                return {"accountId": ACCOUNT_ID, "arn": "arn", "userId": "user", "id": "id"}, []
            case "aws:ec2/getSecurityGroup:getSecurityGroup":
                CALLS.append((args.token, dict(args.args)))
                return {"id": CLOUDFRONT_VPC_ORIGIN_SG, "vpcId": args.args.get("vpcId")}, []
            case "aws:cloudfront/getCachePolicy:getCachePolicy":
                return {"id": f"cache-policy-{args.args['name']}", "name": args.args["name"]}, []
            case "aws:cloudfront/getOriginRequestPolicy:getOriginRequestPolicy":
                return {"id": f"origin-policy-{args.args['name']}", "name": args.args["name"]}, []
            case "aws:elb/getServiceAccount:getServiceAccount":
                return {"id": ELB_ACCOUNT_ID, "arn": f"arn:aws:iam::{ELB_ACCOUNT_ID}:root"}, []
        return {}, []


pulumi.runtime.set_mocks(Mocks(), preview=False)


# The one throwaway database for every module that needs PostgreSQL (see pgtest.py).
from tests.unit.pgtest import db  # noqa: E402, F401

# The build step 6 tests' database (see step6.py).
from tests.unit.step6 import step6_db  # noqa: E402, F401
