# kcal

Fetches daily steps and workout calorie data from Garmin Connect.

Each row of output covers one day:

- `weight_kg` — weight from your Garmin scale, taken from the last weigh-in
  of the day if there was more than one; blank if you didn't weigh in that day
- `workout_active_calories` — active calories from tracked workouts, summed
  across all of that day's activities. Garmin's per-activity `calories` field
  is gross (it includes the basal metabolic cost for that activity's
  duration), so this is net of that BMR portion
- `workout_calories` — gross calories Garmin attributes to workouts
  (FYI; includes the BMR portion netted out of `workout_active_calories`)
- `steps` — Garmin's raw daily step total
- `non_workout_steps` — `steps` minus steps attributed to tracked workouts
  (never negative)
- `active_calories` — Garmin's daily active calorie total
- `passive_calories` — Garmin's daily BMR (basal/passive) calorie total

## Setup

```
python -m venv .venv
.venv/Scripts/activate   # or `source .venv/bin/activate` on macOS/Linux
pip install -e .
```

Set your Garmin Connect credentials as environment variables:

```
export GARMIN_EMAIL=you@example.com
export GARMIN_PASSWORD=yourpassword
```

The first login caches a session token under `~/.kcal/garmin_tokens`, so
credentials aren't needed again until that session expires. If your account
has MFA enabled, you'll be prompted for the code on first login.

## Usage

```
kcal fetch --date 2026-07-06
kcal fetch --from 2026-07-01 --to 2026-07-07 --output week.csv
kcal fetch --from 2026-07-01
```

Output is CSV, printed to stdout unless `--output` is given.

Date selection:
- `--date`: a single specific day.
- `--from` and `--to`: an inclusive range.
- `--from` alone: from that day through yesterday.
- neither given: yesterday only (today is skipped since Garmin's daily total
  isn't final until the day is over).
- `--to` without `--from` is an error.

## Tests

```
pip install -e . pytest
pytest
```

## MCP server

`kcal-mcp` runs a stdio MCP server with five tools:

- `get_garmin_summary` (`date`, or `from_date`/`to_date`; defaults to
  yesterday; optional `metrics`): compact numbers per day across a range, for
  trends and for comparing metrics (e.g. HRV against sleep, training load or
  logged behaviours). `metrics` picks groups:
  - `activity`: steps, non-workout steps, active/resting/workout calories,
    intensity minutes, floors (the same numbers as the CSV)
  - `heart`: resting, 7-day resting, min and max heart rate
  - `stress`: average and max stress, body battery
  - `lifestyle`: behaviours logged in Garmin's lifestyle log
  - `weight`: weight, body fat, muscle mass
  - `workouts`: compact list of the day's workouts
  - `hrv`: overnight HRV, weekly average, baseline range, status
  - `sleep`: score, bedtime and wake time, stages, heart rate, breathing,
    SpO2
  - `readiness`: training readiness and acute training load

  The default is `activity`, `weight`, `sleep` and `hrv`. The result is a
  table (`fields` once, then one array per day), which keeps a year of HRV,
  sleep and readiness under the size limit. Ranges go up to 366 days, or 120
  with a per-day group (`activity`, `heart`, `stress`, `lifestyle`). Sleep,
  HRV and readiness on date D describe the night ending that morning.
- `get_garmin_day` (`date`, optional `metrics`): Garmin's own detail for one
  day, from any of its single-day endpoints (sleep, HRV, heart rate, stress,
  ...). Each metric is listed and described in the tool's schema.
- `list_garmin_endpoints`: every read-only Garmin Connect endpoint and its
  parameters.
- `call_garmin_endpoint` (`endpoint`, `args`): call any of those endpoints,
  e.g. `get_sleep_data` with `{"cdate": "2026-10-05"}`. Only `get_*` methods
  are reachable, so nothing can write or delete. Results over ~50k chars are
  replaced by a notice asking for a narrower request.
- `get_kcal_server_info`: which code the running server loaded and whether
  it's stale (code on disk changed since it started).

`kcal fetch` output is unchanged. Login is non-interactive, so run
`kcal fetch` once in a terminal first to cache the session token.

Claude Desktop / Claude Code config (stdio):

```json
{
  "mcpServers": {
    "kcal": {
      "command": "path\\to\\kcal\\.venv\\Scripts\\kcal-mcp.exe"
    }
  }
}
```

### Over HTTP

`kcal-mcp --http` serves the same tools over Streamable HTTP at `/mcp` on
`127.0.0.1`, for remote clients such as claude.ai and ChatGPT reaching it
through a tunnel. See [docs/http-server-design.md](docs/http-server-design.md).
Over HTTP, the tools only reach an allowlist of health and fitness endpoints
(`REMOTE_ALLOWED` in `endpoints.py`), and location and identity data
(coordinates, GPS tracks, place names, your name and profile photo) is
stripped from every result.

Clients connect with OAuth: they register themselves and send you to
`<public URL>/login`, where you sign in to Garmin (with the verification
code if Garmin asks). Only the Garmin account set as owner gets through,
and failed sign-ins are capped at 5 an hour. When Garmin's session expires,
the tools answer with a link to the same page.

1. Run `kcal whoami` to get your Garmin profile ID.
2. Start a tunnel, e.g. `cloudflared tunnel --url http://localhost:8000`.
3. Start `kcal-mcp --http --public-url <tunnel URL> --garmin-owner <profile ID>`.
4. Add `<tunnel URL>/mcp` as a connector. In claude.ai choose "Sign in now"
   and "Register automatically"; in ChatGPT choose OAuth.

`kcal-mcp --revoke-all` signs every client out, even while the server runs.

| Setting | Flag | Default |
|---|---|---|
| `KCAL_PUBLIC_URL` | `--public-url` | required, e.g. `https://abc.trycloudflare.com` |
| `KCAL_GARMIN_OWNER` | `--garmin-owner` | required; your Garmin profile ID from `kcal whoami` |
| `KCAL_PORT` | `--port` | `8000` |
| `KCAL_STATE_DIR` | | `~/.kcal` (Garmin tokens, shared with `kcal fetch`; `mcp_auth.sqlite` with OAuth clients and hashed tokens) |
