"""What the provisioning task is told by the platform stack, through its environment.

Nothing here is an identifier written in code: every ARN, name and number comes from the
task definition, which the platform's Pulumi program fills in.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from assay_tss import naming
from assay_tss.policies import Principals

# Pulumi project of the per-tenant stacks: `assay-tenant/<tenant_id>`.
PROJECT = "assay-tenant"


@dataclass(frozen=True)
class ProvisionSettings:
    region: str
    account_id: str
    name_prefix: str
    principals: Principals
    # Where the tenant stacks' state lives, and the key that encrypts their secrets.
    state_bucket: str
    state_key_arn: str
    # The Cognito admin Lambda (outside the VPC) that invites a tenant's first admin.
    invite_function: str
    vector_dimensions: int
    default_tags: Mapping[str, str]
    # Scratch space for the Pulumi workspace; the task's only writable directory.
    work_dir: Path
    # Extra arguments for the AWS provider. Empty in the task; the image test uses it to
    # run a preview with no network.
    provider_args: Mapping[str, Any] = field(default_factory=dict)
    # Overrides for the image test: a local backend and a passphrase.
    backend_url: str | None = None
    secrets_provider: str | None = None

    @property
    def backend(self) -> str:
        return self.backend_url or f"s3://{self.state_bucket}?region={self.region}&awssdk=v2"

    @property
    def secrets(self) -> str:
        return self.secrets_provider or f"awskms:///{self.state_key_arn}?region={self.region}"

    @classmethod
    def from_env(cls) -> ProvisionSettings:
        env = os.environ
        tags = json.loads(env["DEFAULT_TAGS"])
        if not isinstance(tags, dict):
            raise ValueError("DEFAULT_TAGS must be a JSON object")
        return cls(
            region=env["AWS_REGION"],
            account_id=env["ACCOUNT_ID"],
            name_prefix=naming.check_prefix(env["NAME_PREFIX"]),
            principals=Principals(
                tenant_role_arn=env["TENANT_ROLE_ARN"],
                break_glass_role_arn=env["BREAK_GLASS_ROLE_ARN"],
                provisioning_role_arn=env["PROVISIONING_ROLE_ARN"],
                scan_role_arn=env["SCAN_ROLE_ARN"],
            ),
            state_bucket=env["STATE_BUCKET"],
            state_key_arn=env["STATE_KEY_ARN"],
            invite_function=env["INVITE_FUNCTION"],
            vector_dimensions=int(env["VECTOR_DIMENSIONS"]),
            default_tags={str(k): str(v) for k, v in tags.items()},
            work_dir=Path(env["WORK_DIR"]),
        )
