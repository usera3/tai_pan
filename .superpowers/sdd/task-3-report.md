# Task 3 Report

Status: DONE_WITH_CONCERNS

## Commit

- `38af72d feat: add invitation-based cloud authentication`
- Baseline: `2c13d563`

## RED

Required Task 3 command:

```text
$ .venv/bin/python -m pytest -q tests/cloud/test_auth_routes.py tests/cloud/test_mode_selection.py
ERROR tests/cloud/test_auth_routes.py
ModuleNotFoundError: No module named 'app.cloud.app'
1 error in 4.23s
```

Atomic repository contract:

```text
$ .venv/bin/python -m pytest -q tests/cloud/test_repository.py -k 'invited_user_creation'
FF
AttributeError: 'CloudRepository' object has no attribute 'register_user_with_invitation'
2 failed, 15 deselected in 3.65s
```

## GREEN

Required Task 3 tests:

```text
$ .venv/bin/python -m pytest -q tests/cloud/test_auth_routes.py tests/cloud/test_mode_selection.py
......................
22 passed in 48.13s
```

Minimal local and repository regression requested after interruption:

```text
$ .venv/bin/python -m pytest -q tests/test_routes.py tests/cloud/test_repository.py -k 'invited_user_creation or test_routes'
............
12 passed, 15 deselected in 5.58s
```

Whitespace verification:

```text
$ git diff --check
# exit 0, no output
```

The brief's combined Task 3 plus local-routes command was interrupted before it
produced a result. The complete pytest suite was not run because the user then
explicitly required termination of long-running commands and prohibited a full
suite run.

## Modified Files

- `app/cloud/app.py`
- `app/cloud/dependencies.py`
- `app/cloud/repository.py`
- `app/cloud/routes/__init__.py`
- `app/cloud/routes/auth.py`
- `app/cloud/schemas.py`
- `app/main.py`
- `tests/cloud/test_auth_routes.py`
- `tests/cloud/test_mode_selection.py`
- `tests/cloud/test_repository.py`

`repository.py` required minimal additions because its previous public API
could only consume an invitation for an already-created user. The new
transactional method creates the user and consumes the invitation under one
`BEGIN IMMEDIATE` transaction. Tests cover duplicate-username rollback and
concurrent single-use behavior. The repository also gained the password/session
transaction, login timestamp update, and SQLite registration-attempt count
needed by the authentication routes.

## Self-review

- Session and CSRF plaintext are generated independently, placed only in the
  Secure session cookie or authenticated JSON as applicable, and stored as
  hashes in SQLite. Secret-bearing schema fields are excluded from repr.
- Login errors are identical for unknown users, wrong passwords, and disabled
  users. Login and registration limits use SQLite `auth_attempts` records.
- `verify_csrf` checks both exact `PUBLIC_ORIGIN` and the current session's CSRF
  hash. Logout and password change both require it.
- `current_user` rejects disabled users; `active_user` blocks users requiring a
  password change; `admin_user` builds on `active_user`.
- Password changes revoke all sessions transactionally and issue a new session.
- Default and explicit local mode preserve the local factory. Cloud imports fail
  closed on incomplete configuration.
- No unrelated worktree changes were reverted.

## Concerns

- Full pytest evidence is absent by explicit stop instruction. Only the exact
  Task 3 suite and focused local/repository regressions have fresh passing
  evidence.
- The interrupted combined command has no final result, although its two parts
  passed separately in the runs recorded above.
- Rate-limit identity uses `request.client.host`; production correctness depends
  on the deployment's trusted proxy-header configuration.

## Independent Security Review Remediation (2026-07-17)

Status: DONE_WITH_CONCERNS

### RED

Focused security regressions were added before production changes:

```text
$ .venv/bin/python -m pytest -q tests/cloud/test_auth_routes.py tests/cloud/test_repository.py -k "registration_rolls_back_user or password_change_rolls_back or counts_invalid_json or rate_limit_claim_fails_closed or eleven_concurrent or rate_limit_covers or six_concurrent or successful_login_response_survives or expired_session or ordinary_user or password_change_revokes_other or verified_old_password"
FFF..FFF...FFF
9 failed, 5 passed, 31 deselected in 25.97s
```

The failures were the expected missing protections: validation failures bypassed
registration limits, registration claim failures did not fail closed, all 11
concurrent registration submissions were accepted, both account and IP login
races recorded 6 failures, audit failure replaced a successful response, stale
verified credentials won the password-change race, and repository registration
and password-change APIs did not include session insertion in their transactions.

### GREEN

The exact focused command above was rerun after the minimal implementation:

```text
..............
14 passed, 31 deselected in 28.15s
```

Per the controller's stop instruction, no additional pytest command was started.

### Commit

- Commit message: `fix: harden cloud authentication transactions`
- Parent: `38af72d62b3cd973ed7aa090745980134e85acc4`
- This report is included in the remediation commit; the resulting hash is
  reported to the controller after commit creation.

### Self-review

- Registration pre-generates session and CSRF tokens, then invitation
  validation/consumption, user creation, and hashed session insertion commit in
  one `BEGIN IMMEDIATE` transaction. Injected session insertion failure leaves
  neither a user nor a consumed invitation.
- Login session issuance rechecks both `status = 'active'` and the exact verified
  password hash inside a write transaction. Password change similarly guards the
  verified hash and atomically updates the password, clears the forced-change
  flag, revokes old sessions, and inserts the replacement session.
- Login failure and registration submission slots are claimed under SQLite write
  transactions. Concurrent tests independently cover account and IP login limits
  and the 10-submission registration limit.
- Registration claims run in middleware before body parsing, use only
  `request.client.host`, return fixed 422/429/503 bodies, and do not trust
  `X-Forwarded-For`.
- Successful-login audit failure is best-effort and cannot replace the primary
  response. Audit calls contain username, client address, outcome, and timestamp;
  passwords, invitation codes, session tokens, and CSRF tokens are excluded.
- Ordinary-user admin rejection, expired-session rejection, other-session
  revocation on password change, secret redaction, and synchronized stale-login
  rejection are covered. The auth-route test file ends with exactly one newline.
- No Task 4 administrator workflow or deployment file was changed, and no other
  worker's changes were reverted.

### Concerns

- The controller will run the complete pytest suite. The broader requested cloud
  suites, local `tests/test_routes.py`, and full pytest do not have fresh evidence
  in this remediation run because the controller explicitly stopped expansion.
- Trusted proxy handling remains deferred to Task 8; this change intentionally
  continues to use `request.client.host`.
