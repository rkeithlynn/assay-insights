"""Names shared by the API and the Cognito pre-token trigger. No imports: the trigger image
ships this file on its own.

Cognito puts custom attributes in the ID token only, so the tenant pool's pre-token trigger
copies the tenant ID into the access token, together with whether the user has TOTP MFA.
"""

# Access-token claims added by the pre-token trigger (tenant pool only).
TENANT_ID_CLAIM = "tenant_id"
MFA_CLAIM = "mfa"

# The tenant pool's custom attribute. Immutable: set once, when the user is created.
TENANT_ID_ATTRIBUTE = "custom:tenant_id"

TENANT_GROUPS = ("tenant-admin", "tenant-user")
PLATFORM_GROUPS = ("master-admin", "master-user")
