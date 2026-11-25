"""A wait for new IAM roles to be visible to other services, before a policy names them.

IAM is eventually consistent: for some seconds after a role is created, another AWS service
may not know it yet, and refuses a resource policy that names it as a principal ("Invalid
policy document ... ensure that Principals are valid"). The first `pulumi up` of build
step 6's second half failed this way (2026-11-25): the Tenant Storage Service's API policy
named the worker task's role, created a moment earlier in the same run. A second run
succeeded, because by then the role was known.

A policy's dependency on the role is not enough: Pulumi waits for the role to be created,
not for IAM to have told every service. So the policies that name a role created in the same
program also depend on `iam_propagation`, a local command that waits
`IAM_PROPAGATION_SECONDS` once the roles exist. It runs when it is first created and again
only when the set of roles changes (its triggers), never on an ordinary `pulumi up`, and
never during a preview. It changes nothing in AWS.

Which policies need it: those with a role created by this program in a `Principal` element.

- The TSS API's resource policy (API Gateway): the one that failed. The provider does not
  retry it.
- The tenant role's trust policy (IAM) and the tenant-state key's policy (KMS): the provider
  retries both while IAM catches up, and each was created in the same run as its role
  without failing (build step 4). They get the same wait all the same, so a first run does
  not depend on that.

Policies that name a role only in a condition (`aws:PrincipalArn`) are not checked by AWS
against IAM, and need nothing: the ingest queue's policy, the state bucket's policy and every
VPC endpoint policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pulumi
import pulumi_command as command

# IAM's own guidance is "seconds". The failed run needed less than the two minutes between
# it and the run that succeeded; half a minute is ample, and is spent once.
IAM_PROPAGATION_SECONDS = 30
WAIT = f"import time; time.sleep({IAM_PROPAGATION_SECONDS})"


def iam_propagation(
    name: str,
    *,
    roles: Sequence[pulumi.Input[str]],
    repo_root: Path,
    opts: pulumi.ResourceOptions | None = None,
) -> command.local.Command:
    """Waits once the roles exist. A resource that names them in a policy lists this in its
    `depends_on`, so it is created or updated only after the wait."""
    if not roles:
        raise ValueError("a wait for IAM needs at least one role")
    return command.local.Command(
        name,
        dir=repo_root.as_posix(),
        interpreter=["uv", "run", "--frozen", "python", "-c"],
        create=WAIT,
        # A new or replaced role has a new ARN (its name carries a random suffix), which
        # runs the wait again before the policies that name it change.
        triggers=[pulumi.Output.all(*roles).apply(lambda arns: sorted(str(arn) for arn in arns))],
        opts=opts,
    )
