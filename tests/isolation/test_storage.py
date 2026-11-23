"""Storage isolation, against the real account (BUILD_SPEC.md section 6).

Three groups:

1. A tenant's TSS-issued credentials work on its own bucket, key, vector index and face
   collection (so an environment where everything is refused cannot pass), and are refused
   on the other tenant's. Run in both directions.
2. The credentials do only what was asked for (upload cannot read, read cannot write), last
   15 minutes, cannot start another session, and cannot change the tags the policies match.
3. A principal whose own IAM policy allows it (the runner's role) is still refused by the
   tenant role's trust policy and by each tenant's bucket and key policies.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

import boto3
import pytest

from assay_tss import naming
from assay_tss.models import StorageAccess, StorageContext
from assay_tss.policies import BUCKET_TAG, TENANT_TAG, VECTOR_BUCKET_TAG
from tests.isolation.conftest import PROBE_TAG, Env, Pair, assert_denied, client

if TYPE_CHECKING:
    from mypy_boto3_kms import KMSClient
    from mypy_boto3_lambda import LambdaClient
    from mypy_boto3_rekognition import RekognitionClient
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3vectors import S3VectorsClient
    from mypy_boto3_s3vectors.type_defs import VectorDataTypeDef
    from mypy_boto3_sts import STSClient

Resolve = Callable[[uuid.UUID, StorageAccess], StorageContext]


def s3(session: boto3.Session, region: str) -> S3Client:
    return cast("S3Client", client(session, "s3", region))


def kms(session: boto3.Session, region: str) -> KMSClient:
    return cast("KMSClient", client(session, "kms", region))


def vectors(session: boto3.Session, region: str) -> S3VectorsClient:
    return cast("S3VectorsClient", client(session, "s3vectors", region))


def faces(session: boto3.Session, region: str) -> RekognitionClient:
    return cast("RekognitionClient", client(session, "rekognition", region))


def sts(session: boto3.Session, region: str) -> STSClient:
    return cast("STSClient", client(session, "sts", region))


def lambda_(session: boto3.Session, region: str) -> LambdaClient:
    return cast("LambdaClient", client(session, "lambda", region))


def vector_bucket_arn(env: Env, context: StorageContext) -> str:
    return f"arn:aws:s3vectors:{env.region}:{env.account_id}:bucket/{context.vector_bucket}"


def collection_arn(env: Env, context: StorageContext) -> str:
    return f"arn:aws:rekognition:{env.region}:{env.account_id}:collection/{context.face_collection}"


# --- The contexts themselves ----------------------------------------------------------------


def test_each_tenant_gets_its_own_resources(
    env: Env, context_a: StorageContext, context_b: StorageContext
) -> None:
    for tenant_id, context in ((env.tenant_a, context_a), (env.tenant_b, context_b)):
        assert context.tenant_id == tenant_id
        assert context.bucket == naming.bucket_name(env.name_prefix, tenant_id)
        assert context.vector_bucket == naming.vector_bucket_name(env.name_prefix, tenant_id)
        assert context.face_collection == naming.face_collection_id(env.name_prefix, tenant_id)
        assert context.vector_index == naming.VECTOR_INDEX
    assert context_a.bucket != context_b.bucket
    assert context_a.kms_key_arn != context_b.kms_key_arn
    assert context_a.vector_bucket != context_b.vector_bucket


def test_credentials_last_at_most_15_minutes(tenant: StorageContext) -> None:
    remaining = tenant.credentials.expiration - dt.datetime.now(dt.UTC)
    assert dt.timedelta(0) < remaining <= dt.timedelta(minutes=15)


def test_session_is_the_tenant_role_tagged_for_one_tenant(env: Env, tenant: StorageContext) -> None:
    identity = sts(tenant.session(), env.region).get_caller_identity()
    session_name = f"t-{tenant.tenant_id.hex[:8]}-{StorageAccess.PIPELINE.value}"
    assert identity["Arn"].endswith(f":assumed-role/{env.tenant_role_name}/{session_name}")


# --- 1. Own resources work; the other tenant's are refused -----------------------------------


def test_own_bucket_is_reachable(tenant: StorageContext) -> None:
    response = s3(tenant.session(), tenant.region).list_objects_v2(Bucket=tenant.bucket, MaxKeys=1)
    assert response["ResponseMetadata"]["HTTPStatusCode"] == 200


def test_own_key_is_reachable(tenant: StorageContext) -> None:
    described = kms(tenant.session(), tenant.region).describe_key(KeyId=tenant.kms_key_arn)
    assert described["KeyMetadata"]["Arn"] == tenant.kms_key_arn
    assert described["KeyMetadata"]["KeyManager"] == "CUSTOMER"


def test_own_vector_index_is_reachable(tenant: StorageContext) -> None:
    api = vectors(tenant.session(), tenant.region)
    index = api.get_index(vectorBucketName=tenant.vector_bucket, indexName=tenant.vector_index)
    assert index["index"]["indexName"] == tenant.vector_index
    listed = api.list_vectors(
        vectorBucketName=tenant.vector_bucket, indexName=tenant.vector_index, maxResults=1
    )
    assert listed["ResponseMetadata"]["HTTPStatusCode"] == 200


def test_own_face_collection_is_reachable(tenant: StorageContext) -> None:
    described = faces(tenant.session(), tenant.region).describe_collection(
        CollectionId=tenant.face_collection
    )
    assert described["ResponseMetadata"]["HTTPStatusCode"] == 200


def test_other_tenants_bucket_is_refused(pair: Pair) -> None:
    api = s3(pair.own.session(), pair.own.region)
    other = pair.other
    assert_denied(lambda: api.list_objects_v2(Bucket=other.bucket, MaxKeys=1))
    assert_denied(lambda: api.get_object(Bucket=other.bucket, Key=other.text_key(uuid.uuid4())))
    assert_denied(
        lambda: api.head_object(Bucket=other.bucket, Key=other.original_key(uuid.uuid4()))
    )
    assert_denied(
        lambda: api.put_object(
            Bucket=other.bucket,
            Key=other.inbox_key(uuid.uuid4(), uuid.uuid4()),
            Body=b"isolation probe",
        )
    )
    assert_denied(lambda: api.delete_object(Bucket=other.bucket, Key=other.text_key(uuid.uuid4())))


def test_other_tenants_key_is_refused(pair: Pair) -> None:
    api = kms(pair.own.session(), pair.own.region)
    key = pair.other.kms_key_arn
    assert_denied(lambda: api.describe_key(KeyId=key))
    assert_denied(lambda: api.generate_data_key(KeyId=key, KeySpec="AES_256"))
    assert_denied(lambda: api.encrypt(KeyId=key, Plaintext=b"isolation probe"))


def test_other_tenants_vector_index_is_refused(pair: Pair) -> None:
    api = vectors(pair.own.session(), pair.own.region)
    other = pair.other
    own_index = api.get_index(
        vectorBucketName=pair.own.vector_bucket, indexName=pair.own.vector_index
    )
    dimension = own_index["index"]["dimension"]
    bucket, index = other.vector_bucket, other.vector_index
    probe: VectorDataTypeDef = {"float32": [0.0] * dimension}
    assert_denied(lambda: api.get_index(vectorBucketName=bucket, indexName=index))
    assert_denied(lambda: api.list_vectors(vectorBucketName=bucket, indexName=index, maxResults=1))
    assert_denied(
        lambda: api.query_vectors(
            vectorBucketName=bucket, indexName=index, topK=1, queryVector=probe
        )
    )
    assert_denied(
        lambda: api.put_vectors(
            vectorBucketName=bucket,
            indexName=index,
            vectors=[{"key": "isolation-probe", "data": probe}],
        )
    )


def test_other_tenants_face_collection_is_refused(pair: Pair) -> None:
    api = faces(pair.own.session(), pair.own.region)
    collection = pair.other.face_collection
    assert_denied(lambda: api.describe_collection(CollectionId=collection))
    assert_denied(lambda: api.list_faces(CollectionId=collection, MaxResults=1))


# --- 2. The credentials do only what was asked for -------------------------------------------


def test_a_tenant_key_works_only_through_storage(tenant: StorageContext) -> None:
    """Even a tenant's own key cannot be used directly, only by S3 and S3 Vectors for it."""
    api = kms(tenant.session(), tenant.region)
    assert_denied(lambda: api.generate_data_key(KeyId=tenant.kms_key_arn, KeySpec="AES_256"))


def test_upload_access_cannot_read_or_list(env: Env, resolve: Resolve) -> None:
    upload = resolve(env.tenant_a, StorageAccess.UPLOAD)
    api = s3(upload.session(), upload.region)
    assert_denied(lambda: api.list_objects_v2(Bucket=upload.bucket, MaxKeys=1))
    assert_denied(lambda: api.get_object(Bucket=upload.bucket, Key=upload.text_key(uuid.uuid4())))
    # Outside inbox/ an upload is refused too.
    assert_denied(
        lambda: api.put_object(
            Bucket=upload.bucket, Key=upload.original_key(uuid.uuid4()), Body=b"isolation probe"
        )
    )


def test_read_access_cannot_write(env: Env, resolve: Resolve) -> None:
    read = resolve(env.tenant_a, StorageAccess.READ)
    api = s3(read.session(), read.region)
    assert api.list_objects_v2(Bucket=read.bucket, MaxKeys=1)["ResponseMetadata"]
    assert_denied(
        lambda: api.put_object(
            Bucket=read.bucket,
            Key=read.inbox_key(uuid.uuid4(), uuid.uuid4()),
            Body=b"isolation probe",
        )
    )
    vector_api = vectors(read.session(), read.region)
    assert_denied(
        lambda: vector_api.put_vectors(
            vectorBucketName=read.vector_bucket,
            indexName=read.vector_index,
            vectors=[{"key": "isolation-probe", "data": {"float32": [0.0]}}],
        )
    )
    assert_denied(
        lambda: faces(read.session(), read.region).describe_collection(
            CollectionId=read.face_collection
        )
    )


def test_a_tenant_session_cannot_start_another_session(env: Env, pair: Pair) -> None:
    api = sts(pair.own.session(), env.region)
    assert_denied(
        lambda: api.assume_role(
            RoleArn=env.tenant_role_arn,
            RoleSessionName="isolation-probe",
            Tags=[
                {"Key": TENANT_TAG, "Value": str(pair.other.tenant_id)},
                {"Key": BUCKET_TAG, "Value": pair.other.bucket},
                {"Key": VECTOR_BUCKET_TAG, "Value": pair.other.vector_bucket},
            ],
            SourceIdentity=pair.own.source_identity,
        )
    )


@pytest.mark.parametrize("target", ["own", "other"])
def test_a_tenant_session_cannot_change_key_tags(pair: Pair, target: str) -> None:
    """The tenant role's policy matches a key by its tenant_id tag."""
    api = kms(pair.own.session(), pair.own.region)
    key = pair.own.kms_key_arn if target == "own" else pair.other.kms_key_arn
    assert_denied(
        lambda: api.tag_resource(KeyId=key, Tags=[{"TagKey": PROBE_TAG, "TagValue": "x"}])
    )
    assert_denied(lambda: api.untag_resource(KeyId=key, TagKeys=[PROBE_TAG]))


def test_a_tenant_session_cannot_change_other_tags(env: Env, tenant: StorageContext) -> None:
    session = tenant.session()
    assert_denied(
        lambda: s3(session, tenant.region).put_bucket_tagging(
            Bucket=tenant.bucket, Tagging={"TagSet": [{"Key": PROBE_TAG, "Value": "x"}]}
        )
    )
    assert_denied(
        lambda: vectors(session, tenant.region).tag_resource(
            resourceArn=vector_bucket_arn(env, tenant), tags={PROBE_TAG: "x"}
        )
    )
    assert_denied(
        lambda: faces(session, tenant.region).tag_resource(
            ResourceArn=collection_arn(env, tenant), Tags={PROBE_TAG: "x"}
        )
    )


def test_a_tenant_session_cannot_invite_users(env: Env, tenant: StorageContext) -> None:
    api = lambda_(tenant.session(), env.region)
    assert_denied(
        lambda: api.invoke(
            FunctionName=env.invite_function, Payload=json.dumps({"operation": "probe"}).encode()
        )
    )


# --- 3. The runner's role: allowed by IAM, refused by the resources ---------------------------


def test_only_the_tss_can_assume_the_tenant_role(
    env: Env, runner: boto3.Session, tenant: StorageContext
) -> None:
    """The runner's policy allows this AssumeRole; the tenant role's trust policy does not."""
    api = sts(runner, env.region)
    assert_denied(
        lambda: api.assume_role(
            RoleArn=env.tenant_role_arn,
            RoleSessionName="isolation-probe",
            DurationSeconds=900,
            Tags=[
                {"Key": TENANT_TAG, "Value": str(tenant.tenant_id)},
                {"Key": BUCKET_TAG, "Value": tenant.bucket},
                {"Key": VECTOR_BUCKET_TAG, "Value": tenant.vector_bucket},
            ],
            SourceIdentity=f"{env.service}.probe",
        )
    )
    assert_denied(
        lambda: api.assume_role(RoleArn=env.tenant_role_arn, RoleSessionName="isolation-probe")
    )


def test_bucket_policy_refuses_the_runner(runner: boto3.Session, tenant: StorageContext) -> None:
    api = s3(runner, tenant.region)
    assert_denied(lambda: api.list_objects_v2(Bucket=tenant.bucket, MaxKeys=1))
    assert_denied(lambda: api.get_object(Bucket=tenant.bucket, Key=tenant.text_key(uuid.uuid4())))
    assert_denied(
        lambda: api.put_object(
            Bucket=tenant.bucket,
            Key=tenant.inbox_key(uuid.uuid4(), uuid.uuid4()),
            Body=b"isolation probe",
        )
    )
    assert_denied(lambda: api.get_bucket_policy(Bucket=tenant.bucket))
    assert_denied(
        lambda: api.put_bucket_tagging(
            Bucket=tenant.bucket, Tagging={"TagSet": [{"Key": PROBE_TAG, "Value": "x"}]}
        )
    )


def test_key_policy_refuses_the_runner(runner: boto3.Session, tenant: StorageContext) -> None:
    api = kms(runner, tenant.region)
    key = tenant.kms_key_arn
    assert_denied(lambda: api.generate_data_key(KeyId=key, KeySpec="AES_256"))
    assert_denied(lambda: api.encrypt(KeyId=key, Plaintext=b"isolation probe"))
    assert_denied(
        lambda: api.tag_resource(KeyId=key, Tags=[{"TagKey": PROBE_TAG, "TagValue": "x"}])
    )
    assert_denied(lambda: api.untag_resource(KeyId=key, TagKeys=[PROBE_TAG]))


def test_the_runner_cannot_read_tenant_vectors(
    runner: boto3.Session, tenant: StorageContext
) -> None:
    """The vector bucket's policy admits only the tenant role and the provisioning role."""
    api = vectors(runner, tenant.region)
    assert_denied(
        lambda: api.list_vectors(
            vectorBucketName=tenant.vector_bucket,
            indexName=tenant.vector_index,
            maxResults=1,
            returnData=True,
        )
    )


def test_the_runner_cannot_invite_users(env: Env, runner: boto3.Session) -> None:
    """Only the provisioning role may invoke the Cognito admin Lambda."""
    api = lambda_(runner, env.region)
    assert_denied(
        lambda: api.invoke(
            FunctionName=env.invite_function, Payload=json.dumps({"operation": "probe"}).encode()
        )
    )
