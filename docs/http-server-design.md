# Remote (HTTP) MCP server — design

Status: agreed in discussion 2026-10-09. Built so far: steps 1-4 (HTTP
transport, settings, remote exposure rules, OAuth authorization server,
Garmin login page). HTTP mode always requires OAuth; there is no
unauthenticated mode. Next: connecting the real clients (step 5). See
"Build order".

## Goal

Let claude.ai (web and mobile) and ChatGPT use the garmin-reader MCP tools, not just
local clients over stdio, for anyone with a Garmin account, each of whom sees
only their own Garmin data.

## Decisions

| Topic | Decision |
|---|---|
| Transport | Streamable HTTP without MCP sessions, behind a flag. stdio stays the default. |
| Hosting | A PC behind a Cloudflare **quick tunnel** at first; code written so a move to an always-on host is a config change (now a Google Cloud VM, see deployment.md). |
| MCP auth | OAuth 2.1 via the MCP SDK's built-in authorization server (`mcp.server.auth`), with dynamic client registration. |
| Who may log in | Whoever completes a **Garmin login (with MFA)** on our `/login` page (reached via `/authorize`). There is no list of permitted users (there used to be, `GARMIN_READER_ALLOWED_EMAILS`). Garmin accepting the password proves they own that account. No separate identity provider yet. |
| Several users | Each user's OAuth tokens carry their email; each tool call uses only the Garmin session stored for that user, never another's or a shared one. |
| Later | A Google (or GitHub) sign-in step in front of the Garmin form, once the hostname is stable. Optionally, URL-mode elicitation as a nicer way to open `/login`. |
| Garmin expiry | Tool error that tells the user exactly what to do, with a link to `/login`; no reconnecting the connector. This is the intended design, not a stopgap. |
| Exposure | `call_garmin_endpoint` is limited to an **allowlist** of health/fitness endpoints, with location and identity fields stripped, over stdio and HTTP alike (stdio used to be unrestricted; see section 5). |

## 1. Transport

- `garmin-reader-mcp` keeps running over stdio. `garmin-reader-mcp --http` serves Streamable
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
  hostname from `GARMIN_READER_PUBLIC_URL`.
- The tools themselves don't change, except for the remote exposure filter
  (section 5).

## 2. Authentication flow

The SDK provides the OAuth endpoints (`/.well-known/oauth-authorization-server`,
`/.well-known/oauth-protected-resource`, `/register`, `/authorize`, `/token`,
`/revoke`) and bearer-token checks on `/mcp`. We implement
`OAuthAuthorizationServerProvider` and one `/login` page via `custom_route`.

`/login` has two entry points, sharing the same form, MFA step
and rate limit:

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
4. `/login` (POST) logs in with `Garmin(..., return_on_mfa=True)`; the
   email is compared lowercase. Any email Garmin accepts gets in.
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
   that profile's own store (`GARMIN_READER_STATE_DIR/users/<profile ID>/`) and
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
  user's typos don't lock the others out.
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

All stored in one SQLite file (`GARMIN_READER_STATE_DIR/mcp_auth.sqlite`): registered
clients, refresh tokens, access tokens (hashed), and each user's Garmin
profile. Moving hosts = copying this file and `users/`; skipping the copy
costs one reconnect per client.

Every token's subject is its user's email. On startup, every stored Garmin
session no user has is deleted, and so are tokens from the single-owner
version, whose subject was a Garmin profile ID. When an email signs in to
(or imports) a different Garmin account, its previous account's stored
session is deleted at once, unless another email still uses it.

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
its own, read by a model that knows nothing about garmin-reader:

- say what happened (the Garmin session expired; nothing is wrong with the
  connector);
- give the full `/login` URL, built from `GARMIN_READER_PUBLIC_URL`;
- say what to do there (sign in to Garmin, enter the MFA code if asked);
- say what to do after (ask again; no need to reconnect the connector);
- tell the model to show the link to the user rather than retry.

Tests assert each of these parts is in the message.

Opening `/login` directly needs no extra gate, because passing it requires
logging in to Garmin; it refreshes that user's session only.

`_no_mfa()`'s "run `garmin-reader fetch`" message stays for stdio; in HTTP mode the
error points to `/login` instead.

## 4. Settings

All from environment variables (CLI flags may override), no hard-coded paths:

| Variable | Default | Meaning |
|---|---|---|
| `GARMIN_READER_PUBLIC_URL` | — (required with `--http`) | Public base URL, e.g. the quick-tunnel URL. Issuer and resource URL derive from it. |
| `GARMIN_READER_PORT` | `8000` | Local port. |
| `GARMIN_READER_STATE_DIR` | `~/.garmin-reader` | Holds `users/` (each HTTP user's Garmin session), `garmin_tokens/` (the CLI's and stdio's) and `mcp_auth.sqlite`. |

The server refuses to start in HTTP mode without the required settings.

Quick tunnel: `cloudflared tunnel --url http://localhost:8000` prints a new
`*.trycloudflare.com` URL on every start. Set `GARMIN_READER_PUBLIC_URL` to it, start
the server, and re-add the connector in each client (the old registration is
tied to the old URL). A launcher that starts `cloudflared`, reads the URL and
starts the server is a possible convenience later.

## 5. Remote exposure

Tools only reach allowlisted endpoints, whatever the transport.
`call_garmin_endpoint` fails with "not available in garmin-reader" for
anything else, `list_garmin_endpoints` only lists allowed endpoints, and a
disallowed `metrics` entry of `get_garmin_day` gets its own "not available"
error while the other metrics are returned.

stdio used to be unrestricted, on the idea that a local server keeps the data
on the machine. It doesn't: Claude Desktop sends tool results to Anthropic
like claude.ai does, and can forward a cloud session's calls to the local
server. Every MCP client hands the data to a model, so there is one policy.
Unfiltered access, if ever needed, belongs in the CLI, whose output goes to
a terminal, not a model.

The allowlist is enforced in one place: tools reach Garmin only through a
stand-in object that checks every attribute against the policy (including
the curated tools, which only use allowed endpoints anyway). Code running
outside a tool wrapper gets the policy too.

Every `garminconnect` getter is either **allowed** or **denied**, in two
explicit lists in `endpoints.py` (`ALLOWED`, and `DENIED` with a reason for
each). A test fails when a getter is on neither list, so one the library adds
is blocked until someone decides on it, rather than staying blocked
unnoticed: the first allowlist predated about 145 getters, and ~20 of them
were missing from `get_garmin_day` until this rule.

The privacy line is drawn by the filter below, not by the lists: anything
whose health or fitness substance survives stripping is allowed. That
includes devices, gear, badges, golf, goals and training plans (harmless once
stripped), and women's health and ECG (sensitive, but the owner's own health
data).

**Denied** are only the endpoints whose substance stripping can't remove:

- Location: GPS tracks and per-second samples (`activity_details`,
  `activity_subsecond_details`, `activity_swim_details`,
  `activity_map_details`, `activity_polyline`), `activity_weather`,
  `activity_segments`, `activity_images`, courses, and `golf_shot_data`.
- Travel: `jetlag_*` and `heat_altitude_acclimation*`.
- Race events, named after where and when they happen: `calendar_event(s)`,
  `calendar_week`, `upcoming_events`, `primary_event`.
- Identity: `full_name`, `user_profile(_base)`, `userprofile_settings`,
  `personal_information`.
- Other people's data: friends' (`adhoc`) challenges.
- `device_messages`, the watch's message queue: a pushed workout's title
  arrives there as `messageName`, and nothing in it is health data.
- Getters that need an ID results never carry: the per-device ones
  (`device_settings`, `device_solar_*`, ...) and `gear`/`gear_defaults`
  (`gear_list` covers gear).

**Stripping private fields:** activity summaries and splits carry start/end
coordinates (`startLatitude`, `endLongitude`, …), and `get_activity` has a
place name (`locationName`); these reveal where the owner lives. Allowed
endpoints also carry the owner's identity (`ownerFullName`,
`ownerDisplayName`, `ownerProfileImageUrl*`, `userInfoDto`, `userId`,
`userProfilePk`) and their devices' (`deviceId`, `unitId`, `serialNumber`,
`deviceName`). Every result passes through a filter that drops, recursively,
keys containing the words `latitude`, `longitude`, `lat`, `lon`, `lng`, `gps`,
`polyline`, `geo`, `location`, `altitude`, `owner`, `fullname`,
`displayname`, `email`, `description`, `notes` (free text on gear, blood
pressure readings and cycle logs), `chronological`, `birth`, `birthdate`,
`birthday` (actual age and birth date; Garmin's fitness age is kept) or
`serial`, or the word pairs "full name", "display name", "user name",
"profile image", "user info", "activity name", "course name", "workout name"
(these titles can contain places), "user id", "user profile", "profile id",
"profile pk", "device id", "unit id", "device name", "image url", "settings
file" "custom make" (`customMakeModel`, a gear nickname) or "message name" (a
workout's title queued on a device, which `active_devices` lists). In the gear
endpoints (`gear_list`, `gear_details`, `activity_gear_v2`), `name` is the
owner's nickname for the gear, so `call_garmin_endpoint` drops it there
(`ENDPOINT_PRIVATE_KEYS`); elsewhere `name` is data, e.g. lifestyle log
entries. Keys are split into words (camelCase or snake_case) and compared whole,
so `averageOxygen…`, `timeAllocation` and `latestSpo2` are kept. Elevation
(how hilly, not where), a device's model and time zone are left in: they're
coarse.

Device IDs also appear as dict keys (`training_status` is keyed by device).
The filter renames keys equal to a device ID found in the same result to
`device`, `device 2`, …, so the data stays and the ID goes. Only IDs of six
digits or more count (Garmin's have ten), so zone or rank keys like `"2"` are
never renamed.

`get_garmin_summary` reads HRV, sleep and training readiness for a date
range through garminconnect's range getters (`get_hrv_data_range`,
`get_sleep_daily`, `get_training_readiness_range`), which are on the
allowlist. Raw requests (`connectapi`) are refused for every path.
`get_garmin_day`'s `metrics`
schema lists only the allowlisted single-day endpoints, so other names are
refused before any code runs.

**Tests**, positive and negative:

- Every endpoint on the "not allowed" list is rejected over stdio and HTTP, through
  each entry point: `call_garmin_endpoint`, the `metrics` of
  `get_garmin_day`, and its absence from `list_garmin_endpoints`. The rejection
  happens before any Garmin request is made (the faked `Garmin` records no
  call).
- A getter that's on neither list (e.g. a made-up `get_new_thing` added to the
  faked `Garmin`) is rejected too, proving the default is deny.
- Every allowlisted name exists on the installed `garminconnect.Garmin`, so a
  library rename can't silently drop an endpoint, and every getter is on
  exactly one of the two lists.
- A `training_status` fixture shaped like a real response comes out with no
  profile or device IDs, device names, image URLs or altitude, its
  device-keyed maps renamed.
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
  `GARMIN_READER_STATE_DIR` should only be readable by the account running the
  server. Whoever runs the server can read every user's data.
- One Garmin session per profile, each in its own store and cache entry;
  a user's re-login replaces only theirs, under that profile's lock.
- Revoking all access: `garmin-reader-mcp --revoke-all` signs every client out,
  even while the server runs (clients stay registered and just log in
  again). Deleting `mcp_auth.sqlite` and restarting also forgets the
  registrations.
- `/register` and `/authorize` are open to anyone with the URL, so what they
  can make the server keep is bounded: at most 100 waiting authorizations
  (oldest dropped), clients that never got a token are forgotten after a
  day, and registration stops at 500 clients.
- `GARMIN_READER_STATE_DIR` is created owner-only (0700) and `mcp_auth.sqlite` is
  0600 where the OS supports it (on Windows the user profile's ACLs apply).
- The OAuth scope is `garmin:read`. Registration is open to any client, as
  the MCP spec expects; what protects the data is that tokens are only
  issued after a user logs in to Garmin on `/login`. That page should therefore
  show which client is asking and where it will send the user back
  (client name and redirect host), so a look-alike client can be spotted.

## 7. Moving to an always-on host later

Done so far (independent of the host): `GARMIN_READER_HOST` and the `PORT` fallback,
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
   became an email allowlist with a Garmin session per user, which was then
   removed: anyone with a Garmin account may sign in)*
5. Connect claude.ai and ChatGPT through a quick tunnel; check the current
   requirements of each (ChatGPT developer mode, claude.ai custom connectors).
6. Later: Google sign-in gate, hosting; optionally URL-mode elicitation.

## Open questions

- Training plans are excluded mainly as "not health data"; allow them if they
  turn out to be useful.
