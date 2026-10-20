# Upgrading garminconnect

garmin-reader adapts parts of garminconnect's authentication implementation because
the library can swallow token-refresh failures and retry an expired access
token. Without the adapter, a temporary refresh outage can look like a
logout. Garmin's own token lock serializes refreshes and token-file writes;
the adapter reuses it to protect its temporary HTTP hook and recorded error.

The implementation and library tests were checked against **garminconnect
0.3.19**. [pyproject.toml](../pyproject.toml) currently permits `>=0.3.19,<0.4`
and requires Python 3.12+. The upper bound limits upgrades; it does not
guarantee that private internals remain compatible within the 0.3 series.
Review these dependencies when changing the installed version, including
when rebuilding a container that resolves a newer permitted release.

## 0.3.19 audit (2026-10-16)

[garminconnect 0.3.19](https://pypi.org/project/garminconnect/0.3.19/) was
compared with 0.3.17 source-to-source and tested in a separate environment
and then the project's. It still requires Python 3.12+ (the Python 3.13
Docker image satisfies it) and `curl_cffi`, `requests` and `ua-generator`;
it adds `tzdata` on Windows only. This is now the minimum supported release.

The package was restructured: `garminconnect/client.py` became the
`client/` package (the `Client` class is assembled from login, session,
token-store and transport mixins) and `Garmin`'s getters moved into an
`api/` package of mixins. Every private member garmin-reader depends on still
exists on the same objects, and replacing it on the instance still
intercepts it:

| Dependency | Finding in 0.3.19 | Decision |
|---|---|---|
| `_refresh_session`, `_token_lock` | Same logic, now in `client/session.py`; token writes after a refresh go through a new `_persist_tokens()`, which dumps to `_tokenstore_path` and suppresses errors exactly as before. The lock is still a `threading.RLock`. | No change. |
| Refresh POST through `_http_post` | `_refresh_di_token()` now calls a new `_di_token_post()`, which still sends through `self._http_post` synchronously. Refresh failures are still swallowed and the old token retried, so garmin-reader's interception is still needed. | No change; the real-client refresh tests pass. |
| Credential login, MFA, `_mfa_session`, `_mfa_flow`, `_mfa_pending`, `_complete_mfa` | Unchanged logic, now in `client/sso.py`. `_mfa_session` and `_mfa_flow` now exist from construction rather than from the first login. Login logs a warning if `curl_cffi` is missing, which can't happen: it is a required dependency. | No change. |
| Token persistence (`dump`, `load`) | Unchanged, now in `client/tokens.py`. | No change. |
| API path checks in `_run_request` | Moved to a new `_resolve_url()`: the same traversal, query and fragment checks, and it now also accepts a full `https://` URL on a Garmin host. Errors now carry the HTTP response as `error.response`; their message is unchanged. | No change: remotely garmin-reader sends no raw paths at all now (below). |
| Getter error decorator | Still wraps connection errors with `raise ... from e`, but now retries only 5xx responses or errors with a network failure in their cause chain (before: anything without a status). | garmin-reader's refresh-outage marker is still reached through the cause chain; outage and 404-isolation tests pass. |
| Range getters | `get_hrv_data_range` and `get_sleep_daily` are unchanged. A new `get_training_readiness_range(start, end)` requests exactly the endpoint garmin-reader used by raw path. | `get_garmin_summary` now uses it, so remote calls never send a raw path: the raw-path exception (`summary.RANGE_PATHS`) is gone and `training_readiness_range` is on `ALLOWED`. |

No getters were removed; 144 were added (256 in all). The remote allowlist
excludes all of them except `get_training_readiness_range`. Getters on the
remote allowlist whose signatures changed, all with optional additions:
`get_endurance_score` (`aggregation`, `"weekly"` by default, as before for
ranges), `get_personal_record` (`include_history`, `months`),
`get_nutrition_daily_food_log` (`include_goals`, `include_nutrition`,
`meal_id`) and `get_workouts` (filtering and ordering options). Their
single-day and missing-end behavior is unchanged.

Validation:

- 0.3.19: full offline suite, **1073 passed, 1 skipped**; `pip check` passed.
- As before, no live account or production token store was used; live
  login/MFA still needs the deployment check below.

## 0.3.17 audit (2026-10-11)

[garminconnect 0.3.17](https://pypi.org/project/garminconnect/0.3.17/)
was installed and tested in the project's virtual environment. It requires
Python 3.12+, which the Python 3.13 Docker image satisfies. This is the
minimum supported release; garmin-reader uses its token lock.

| Workaround or assumption | Finding in 0.3.17 | Decision |
|---|---|---|
| Serialize refresh and token writes | The client now owns a `threading.RLock`: `_run_request`, `_refresh_session`, and `_refresh_di_token` use it. `dump()` snapshots tokens under the lock and atomically replaces the file with an owner-only temporary file. | Removed garmin-reader's separate lock and raised the minimum to 0.3.17. The error adapter uses `client._token_lock` around HTTP-hook installation, refresh, failure checking, and hook restoration. This also protects direct calls to the wrapped refresh method. |
| Observe refresh HTTP errors | `_refresh_di_token()` still treats every non-success response as an authentication error; `_refresh_session()` still catches every exception. Unwrapped refresh 429, 408, 500, 503, timeout, and connection failures all retry the old access token and ultimately produce an authentication error. | Keep `_http_post` interception and the post-refresh re-raise. |
| Prevent credential fallback during a cache refresh outage | `Garmin.login()` still catches proactive-refresh errors in its cache-loading block. With just our refresh interceptor, it requests credentials or attempts password login instead of preserving the outage error. | Keep the temporary `client.login` guard and original-error restoration in `auth.login()`. |
| Preserve exception causes | The getter decorator still wraps connection errors with `raise ... from e`. Its new `GarminConnectNotFoundError` remains a subclass of `GarminConnectConnectionError`. | Keep the refresh marker and cause traversal so outages fail the whole call while optional endpoint errors stay isolated. |
| Credential login, MFA, profile verification, and persistence | `client.login(..., return_on_mfa=True)` still bypasses token-store loading and returns `("needs_mfa", None)`. MFA state remains on the client; `resume_login(None, code)` still works at the implementation level. The DI token fields, `dump`/`load` format, HTTP boundary, and profile lookup path remain compatible. | Retain these dependencies. Upstream's token-validity check does not establish that the session belongs to the user's recorded Garmin profile. |

The raw-library removal checks in `tests/test_garmin_library.py` deliberately
assert the current upstream failure behavior, alongside tests for garmin-reader's
corrected behavior. If those removal checks fail after an upgrade, inspect
whether upstream has fixed the problem before updating the adapter.

The real-client concurrency test runs both with and without the adapter,
verifying four ordered token rotations, restoration of the HTTP hook, and
reloading the final token pair.

No getters were removed. Eighteen getters were added; the remote allowlist
excludes all of them. `get_activities()` gained an optional `activitysubtype`
argument. Two local-only signatures also changed: `get_goals()` now defaults
to `start=1`, and `get_golf_shot_data()` defaults `hole_numbers` to `None`.
The audited remote getter changes preserve garmin-reader's call signatures. They
include safer display-name encoding, bounded activity pagination, and
conversion of string `lastSevenDaysAvgSpO2` values to floats. Most responses
remain server-provided data; a source audit cannot establish future response
schemas or guarantee that new private fields will never appear.

Validation:

- 0.3.17: full offline suite, **546 passed, 1 skipped**; `pip check` passed.
- Login/MFA internals were inspected, but no live account or production
  token store was used. Live login/MFA and Garmin response schemas still
  require the deployment check below.

## Production dependencies

The underscore-prefixed methods below are private library APIs. Other rows
describe lower-level client attributes or behavior that garmin-reader also relies on;
they should not be mistaken for a guarantee of compatibility.

| Dependency | Where garmin-reader uses it | What to check in the candidate version |
|---|---|---|
| `api.client._refresh_session` | [`auth._guard_token_refresh()`](../src/garmin_reader/auth.py) saves the bound method and replaces it with an error-preserving wrapper. | Refreshes must still pass through this method, and replacing it on the instance must still intercept them. A missing method must fail guard installation rather than silently disable it. |
| `api.client._token_lock` | Guard installation captures this upstream lock and requires a `threading.RLock`, failing immediately if it is missing or has an incompatible type. The adapter reuses it across HTTP-hook installation, refresh, error checking, and restoration. | It must remain the same reentrant lock shared with the client's refresh and token mutation paths. Nested acquisition is necessary: callers and the original refresh method also acquire it. An alternative implementation needs explicit review before relaxing the type check. Keep the real-client concurrency checks both with and without the adapter; their fixture checks the lock type before starting threads. |
| `api.client._http_post` | The same wrapper temporarily replaces this method during a DI token refresh and restores it in `finally`. | `_refresh_session()` must still perform the refresh POST through this method, synchronously while the wrapper is installed. Responses must expose `status_code`. Check that throttling, server errors, and transport exceptions are still observed before the library can swallow them. |
| `api.client.di_token` and `di_refresh_token` | [`auth.py`](../src/garmin_reader/auth.py) uses `di_token` to select the DI refresh guard. [`GarminGateway.save()`](../src/garmin_reader/login_page.py) requires both tokens before saving. | The fields must still identify an access token and a reusable refresh token. Check the token format, rotation behavior, and whether a successful login can produce a different session type. The DI-specific error guard does not intercept legacy non-DI refresh HTTP calls. |
| `api.client.login` called by `Garmin.login()` | [`auth.login()`](../src/garmin_reader/auth.py) temporarily replaces the client method while loading the cached session. | A failed cache refresh may cause `Garmin.login()` to attempt credential login. The guard must stop that fallback for temporary failures and retain the original refresh error, including when credentials are configured. The original client method must be restored on success and failure. |
| Direct credential login and MFA continuation | [`GarminGateway.start()` and `finish()`](../src/garmin_reader/login_page.py) call `api.client.login(..., return_on_mfa=True)` and `api.resume_login(None, code)`. | Credential login must actually validate the submitted credentials rather than load `GARMINTOKENS`. Check the returned status (`"needs_mfa"`), where MFA continuation state lives, and whether passing `None` remains valid. |
| `api.client._mfa_session` and `_mfa_flow` | [`GarminGateway.finish()`](../src/garmin_reader/login_page.py) temporarily wraps the MFA session to see whether any verification POST got an answer from Garmin, because `_complete_mfa` raises the same authentication error for a wrong code as for timeouts, 5xx and bot-check pages (and the widget flow lets transport errors escape raw). No answer raises `GarminVerificationUnavailable`. | Both flows must still send the code through `_mfa_session.post`, and restore nothing else on the session. Portal and mobile must still answer in JSON, the widget in HTML; `_mfa_flow` must still say which. Doubtful responses count as an answer (a counted wrong code), never as an outage. The real-client tests cover timeouts, 5xx (including JSON bodies), bot-check pages, 429s and wrong codes in both flows. |
| `api.client._mfa_pending` | [`GarminGateway.mfa_pending()`](../src/garmin_reader/login_page.py) reads it after a failed code to decide whether `/login` shows the code form again or makes the user start over. | A wrong or rate-limited code must leave it `True` (0.3.17 and later keep the step open) and success must clear it; the real-client tests check both. If it disappears, `mfa_pending()` says no and the user starts over: safe, but the retry is lost. |
| Token persistence | [`GarminGateway.save()`](../src/garmin_reader/login_page.py) calls `api.client.dump(path)`; [`auth.login()`](../src/garmin_reader/auth.py) reloads that store through `Garmin.login(path)`. | Saved tokens must reload without credentials and remain renewable. Inspect changes to the file format, save timing, and file permissions. |
| Range getters `get_hrv_data_range(start, end)`, `get_sleep_daily(start, end)` and `get_training_readiness_range(start, end)` | [`summary._hrv()`, `_sleep()` and `_readiness()`](../src/garmin_reader/summary.py); all three are on `ALLOWED`. | `get_hrv_data_range` must still return `{"hrvSummaries": [...]}` from one request. `get_sleep_daily` must still split ranges into Garmin's 28-day chunks and return the merged, de-duplicated `individualStats` rows. `tests/test_garmin_library.py` checks both with the real client. `get_training_readiness_range` must still return the list of readiness entries (several per day possible) from one request. Because range getters can make many requests (one per chunk or page), `call_garmin_endpoint` refuses date ranges over 366 days before calling any getter with a `start`/`end`, `startdate`/`enddate` or `fromdate`/`todate` pair; check new getters use one of those names. A missing end counts as today unless the getter is in `endpoints.MISSING_END_IS_START` (those set end = start); the real-client tests check that list, and a new getter with an optional end needs a decision. |
| Exception translation and chaining | [`auth.is_refresh_unavailable()`](../src/garmin_reader/auth.py), [`endpoints.py`](../src/garmin_reader/endpoints.py), [`summary.py`](../src/garmin_reader/summary.py), and [`mcp_server.py`](../src/garmin_reader/mcp_server.py). | Library decorators may wrap connection errors. The adapter's `GarminRefreshUnavailable` marker must remain reachable through `__cause__` or `__context__`. A refresh outage must fail the whole tool call with retry guidance; an unsupported optional endpoint's 404 must still allow other metrics to return. |

`garmin_reader_refresh_error` and `garmin_reader_refresh_guarded` are attributes added by garmin-reader, not
Garmin APIs. Check that an upgrade still permits these instance/function
attributes and does not introduce conflicting names.

Profile verification also uses `api.client.connectapi()` with the raw Garmin
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

1. In a disposable environment, install the candidate version with garmin-reader and
   pytest. Record the Python and Garmin versions and inspect the candidate's
   `Garmin.login`, `Garmin.resume_login`, client `_refresh_session`,
   `_refresh_di_token`, `_http_post`, `_run_request`, `dump`, and `load`, plus
   its getter exception decorator. Follow renamed or replaced methods to
   their new implementations. Verify every production dependency above.
2. Run the focused checks using that environment's Python:

   ```console
   python -m pytest tests/test_garmin_library.py tests/test_auth.py tests/test_login_page.py tests/test_users.py -q
   python -m pytest tests/test_summary.py tests/test_endpoints.py tests/test_exposure.py -q
   ```

   In particular, retain these distinctions:

   - A successful refresh uses the new access token and returns the expected data.
   - Rejected credentials or a revoked refresh token produce sign-in guidance.
   - API throttling and refresh throttling produce retry guidance.
   - Refresh 408/5xx responses, timeouts, and connection failures retain the
     session and tokens. Recovery works without a password login.
   - Cached-login refresh outages cannot trigger credential fallback, with
     or without credentials configured.
   - Parallel refreshes do not overlap, profile checks precede health requests,
     and an optional endpoint failure remains isolated from other metrics.
3. Review getter names and signatures, `ALLOWED`, and response fields
   used by the privacy filter and summaries. New `get_*` methods stay
   blocked until added to the allowlist on purpose.
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
