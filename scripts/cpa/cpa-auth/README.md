# Managed CPA Claude authentication

本页命令从工具包根目录执行：先在仓库根目录运行 `cd scripts/cpa`。实际配置和会话放在仓库外。

For daily self-service use, run `python3 cpa-tools/claude_auth.py` from the project root. It displays links in your terminal, sequences both grants, validates the account, backs up and binds quota, and enables the new account only after acceptance. GPT is not required. Setup, recovery and limitations are maintained in the [self-service SOP](../cpa-tools/README.md). The supervisors below are internal building blocks and advanced/manual entry points.

`managed_setup.py` runs the locally installed Claude Code `setup-token` command in an isolated configuration directory. It captures terminal output privately, checks the automatic local callback listener, then writes a protected authorization URL for the agent to give the user. The user opens it in their own browser or private window, chooses the account and authorizes. It does not open a browser by default. No token needs to be copied or submitted by the user.

Create a private deployment JSON outside this shareable source directory, mode `600`, with these fields:

```json
{
  "ssh": ["ssh", "-i", "/PRIVATE/SSH_KEY", "-p", "SSH_PORT", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", "admin@VPS_HOST"],
  "observer_config": "/etc/cpa-observer/config.json",
  "credential_name": "claude-upstream-b-setup.json",
  "quota_credential_name": "claude-upstream-b-quota.json",
  "expected_email": "ACCOUNT_EMAIL_CONFIRMED_BY_USER",
  "model": "claude-sonnet-5-5"
}
```

Replace placeholders using the authorized deployment. The SSH user must have permission to read the observer configuration, manage CPA credentials and run an isolated CPA process. This implementation locates the native binary through `cliproxyapi.service`; adapt that unit name when the installation differs. The remote host needs Python 3, systemd and the deployed native CPA v8. The local host needs Python 3 and Claude Code. Browser-opening utilities are required only for the explicit `--open-browser` opt-in.

The agent starts a persistent execution session:

```bash
python3 -u cpa-auth/managed_setup.py \
  --deployment-config /PRIVATE/deployment.json \
  --state-dir /PRIVATE/login-session
```

Use a new credential filename for another account. Existing names are rejected. For renewal, follow the backup and replacement procedure in section 11.1 of the accompanying deployment guide; this command does not overwrite an existing credential.

New credentials are imported disabled. Validation runs the normal Claude executor in an isolated loopback CPA with only that credential, waits for model registration and performs a small real generation. Model success alone does not enable the credential or complete acceptance. Account identity, quota windows and model generation must all pass. Missing account data is not guessed from an email domain or a synthetic UUID.

If the standard setup token lacks `user:profile`, preserve it and start the companion OAuth grant:

```bash
python3 -u cpa-auth/managed_quota.py \
  --deployment-config /PRIVATE/deployment.json \
  --state-dir /PRIVATE/quota-login-session
```

This command establishes an SSH callback forward on port 54545, checks it before providing the link, and stages a separate full OAuth credential for the same expected account. The user opens the link in their chosen private window and authorizes. CPA's login receiver may show its own success page. Its login process has a five-minute callback timeout; a stale link must be replaced. The helper checks the returned profile email against `expected_email` and tests actual usage windows. It keeps this credential disabled for model scheduling and does not overwrite the inference token.

After `ready_for_binding: true`, the agent backs up the service configuration, credential files and SQLite database, adds the `quota_bindings` mapping documented in the observer README, verifies the quota source identity and binds its returned account metadata to the inference credential. Preserve the inference access token, its setup-token marker and empty refresh field. The user confirms the same account was chosen in both grants; record this verification method instead of claiming the opaque inference token independently disclosed an email. Then enable the model credential, restart only the observer and verify public `/v1/quota` has complete coverage. A failed quota/identity check leaves onboarding pending.

The observer queries the disabled quota source through CPA's local management API. On 401 it requests a targeted refresh of that source once, discarding the credential-bearing refresh response without reading it, then retries quota. A missing scope requires authorization; a 429 honors the upstream retry delay. Never use an old credential from another account as a substitute.

Only sanitized phases and HTTP statuses are printed. `status.json` records progress. `authorization-url.private` is supplied only to the user while its receiver is live and is removed when the session ends; do not publish it in general documents. The setup flow also retains `authorization-metadata.private.json` with requested scopes and their source. CPA's full quota credential preserves returned identity, organization, expiry and refresh fields. Actual inference expiry is marked unknown when the CLI did not expose it; never manufacture a date. Temporary configuration and token files are removed after import, while installed credentials remain on the VPS. If import fails after capture, the protected credential file is retained for recovery. Do not publish deployment JSON, session directories, credentials or raw CLI output.

The setup-token callback runs on the browser computer; the companion quota flow uses a temporary SSH callback forward to VPS CPA. Daily API traffic uses the HTTPS gateway and needs neither login process nor callback tunnel. `--open-browser` is an explicit opt-in only when the user asks; default to delivering the link. Page branding identifies the callback renderer, not the token scope, account domain, expiry or quota capability.
