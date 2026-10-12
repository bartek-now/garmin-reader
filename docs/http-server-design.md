# Remote (HTTP) MCP server — design

Status: agreed in discussion 2026-10-09. Built so far: steps 1-4 (HTTP
transport, settings, remote exposure rules, OAuth authorization server,
Garmin login page). HTTP mode always requires OAuth; there is no
unauthenticated mode. Next: connecting the real clients (step 5). See
"Build order".

## Goal

Let claude.ai (web and mobile) and ChatGPT use the kcal MCP tools, not just
local clients over stdio, for a few allowed people, each of whom sees only
their own Garmin data.

## Decisions

| Topic | Decision |
|---|---|
| Transport | Streamable HTTP without MCP sessions, behind a flag. stdio stays the default. |
| Hosting | A PC behind a Cloudflare **quick tunnel** at first; code written so a move to an always-on host is a config change (now a Google Cloud VM, see deployment.md). |
| MCP auth | OAuth 2.1 via the MCP SDK's built-in authorization server (`mcp.server.auth`), with dynamic client registration. |
| Who may log in | Whoever completes a **Garmin login (with MFA) with an email on the allowlist** (`KCAL_ALLOWED_EMAILS`) on our `/login` page (reached via `/authorize`). Garmin accepting the password proves they own that account. No separate identity provider yet. |
| Several users | Each user's OAuth tokens carry their email; each tool call uses only the Garmin session stored for that user, never another's or a shared one. |
| Later | A Google (or GitHub) sign-in step in front of the Garmin form, once the hostname is stable. Optionally, URL-mode elicitation as a nicer way to open `/login`. |
| Garmin expiry | Tool error that tells the user exactly what to do, with a link to `/login`; no reconnecting the connector. This is the intended design, not a stopgap. |
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

`/login` has two entry points, sharing the same form, MFA step, allowlist
check and rate limit:

- **With a pending OAuth request** (`/login?req=<id>`, reached from
  `/authorize`): after the Garmin login it completes the OAuth flow.
- **Opened directly** (the link in the expired-Garmin-session error): it only
  replaces that user's stored Garmin tokens and says "done, you can retry". The MCP
  tokens are still valid, so no OAuth flow is needed. `/authorize` can't serve
  this case: it only works when started by a registered client (client ID,
  redirect URI, PKCE) and ends by handing that client a code.

Connecting a client:

1. Client discovers metadata, registers itself (dynamic client registration)
   and opens `/authorize` in the user's browser (PKCE).
2. `provider.authorize()` stores the pending request under a random,
   short-lived ID and returns a redirect to `/login?req=<id>`.
3. `/login` (GET) shows a form: Garmin email + password.
4. `/login` (POST) refuses an email that isn't on the allowlist (compared
   lowercase) without contacting Garmin. Otherwise it logs in with
   `Garmin(..., return_on_mfa=True)`.
   - MFA required → keep the half-finished login state in memory under the
     request ID (a few minutes, never on disk) and show an MFA-code form.
     The second POST calls `resume_login(state, code)`. After a wrong code
     Garmin keeps the step open, so the form is shown again with the error
     (each wrong code counts toward the failure limit); if Garmin has closed
     the step, the user starts over. If Garmin couldn't be reached while
     checking the code, the user is told so, it isn't counted as a failed
     attempt, and the same code can simply be sent again. One code at a time is checked per
     step, and expiry is checked again when a waiting submission's turn
     comes. A repeat of the same wrong code is replayed rather than sent
     again, but after a temporary failure (Garmin unreachable or
     rate-limiting) the same code is really tried again. Once the step has
     succeeded, any submission for it gets that success. The form links to
     "Start over".
5. On success: look up the session's Garmin profile ID, save the tokens to
   that profile's own store (`KCAL_STATE_DIR/users/<profile ID>/`) and
   record the email's profile in the database.
6. Drop that user's cached Garmin session, issue an authorization code for
   the email and redirect to the client's `redirect_uri`. The client
   exchanges it at `/token`.

Rules for the login page:

- The password lives only in the request handler's memory: never logged,
  never stored, never echoed back into the form.
- **Rate limit:** at most 5 failed Garmin attempts per hour per email (not
  per IP, since everything may arrive through one tunnel or proxy), then
  refuse until the window passes. Failed attempts reach Garmin, which can
  lock or throttle the account; this is what protects it. Per email, one
  user's typos don't lock the others out; emails not on the allowlist never
  reach Garmin, so they aren't counted.
  In-flight attempts keep their slots through the profile lookup, including
  after the MFA step.
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
clients, refresh tokens, access tokens (hashed), and each user's Garmin
profile. Moving hosts = copying this file and `users/`; skipping the copy
costs one reconnect per client.

Every token's subject is its user's email. Only allowed users get codes or
tokens, and a token whose user isn't allowed doesn't load, even if it is
still in the database. On startup, tokens and profiles of users no longer
allowed are deleted, along with every stored Garmin session no allowed user
has; so are tokens from the single-owner version, whose subject was a
Garmin profile ID. When an email signs in to (or imports) a different
Garmin account, its previous account's stored session is deleted at once,
unless another allowed email still uses it.

The database also records a revocation generation. Pending sign-ins and
authorization codes carry their starting generation, and token issuance
checks it in the same SQLite transaction as the write. `--revoke-all`
advances that generation, so a separate CLI process can invalidate codes and
pending sign-ins held in the running server's memory.

Each tool call reads the user from its access token and uses only that
user's Garmin session, loaded from their profile's store. A call without a
user fails rather than falling back to any session. Loaded sessions are
cached per Garmin profile, not per email, so two emails of one account
(after a login-email change) don't each rotate the other's refresh token.
Each profile's stored session has a revision in the database, renewed by
every sign-in or `--import-garmin-session`; a call whose revision differs
from the cached one reloads first, even if another process made the change.
Each loaded session is checked to be the user's Garmin profile before any
tool can read health data, and checked again whenever it's reloaded.

## 3. Expired credentials

| Situation | Server response | What the user sees |
|---|---|---|
| MCP access token expired | 401 | Nothing; the client refreshes it. |
| MCP refresh failed / revoked | 401 with `WWW-Authenticate: Bearer error="invalid_token", error_description=…, resource_metadata=…` | The client's own "reconnect" prompt. The description is filled in but clients don't show it, so it's not relied on. |
| **Garmin tokens expired or revoked** | Normal tool result, `isError: true` | The model relays: *"Garmin session expired. Open https://…/login, sign in, then ask again."* |
| Garmin unreachable or rate-limiting | Normal tool result, `isError: true` | "Try again in a few minutes", with no login link: signing in again wouldn't help and would spend sign-in attempts. |

The Garmin-expiry error is the user's only cue, so it must be actionable on
its own, read by a model that knows nothing about kcal:

- say what happened (the Garmin session expired; nothing is wrong with the
  connector);
- give the full `/login` URL, built from `KCAL_PUBLIC_URL`;
- say what to do there (sign in to Garmin, enter the MFA code if asked);
- say what to do after (ask again; no need to reconnect the connector);
- tell the model to show the link to the user rather than retry.

Tests assert each of these parts is in the message.

Opening `/login` directly needs no extra gate, because passing it requires
logging in to Garmin with an allowed email; it refreshes that user's
session only.

`_no_mfa()`'s "run `kcal fetch`" message stays for stdio; in HTTP mode the
error points to `/login` instead.

## 4. Settings

All from environment variables (CLI flags may override), no hard-coded paths:

| Variable | Default | Meaning |
|---|---|---|
| `KCAL_PUBLIC_URL` | — (required with `--http`) | Public base URL, e.g. the quick-tunnel URL. Issuer and resource URL derive from it. |
| `KCAL_PORT` | `8000` | Local port. |
| `KCAL_ALLOWED_EMAILS` | — (required with `--http`) | Garmin login emails allowed to log in, comma-separated. |
| `KCAL_STATE_DIR` | `~/.kcal` | Holds `users/` (each HTTP user's Garmin session), `garmin_tokens/` (the CLI's and stdio's) and `mcp_auth.sqlite`. |

The server refuses to start in HTTP mode without the required settings, or
with the single-owner `KCAL_GARMIN_OWNER` still set.

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
`displayname`, `email`, `description`, `chronological`, `birth`, `birthdate`
or `birthday` (actual age and birth date; Garmin's fitness age is kept), or
the word pairs "full name", "display name", "profile image", "user info",
"activity name", "course name" or "workout name" (these titles can contain
places). Keys are split into words (camelCase or snake_case) and compared whole, so `averageOxygen…` and `timeAllocation` are
kept. Elevation and time zone are left in: they're coarse.

The allowlist lives in `endpoints.py` as one explicit set, so new
`garminconnect` getters are blocked remotely until added on purpose.

`get_garmin_summary` reads HRV and sleep for a date range through
garminconnect's range getters (`get_hrv_data_range`, `get_sleep_daily`),
which are on the allowlist. Training readiness for a range has no getter, so
it is the one raw request: remotely, raw requests are allowed only for that
endpoint, with the whole path matched (`summary.RANGE_PATHS`: the endpoint
followed by two dates), so nothing else is reachable that way. `get_garmin_day`'s `metrics`
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
  `KCAL_STATE_DIR` should only be readable by the account running the
  server. Whoever runs the server can read every user's data.
- One Garmin session per profile, each in its own store and cache entry;
  a user's re-login replaces only theirs, under that profile's lock.
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
  issued after an allowed user logs in on `/login`. That page should therefore
  show which client is asking and where it will send the user back
  (client name and redirect host), so a look-alike client can be spotted.

## 7. Moving to an always-on host later

Done so far (independent of the host): `KCAL_HOST` and the `PORT` fallback,
a Dockerfile, a `/healthz` route, and [deployment.md](deployment.md), which
covers what any host must provide, settings, Garmin sign-in on the host and
moving state. For a Google Cloud e2-micro VM with Caddy in front,
[deploy/gcp/](../deploy/gcp/) creates and updates everything.

Still depends on the choice of host:

- The HTTPS front end and a stable hostname (platform-provided, a reverse
  proxy, or a named Cloudflare Tunnel), so connectors don't need re-adding.
- How the persistent volume is provided.
- Whether Garmin accepts sign-ins from that host's addresses.
- The Google/GitHub sign-in step in front of the Garmin form (needs the
  stable URL).
- Deployment automation.

## Build order

1. `--http` transport with settings, transport-security hosts, and tests
   (no auth yet; only exposed through the tunnel briefly for a smoke test).
2. Remote allowlist and location stripping, with the positive and negative
   tests from section 5.
3. OAuth provider + SQLite store, with tests against the SDK's handlers.
   *(done; `/login` is a 503 placeholder)*
4. `/login` page (both entry points): Garmin login, MFA step, owner check,
   rate limit; tests with a faked `Garmin`. *(done; the owner check later
   became an email allowlist with a Garmin session per user)*
5. Connect claude.ai and ChatGPT through a quick tunnel; check the current
   requirements of each (ChatGPT developer mode, claude.ai custom connectors).
6. Later: Google sign-in gate, hosting; optionally URL-mode elicitation.

## Open questions

- Training plans are excluded mainly as "not health data"; allow them if they
  turn out to be useful.
