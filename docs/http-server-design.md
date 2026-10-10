# Remote (HTTP) MCP server — design

Status: agreed in discussion 2026-10-09. Built so far: steps 1-4 (HTTP
transport, settings, remote exposure rules, OAuth authorization server,
Garmin login page). HTTP mode always requires OAuth; there is no
unauthenticated mode. Next: connecting the real clients (step 5). See
"Build order".

## Goal

Let claude.ai (web and mobile) and ChatGPT use the kcal MCP tools, not just
local clients over stdio, with authentication that only the owner of the
Garmin account can pass.

## Decisions

| Topic | Decision |
|---|---|
| Transport | Streamable HTTP without MCP sessions, behind a flag. stdio stays the default. |
| Hosting | Owner's PC behind a Cloudflare **quick tunnel** for now; code written so a move to an always-on host is a config change. |
| MCP auth | OAuth 2.1 via the MCP SDK's built-in authorization server (`mcp.server.auth`), with dynamic client registration. |
| Who may log in | Whoever completes a **Garmin login (with MFA) as the configured owner** on our `/login` page (reached via `/authorize`). No separate identity provider yet. |
| Later | A Google (or GitHub) sign-in step in front of the Garmin form, once the hostname is stable. Optionally, URL-mode elicitation as a nicer way to open `/login`. |
| Garmin expiry | Tool error that tells the owner exactly what to do, with a link to `/login`; no reconnecting the connector. This is the intended design, not a stopgap. |
| Exposure | Remotely, `call_garmin_endpoint` is limited to an **allowlist** of health/fitness endpoints, with location fields stripped. stdio is unrestricted. |

## 1. Transport

- `kcal-mcp` keeps running over stdio. `kcal-mcp --http` serves Streamable
  HTTP at `/mcp` on `127.0.0.1:<port>`.
- No MCP sessions: each request stands alone, with no `Mcp-Session-Id` or
  per-session state in memory. Server restarts (reboots, code reloads) and a
  later move to a host therefore don't invalidate client sessions. This is
  only about the MCP transport; OAuth and Garmin tokens persist on disk
  (sections 2 and 4).
- Replies are plain JSON rather than an SSE stream, since the server sends no
  progress or log messages. That makes them easy to test with `curl` and is
  one less thing to go wrong through the tunnel.
- Both are fixed constructor arguments (`FastMCP(stateless_http=True,
  json_response=True)`), not settings. Revisit only if a feature needs the
  server to send requests to the client mid-call; URL-mode elicitation
  probably doesn't (it can be returned as an error from the tool call),
  to be confirmed when it's built.
- **DNS-rebinding protection:** FastMCP turns it on for `127.0.0.1` and allows
  only `localhost` Host headers, which would reject requests arriving through
  the tunnel. Pass `TransportSecuritySettings` that also allow the public
  hostname from `KCAL_PUBLIC_URL`.
- The tools themselves don't change, except for the remote exposure filter
  (section 5).

## 2. Authentication flow

The SDK provides the OAuth endpoints (`/.well-known/oauth-authorization-server`,
`/.well-known/oauth-protected-resource`, `/register`, `/authorize`, `/token`,
`/revoke`) and bearer-token checks on `/mcp`. We implement
`OAuthAuthorizationServerProvider` and one `/login` page via `custom_route`.

`/login` has two entry points, sharing the same form, MFA step, owner check
and rate limit:

- **With a pending OAuth request** (`/login?req=<id>`, reached from
  `/authorize`): after the Garmin login it completes the OAuth flow.
- **Opened directly** (the link in the expired-Garmin-session error): it only
  replaces the stored Garmin tokens and says "done, you can retry". The MCP
  tokens are still valid, so no OAuth flow is needed. `/authorize` can't serve
  this case: it only works when started by a registered client (client ID,
  redirect URI, PKCE) and ends by handing that client a code.

Connecting a client:

1. Client discovers metadata, registers itself (dynamic client registration)
   and opens `/authorize` in the owner's browser (PKCE).
2. `provider.authorize()` stores the pending request under a random,
   short-lived ID and returns a redirect to `/login?req=<id>`.
3. `/login` (GET) shows a form: Garmin email + password.
4. `/login` (POST) logs in with `Garmin(..., return_on_mfa=True)`.
   - MFA required → keep the half-finished login state in memory under the
     request ID (a few minutes, never on disk) and show an MFA-code form.
     The second POST calls `resume_login(state, code)`.
5. On success: check the session's Garmin profile ID equals
   `KCAL_GARMIN_OWNER`. If not, discard the session and fail.
6. Save the Garmin tokens to the token store, replace the server's cached
   Garmin session, issue an authorization code and redirect to the client's
   `redirect_uri`. The client exchanges it at `/token`.

Rules for the login page:

- The password lives only in the request handler's memory: never logged,
  never stored, never echoed back into the form.
- **Rate limit:** at most 5 failed Garmin attempts per hour (global, not per
  IP, since everything arrives through the tunnel), then refuse until the
  window passes. Failed attempts reach Garmin, which can lock or throttle the
  account; this is what protects it.
- The pending request expires after 10 minutes; codes are single-use and
  expire after 5 minutes.
- Basic hardening: CSRF token in the form, `Cache-Control: no-store`,
  `X-Frame-Options: DENY`.

The page is built as a chain of steps so a "Sign in with Google" step can be
put in front later without restructuring it.

### Tokens

| Token | Lifetime | Notes |
|---|---|---|
| Access token | 1 hour | Opaque random string; stored hashed. |
| Refresh token | 30 days, rotated on use | Client refreshes silently; a full re-login is needed only after 30 days of no use or a revoke. A rotated token that comes back again (stolen or replayed) ends the whole chain of tokens from that login. |
| Authorization code | 5 minutes, single use | |

All stored in one SQLite file (`KCAL_STATE_DIR/mcp_auth.sqlite`): registered
clients, refresh tokens, access tokens (hashed). Moving hosts = copying this
file; skipping the copy costs one reconnect per client.

## 3. Expired credentials

| Situation | Server response | What the owner sees |
|---|---|---|
| MCP access token expired | 401 | Nothing; the client refreshes it. |
| MCP refresh failed / revoked | 401 with `WWW-Authenticate: Bearer error="invalid_token", error_description=…, resource_metadata=…` | The client's own "reconnect" prompt. The description is filled in but clients don't show it, so it's not relied on. |
| **Garmin tokens expired or revoked** | Normal tool result, `isError: true` | The model relays: *"Garmin session expired. Open https://…/login, sign in, then ask again."* |
| Garmin unreachable or rate-limiting | Normal tool result, `isError: true` | "Try again in a few minutes", with no login link: signing in again wouldn't help and would spend sign-in attempts. |

The Garmin-expiry error is the owner's only cue, so it must be actionable on
its own, read by a model that knows nothing about kcal:

- say what happened (the Garmin session expired; nothing is wrong with the
  connector);
- give the full `/login` URL, built from `KCAL_PUBLIC_URL`;
- say what to do there (sign in to Garmin, enter the MFA code if asked);
- say what to do after (ask again; no need to reconnect the connector);
- tell the model to show the link to the user rather than retry.

Tests assert each of these parts is in the message.

Opening `/login` directly needs no extra gate, because passing it requires
logging in to Garmin as the owner.

`_no_mfa()`'s "run `kcal fetch`" message stays for stdio; in HTTP mode the
error points to `/login` instead.

## 4. Settings

All from environment variables (CLI flags may override), no hard-coded paths:

| Variable | Default | Meaning |
|---|---|---|
| `KCAL_PUBLIC_URL` | — (required with `--http`) | Public base URL, e.g. the quick-tunnel URL. Issuer and resource URL derive from it. |
| `KCAL_PORT` | `8000` | Local port. |
| `KCAL_GARMIN_OWNER` | — (required with `--http`) | Garmin profile ID allowed to log in. |
| `KCAL_STATE_DIR` | `~/.kcal` | Holds `garmin_tokens/` (shared with the CLI on the PC) and `mcp_auth.sqlite`. |

The server refuses to start in HTTP mode without the required settings. A
small `kcal whoami` command prints the logged-in Garmin profile ID so the
owner value is easy to find.

Quick tunnel: `cloudflared tunnel --url http://localhost:8000` prints a new
`*.trycloudflare.com` URL on every start. Set `KCAL_PUBLIC_URL` to it, start
the server, and re-add the connector in each client (the old registration is
tied to the old URL). A launcher that starts `cloudflared`, reads the URL and
starts the server is a possible convenience later.

## 5. Remote exposure

In HTTP mode, tools only reach allowlisted endpoints; stdio stays
unrestricted. `call_garmin_endpoint` fails with "not available remotely" for
anything else, `list_garmin_endpoints` only lists allowed endpoints, and a
disallowed `metrics` entry of `get_garmin_day` gets its own "not available
remotely" error while the other metrics are returned.

The allowlist is enforced in one place: tools reach Garmin only through a
stand-in object that checks every attribute against the current policy
(including the curated tools, which only use allowed endpoints anyway). The
policy fails closed: code running outside a tool wrapper gets the remote
rules, and stdio tools unlock everything explicitly.

**Allowed** (health and fitness):

- Daily metrics: `stats`, `stats_and_body`, `user_summary`, `daily_steps`,
  `steps_data`, `floors`, `heart_rates`, `rhr_day`, `hrv_data`, `sleep_data`,
  `stress_data`, `all_day_stress`, `body_battery`, `body_battery_events`,
  `respiration_data`, `spo2_data`, `hydration_data`,
  `intensity_minutes_data`, `all_day_events`, `weekly_steps`,
  `weekly_stress`, `weekly_intensity_minutes`
- Body: `weigh_ins`, `daily_weigh_ins`, `body_composition`, `blood_pressure`
- Training: `training_readiness`, `morning_training_readiness`,
  `training_status`, `max_metrics`, `endurance_score`, `hill_score`,
  `race_predictions`, `fitnessage_data`, `lactate_threshold`, `cycling_ftp`,
  `running_tolerance`, `personal_record`, `progress_summary_between_dates`
- Activities (completed sessions; the source of workout calories in
  `get_garmin_summary`): `activities`, `activities_by_date`,
  `activities_fordate`, `activity`, `last_activity`, `activity_splits`,
  `activity_split_summaries`, `activity_typed_splits`,
  `activity_exercise_sets`, `activity_hr_in_timezones`,
  `activity_power_in_timezones`, `activity_types`
- Workouts (planned workout templates and the schedule): `workouts`,
  `workout_by_id`, `scheduled_workouts`
- Nutrition: `nutrition_daily_food_log`, `nutrition_daily_meals`,
  `nutrition_daily_settings`, `lifestyle_logging_data`

**Not allowed:** `activity_details` (GPS track), `activity_weather`
(location-derived), profile and settings (`user_profile`,
`userprofile_settings`, `full_name`, `unit_system`), devices, gear, badges and
challenges, golf, training plans, `goals`,
`menstrual_*` and `pregnancy_summary`.

**Stripping private fields:** activity summaries and splits carry start/end
coordinates (`startLatitude`, `endLongitude`, …), and `get_activity` has a
place name (`locationName`); these reveal where the owner lives. Allowed
activity endpoints also carry the owner's identity (`ownerFullName`,
`ownerDisplayName`, `ownerProfileImageUrl*`, `userInfoDto`), which blocking
`full_name` and `user_profile` is meant to keep out. In HTTP mode every result
passes through a filter that drops, recursively, keys containing the words
`latitude`, `longitude`, `polyline`, `geo`, `location`, `owner`, `fullname`,
`displayname` or `email`, or the word pairs "full name", "display name",
"profile image" or "user info". Keys are split into words (camelCase or
snake_case) and compared whole, so `averageOxygen…` and `timeAllocation` are
kept. Elevation and time zone are left in: they're coarse.

The allowlist lives in `endpoints.py` as one explicit set, so new
`garminconnect` getters are blocked remotely until added on purpose.

Besides getters, `get_garmin_summary` uses three Garmin range endpoints that
`garminconnect` doesn't wrap (HRV, sleep statistics and training readiness
for a date range). Remotely, raw requests are allowed only for those, with
the whole path matched (`summary.RANGE_PATHS`: the endpoint followed by two
dates), so nothing else is reachable that way. `get_garmin_day`'s `metrics`
schema lists only the allowlisted single-day endpoints, so other names are
refused before any code runs.

**Tests**, positive and negative:

- Every endpoint on the "not allowed" list is rejected in HTTP mode, through
  each entry point: `call_garmin_endpoint`, the `metrics` of
  `get_garmin_day`, and its absence from `list_garmin_endpoints`. The rejection
  happens before any Garmin request is made (the faked `Garmin` records no
  call).
- A getter that's on neither list (e.g. a made-up `get_new_thing` added to the
  faked `Garmin`) is rejected too, proving the default is deny.
- Every allowlisted name exists on the installed `garminconnect.Garmin`, so a
  library rename can't silently drop an endpoint.
- The same disallowed endpoints still work over stdio.
- Stripping removes nested coordinate and identity keys (in lists and nested
  objects) and keeps look-alike health fields unchanged; no allowlisted
  endpoint name counts as private.
- The stand-in blocks disallowed getters and non-getters by default, and the
  curated tools go through it.
- A failed Garmin login fails `get_garmin_day` once (one login attempt), not
  once per metric.

## 6. Other security notes

- Logs record tool name, arguments and outcome, never result payloads,
  passwords, MFA codes or tokens.
- Garmin tokens and `mcp_auth.sqlite` on disk are what protect the data;
  `KCAL_STATE_DIR` should only be readable by the owner.
- One shared Garmin session; re-login replaces it under the existing lock.
- Revoking all access: `kcal-mcp --revoke-all` signs every client out,
  even while the server runs (clients stay registered and just log in
  again). Deleting `mcp_auth.sqlite` and restarting also forgets the
  registrations.
- `/register` and `/authorize` are open to anyone with the URL, so what they
  can make the server keep is bounded: at most 100 waiting authorizations
  (oldest dropped), clients that never got a token are forgotten after a
  day, and registration stops at 500 clients.
- `KCAL_STATE_DIR` is created owner-only (0700) and `mcp_auth.sqlite` is
  0600 where the OS supports it (on Windows the user profile's ACLs apply).
- The OAuth scope is `garmin:read`. Registration is open to any client, as
  the MCP spec expects; what protects the data is that tokens are only
  issued after the owner logs in on `/login`. That page should therefore
  show which client is asking and where it will send the owner back
  (client name and redirect host), so a look-alike client can be spotted.

## 7. Moving to an always-on host later

- Use a stable hostname (named Cloudflare Tunnel or the host's own domain) so
  connectors don't need re-adding.
- Add a Dockerfile and a `/healthz` route.
- Copy `garmin_tokens/` and `mcp_auth.sqlite` to the host's state volume.
- Add the Google/GitHub sign-in step in front of the Garmin form.

## Build order

1. `--http` transport with settings, transport-security hosts, and tests
   (no auth yet; only exposed through the tunnel briefly for a smoke test).
2. Remote allowlist and location stripping, with the positive and negative
   tests from section 5.
3. OAuth provider + SQLite store, with tests against the SDK's handlers.
   *(done; `/login` is a 503 placeholder)*
4. `/login` page (both entry points): Garmin login, MFA step, owner check,
   rate limit; tests with a faked `Garmin`. *(done)*
5. Connect claude.ai and ChatGPT through a quick tunnel; check the current
   requirements of each (ChatGPT developer mode, claude.ai custom connectors).
6. Later: Google sign-in gate, hosting; optionally URL-mode elicitation.

## Open questions

- Training plans are excluded mainly as "not health data"; allow them if they
  turn out to be useful.
