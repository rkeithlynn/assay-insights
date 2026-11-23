"""Shared Pulumi mocks for the infra unit tests (no AWS calls).

Set before any test module imports a component, because Pulumi reads the mocks when a
resource is registered.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pulumi

# Python 3.14+ no longer creates a default event loop; Pulumi's mock runtime needs one.
asyncio.set_event_loop(asyncio.new_event_loop())

ACCOUNT_ID = "000000000000"
REGION = "us-east-1"

# Every resource registered with the mocks, in order: (type token, name, inputs).
REGISTERED: list[tuple[str, str, dict[str, Any]]] = []


def registered(type_token: str, name_prefix: str = "") -> list[tuple[str, dict[str, Any]]]:
    return [(n, i) for t, n, i in REGISTERED if t == type_token and n.startswith(name_prefix)]


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
        return {}, []


pulumi.runtime.set_mocks(Mocks(), preview=False)


# The one throwaway database for every module that needs PostgreSQL (see pgtest.py).
from tests.unit.pgtest import db  # noqa: E402, F401
