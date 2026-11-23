# Tenant pool pre-token-generation trigger (Lambda, linux/amd64). Built by Pulumi
# (infra/assay_infra/auth.py) with the repository root as the build context.
# It needs only boto3, which the digest-pinned Lambda base image provides, so nothing is
# installed: the image is the base plus two source files.

FROM public.ecr.aws/lambda/python:3.14@sha256:497df5d229e6d5c54279a6a48e7f597ff386bd02a9b3b37e788bb881108fc081
# Lambda runs code as a non-root user. The directory is created first: COPY --chmod would
# otherwise give the directory it creates the files' mode, leaving it untraversable.
RUN install -d -m 0755 ${LAMBDA_TASK_ROOT}/assay_api
COPY --chmod=0444 api/assay_api/__init__.py api/assay_api/claims.py api/assay_api/cognito_pre_token.py ${LAMBDA_TASK_ROOT}/assay_api/
CMD ["assay_api.cognito_pre_token.handler"]
