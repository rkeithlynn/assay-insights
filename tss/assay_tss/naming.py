"""Names of a tenant's storage resources.

Every name is `<prefix>-t-<8 hex>`, where the 8 hex digits are the start of the tenant ID.
A name never contains the customer's name (section 6, rule 1). Names cannot be changed
after creation, so the tenant role's policy can match on them.
"""

from __future__ import annotations

import re
import uuid

# The one vector index in a tenant's vector bucket.
VECTOR_INDEX = "documents"
VECTOR_BUCKET_SUFFIX = "-vectors"

_PREFIX = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")


def check_prefix(name_prefix: str) -> str:
    if not _PREFIX.match(name_prefix):
        raise ValueError(f"name prefix {name_prefix!r} must be lower-case letters, digits, hyphens")
    return name_prefix


def tenant_slug(name_prefix: str, tenant_id: uuid.UUID) -> str:
    return f"{check_prefix(name_prefix)}-t-{tenant_id.hex[:8]}"


def bucket_name(name_prefix: str, tenant_id: uuid.UUID) -> str:
    return tenant_slug(name_prefix, tenant_id)


def vector_bucket_name(name_prefix: str, tenant_id: uuid.UUID) -> str:
    return tenant_slug(name_prefix, tenant_id) + VECTOR_BUCKET_SUFFIX


def face_collection_id(name_prefix: str, tenant_id: uuid.UUID) -> str:
    """The Rekognition collection has the bucket's name."""
    return tenant_slug(name_prefix, tenant_id)


def key_alias(name_prefix: str, tenant_id: uuid.UUID) -> str:
    return f"alias/{tenant_slug(name_prefix, tenant_id)}"


def tenant_pattern(name_prefix: str) -> str:
    """Matches every tenant's bucket and collection name, for IAM resource ARNs."""
    return f"{check_prefix(name_prefix)}-t-*"
