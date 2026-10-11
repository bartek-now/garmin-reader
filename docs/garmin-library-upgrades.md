# Upgrading garminconnect

kcal adapts parts of garminconnect's authentication implementation because
the library can swallow token-refresh failures and retry an expired access
token. Without the adapter, a temporary refresh outage can look like a
logout. The adapter also serializes refreshes to avoid concurrent token
rotation and token-file writes.

The implementation and library tests were checked against **garminconnect
0.3.6**. [pyproject.toml](../pyproject.toml) currently permits `>=0.3.6,<0.4`
and requires Python 3.12+. The upper bound limits upgrades; it does not
guarantee that private internals remain compatible within the 0.3 series.
Review these dependencies when changing the installed version, including
when rebuilding a container that resolves a newer permitted release.

## Production dependencies

The underscore-prefixed methods below are private library APIs. Other rows
describe lower-level client attributes or behavior that kcal also relies on;
they should not be mistaken for a guarantee of compatibility.

| Dependency | Where kcal uses it | What to check in the candidate version |
|---|---|---|
| `api.client._refresh_session` | [`auth._serialize_token_refresh()`](../src/kcal/auth.py) saves the bound method and replaces it with a locked wrapper. | Refreshes must still pass through this method, and replacing it on the instance must still intercept them. If it disappears, the production wrapper currently returns without installing the guard; the real-client tests must detect that change. |
| `api.client._http_post` | The same wrapper temporarily replaces this method during a DI token refresh and restores it in `finally`. | `_refresh_session()` must still perform the refresh POST through this method, synchronously while the wrapper is installed. Responses must expose `status_code`. Check that throttling, server errors, and transport exceptions are still observed before the library can swallow them. |
| `api.client.di_token` and `di_refresh_token` | [`auth.py`](../src/kcal/auth.py) uses `di_token` to select the DI refresh guard. [`GarminGateway.save()`](../src/kcal/login_page.py) requires both tokens before saving. | The fields must still identify an access token and a reusable refresh token. Check the token format, rotation behavior, and whether a successful login can produce a different session type. The DI-specific error guard does not intercept legacy non-DI refresh HTTP calls. |
| `api.client.login` called by `Garmin.login()` | [`auth.login()`](../src/kcal/auth.py) temporarily replaces the client method while loading the cached session. | A failed cache refresh may cause `Garmin.login()` to attempt credential login. The guard must stop that fallback for temporary failures and retain the original refresh error, including when credentials are configured. The original client method must be restored on success and failure. |
| Direct credential login and MFA continuation | [`GarminGateway.start()` and `finish()`](../src/kcal/login_page.py) call `api.client.login(..., return_on_mfa=True)` and `api.resume_login(None, code)`. | Credential login must actually validate the submitted credentials rather than load `GARMINTOKENS`. Check the returned status (`"needs_mfa"`), where MFA continuation state lives, and whether passing `None` remains valid. |
| Token persistence | [`GarminGateway.save()`](../src/kcal/login_page.py) calls `api.client.dump(path)`; [`auth.login()`](../src/kcal/auth.py) reloads that store through `Garmin.login(path)`. | Saved tokens must reload without credentials and remain renewable. Inspect changes to the file format, save timing, and file permissions. |
| Exception translation and chaining | [`auth.is_refresh_unavailable()`](../src/kcal/auth.py), [`endpoints.py`](../src/kcal/endpoints.py), [`summary.py`](../src/kcal/summary.py), and [`mcp_server.py`](../src/kcal/mcp_server.py). | Library decorators may wrap connection errors. The adapter's `GarminRefreshUnavailable` marker must remain reachable through `__cause__` or `__context__`. A refresh outage must fail the whole tool call with retry guidance; an unsupported optional endpoint's 404 must still allow other metrics to return. |

`kcal_refresh_error` and `kcal_serialized` are attributes added by kcal, not
Garmin APIs. Check that an upgrade still permits these instance/function
attributes and does not introduce conflicting names.

Owner verification also uses `api.client.connectapi()` with the raw Garmin
path `/userprofile-service/socialProfile`, expecting a numeric `profileId`.
This depends on Garmin's response schema as well as the Python client. A
profile lookup failure must block health-data access without marking the
session as verified; temporary failures must not discard the session.

## Dependencies used only by tests

[`tests/test_garmin_library.py`](../tests/test_garmin_library.py) uses a real
`Garmin` object and replaces its HTTP boundary. Its fixtures additionally
depend on:

| Test dependency | Purpose |
|---|---|
| `_api_session.request(method, url, headers=..., **kwargs)` | Supplies synthetic API responses and records the bearer tokens sent. |
| `_http_post` | Supplies refresh responses or transport errors and records the refresh tokens used. |
| `_tokenstore_path` | Disables incidental token-file writes in ordinary fixtures. Cache-loading tests instead use a temporary token store. |
| `_token_expires_soon()` | Forces proactive refresh during the real library's cached-login path. |
| `di_token`, `di_refresh_token`, `di_client_id` | Seeds a synthetic authenticated session without contacting Garmin. |

Keep the `stub()` existence checks: a renamed hook should fail the test,
not silently add an attribute that the library never uses. Keep the network
blocks and the exact request, token, and result assertions. Do not make an
upgrade pass by accepting either logout or throttling, skipping a missing
hook, or removing the assertions that prove the fake HTTP layer was reached.

## Upgrade checklist

1. In a disposable environment, install the candidate version with kcal and
   pytest. Record the Python and Garmin versions and inspect the candidate's
   `Garmin.login`, `Garmin.resume_login`, client `_refresh_session`,
   `_refresh_di_token`, `_http_post`, `_run_request`, `dump`, and `load`, plus
   its getter exception decorator. Follow renamed or replaced methods to
   their new implementations. Verify every production dependency above.
2. Run the focused checks using that environment's Python:

   ```console
   python -m pytest tests/test_garmin_library.py tests/test_auth.py tests/test_login_page.py tests/test_owner_binding.py -q
   python -m pytest tests/test_summary.py tests/test_endpoints.py tests/test_remote_exposure.py -q
   ```

   In particular, retain these distinctions:

   - A successful refresh uses the new access token and returns the expected data.
   - Rejected credentials or a revoked refresh token produce sign-in guidance.
   - API throttling and refresh throttling produce retry guidance.
   - Refresh 408/5xx responses, timeouts, and connection failures retain the
     session and tokens. Recovery works without a password login.
   - Cached-login refresh outages cannot trigger credential fallback, with
     or without credentials configured.
   - Parallel refreshes do not overlap, owner checks precede health requests,
     and an optional endpoint failure remains isolated from other metrics.
3. Review getter names and signatures, `REMOTE_ALLOWED`, and response fields
   used by the privacy filter and summaries. New `get_*` methods become
   discoverable locally; remote exposure remains an explicit allowlist.
   New free-text, identity, or location fields need explicit review even if
   existing synthetic response tests still pass.
4. Run the full suite with `python -m pytest -q`. Test the declared minimum
   Garmin version as well if retaining it in the supported range. Check the
   candidate's Python requirement against `pyproject.toml` and the Dockerfile.
5. Before deployment, check credential login, MFA, saving/reloading tokens,
   and a read-only tool call with an authorized test account and a separate
   token store. The login-page tests use fakes; the real-client tests fake
   HTTP and do not establish that Garmin's live login or MFA service works.
   Do not deliberately provoke live rate limits or reuse production tokens.
6. Update this document's verified version, dependency bounds, and any
   affected tests together. If upstream now propagates refresh failures or
   provides a supported hook, remove obsolete interception while retaining
   the behavior tests. Update the README's version requirement if it changes.
