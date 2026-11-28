"""The ingestion pipeline (BUILD_SPEC.md section 8).

Build step 6, first half: the Lambda functions for steps 2 to 4 and the batch housekeeping.

- `scan`: records each GuardDuty Malware Protection scan result (step 2).
- `starter`: turns each queued job into one Step Functions execution (step 3).
- `steps`: the state machine's Lambda steps: fingerprint (step 4) and the failure handler.
- `sweeper`: closes batches left open for 24 hours, times out scans that never arrived, and
  queues again any eligible document that was not started.

Every handler works from database rows, scan events and job messages only, never from a
listing of the bucket, and refuses the isolation tests' reserved document ID.
"""
