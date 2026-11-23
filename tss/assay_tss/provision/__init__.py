"""Tenant provisioning: the task that creates a tenant's storage (BUILD_SPEC.md section 6).

This is `TenantStorageService.provision`. It runs as a Fargate task started by the
provisioning state machine, never inside the TSS Lambda.
"""
