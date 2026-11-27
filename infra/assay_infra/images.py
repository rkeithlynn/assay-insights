"""Private ECR repositories and the container images Pulumi builds into them.

Images are built for linux/amd64 inside Docker (never packaged on the Windows host) by
`scripts/build_image.py`, during `pulumi up` only, and again only when the image's source
files change: their hash is the tag, and repositories are tag-immutable. See that script
for why the build runs through the Docker CLI with attestations turned off.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from pathlib import Path

import pulumi
import pulumi_aws as aws
import pulumi_command as command

BUILD_SCRIPT = "scripts/build_image.py"
IMAGES_KEPT = 10

# Every image that installs locked dependencies runs `uv export --locked`, which needs the
# manifest of every workspace member.
WORKSPACE_MANIFESTS = (
    "pyproject.toml",
    "uv.lock",
    "infra/pyproject.toml",
    "db/pyproject.toml",
    "api/pyproject.toml",
    "schemas/pyproject.toml",
    "tss/pyproject.toml",
    "workers/pyproject.toml",
)


def _source_files(repo_root: Path, files: Sequence[str], trees: Sequence[str]) -> Iterable[Path]:
    for rel in files:
        yield repo_root / rel
    for tree in trees:
        for path in sorted((repo_root / tree).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                yield path


class CrlfSources(ValueError):
    """An image's source files have CRLF line endings on disk."""


def source_tag(repo_root: Path, files: Sequence[str], trees: Sequence[str] = ()) -> str:
    """A tag that changes exactly when one of the image's source files changes.

    Refuses a source file saved with CRLF line endings (owner, 2026-11-27, after it happened
    twice). Git stores every one of these files with LF and shows no difference for a CRLF
    copy, but the image is built from the bytes on disk and this tag is their hash: the image
    would hold the CRLF copy under a tag no clean checkout produces. So `pulumi preview` and
    `pulumi up` stop here, before anything is built.
    """
    digest = hashlib.sha256()
    crlf: list[str] = []
    for path in _source_files(repo_root, files, trees):
        name = path.relative_to(repo_root).as_posix()
        data = path.read_bytes()
        # Every source here is text (a binary file would hold a NUL byte).
        if b"\r\n" in data and b"\0" not in data:
            crlf.append(name)
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    if crlf:
        raise CrlfSources(
            f"{len(crlf)} image source file(s) have CRLF line endings on disk "
            f"({', '.join(crlf[:5])}{', ...' if len(crlf) > 5 else ''}). "
            "Rewrite them with LF: uv run scripts/check_line_endings.py --fix"
        )
    return f"src-{digest.hexdigest()[:16]}"


def parse_digest(stdout: str) -> str:
    """The build script prints only the pushed image's digest on stdout."""
    lines = stdout.strip().splitlines()
    digest = lines[-1].strip() if lines else ""
    algorithm, _, value = digest.partition(":")
    if algorithm != "sha256" or len(value) != 64:
        raise ValueError(f"image build printed no digest: {stdout!r}")
    return digest


def _json(document: object) -> str:
    return json.dumps(document, separators=(",", ":"))


def lifecycle_policy() -> str:
    return _json(
        {
            "rules": [
                {
                    "rulePriority": 1,
                    "description": f"Keep the last {IMAGES_KEPT} images",
                    "selection": {
                        "tagStatus": "any",
                        "countType": "imageCountMoreThan",
                        "countNumber": IMAGES_KEPT,
                    },
                    "action": {"type": "expire"},
                }
            ]
        }
    )


def lambda_pull_policy(region: str, account_id: str) -> str:
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "LambdaPull",
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                    "Condition": {
                        "StringLike": {
                            "aws:sourceArn": f"arn:aws:lambda:{region}:{account_id}:function:*"
                        }
                    },
                }
            ],
        }
    )


class ContainerImage(pulumi.ComponentResource):
    """One ECR repository and the image built from one Dockerfile into it.

    `image_uri` is `<repository>@<digest>`, so a task or function always runs exactly the
    image that was pushed.
    """

    def __init__(
        self,
        name: str,
        *,
        repo_root: Path,
        dockerfile: str,
        files: Sequence[str],
        trees: Sequence[str] = (),
        region: str,
        account_id: pulumi.Input[str],
        lambda_pull: bool = False,
        aws_profile: str | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:compute:ContainerImage", name, None, opts)
        child = pulumi.ResourceOptions(parent=self)

        self.repository = aws.ecr.Repository(
            f"{name}-repo",
            name=name,
            image_tag_mutability="IMMUTABLE",
            image_scanning_configuration=aws.ecr.RepositoryImageScanningConfigurationArgs(
                scan_on_push=True
            ),
            encryption_configurations=[
                aws.ecr.RepositoryEncryptionConfigurationArgs(encryption_type="KMS")
            ],
            force_delete=False,
            opts=child,
        )
        aws.ecr.LifecyclePolicy(
            f"{name}-repo-lifecycle",
            repository=self.repository.name,
            policy=lifecycle_policy(),
            opts=child,
        )
        if lambda_pull:
            aws.ecr.RepositoryPolicy(
                f"{name}-repo-policy",
                repository=self.repository.name,
                policy=pulumi.Output.from_input(account_id).apply(
                    lambda acct: lambda_pull_policy(region, acct)
                ),
                opts=child,
            )

        self.tag = source_tag(repo_root, [*files, dockerfile], trees)
        environment: dict[str, pulumi.Input[str]] = {
            "IMAGE_REPOSITORY_URL": self.repository.repository_url,
            "IMAGE_TAG": self.tag,
            "IMAGE_DOCKERFILE": dockerfile,
            "IMAGE_CONTEXT": repo_root.as_posix(),
            "AWS_REGION": region,
        }
        if aws_profile:
            environment["AWS_PROFILE"] = aws_profile
        self.build = command.local.Command(
            f"{name}-image",
            dir=repo_root.as_posix(),
            interpreter=["uv", "run", "--frozen", "python"],
            create=BUILD_SCRIPT,
            environment=environment,
            triggers=[self.repository.repository_url, self.tag],
            logging=command.local.Logging.STDERR,
            opts=child,
        )
        self.digest = self.build.stdout.apply(parse_digest)
        self.image_uri = pulumi.Output.concat(self.repository.repository_url, "@", self.digest)

        self.register_outputs({"image_uri": self.image_uri})
