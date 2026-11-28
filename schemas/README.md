# schemas

Pydantic v2 models and plain-code validators shared by api/ and workers/ (package
`assay_schemas`).

| Module | Holds |
| --- | --- |
| `companion.py` | The companion file, version 1 (BUILD_SPEC.md section 8): strict parsing and validation with plain code, never a model. Errors name keys, never values. |
| `pipeline.py` | `IngestJob`, the ingest queue's message (IDs only), and `checksum_b64`, S3's form of a SHA-256. |
