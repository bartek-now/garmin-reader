# Deploying the HTTP MCP server

How to run `kcal-mcp --http` on an always-on host instead of a PC behind a
quick tunnel. Everything here applies to any host; choosing one is separate.
Background: [http-server-design.md](http-server-design.md).

## What the host must provide

| Requirement | Why |
|---|---|
| **A stable HTTPS URL** | Clients register against it. Tokens are bound to `<URL>/mcp`, so a new URL means re-adding every connector. |
| **Exactly one running instance** | Sign-ins in progress, verification-code steps, the failed-sign-in limit and the cached Garmin session live in memory. With two instances, `/authorize` can reach one and `/login` the other, and sign-in breaks. A platform that scales to zero also drops a sign-in in progress. |
| **A persistent volume** | Holds the Garmin tokens and the OAuth clients and tokens. Without it, every restart means signing in to Garmin again and reconnecting every client. Use a real disk; SQLite isn't safe on network or object-storage mounts. |
| **Outbound HTTPS** | To Garmin (`*.garmin.com`). |

On Cloud Run that means `--min-instances=1 --max-instances=1` and a disk-backed
volume, which removes most of its benefit; a small VM or a single-machine
platform with volumes (e.g. Fly.io) fits better.

## The container

The [Dockerfile](../Dockerfile) builds an image that runs as a non-root user,
keeps all state in `/data` and listens on `0.0.0.0`:

```bash
docker build -t kcal-mcp .
docker run -d --name kcal-mcp --restart unless-stopped \
  -p 8000:8000 \
  -v kcal-data:/data \
  -e KCAL_PUBLIC_URL=https://kcal.example.com \
  -e KCAL_GARMIN_OWNER=1234567 \
  kcal-mcp
```

Put the HTTPS front end (the platform's, a reverse proxy such as Caddy, or a
named Cloudflare Tunnel) in front of port 8000. The container's health check
calls `/healthz`, which needs no sign-in and reports only that the server is
up. It doesn't check Garmin, so a Garmin outage doesn't get the server
restarted.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `KCAL_PUBLIC_URL` | required | The HTTPS URL clients use, without a path. |
| `KCAL_GARMIN_OWNER` | required | Garmin profile ID allowed to sign in. `kcal whoami` prints yours. |
| `KCAL_HOST` | `127.0.0.1` (`0.0.0.0` in the image) | Address to listen on. |
| `KCAL_PORT` | `$PORT`, else `8000` | Port to listen on. Platforms such as Cloud Run set `PORT`; don't set `KCAL_PORT` there. |
| `KCAL_STATE_DIR` | `~/.kcal` (`/data` in the image) | Where `garmin_tokens/` and `mcp_auth.sqlite` live. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Read by uvicorn: the front end's address, so logs show the real client address from `X-Forwarded-For` instead of the front end's. Only affects logs. Never `*` if the server can be reached without the front end. |

## Signing in to Garmin on the host

After the first start, open `<KCAL_PUBLIC_URL>/login` and sign in to Garmin
(with the verification code if asked). The host then has its own Garmin
session. Do this rather than copying your PC's tokens: a copied session is
shared, and when either side renews it the other side's copy can stop
working.

Garmin's sign-in sits behind bot protection that can be stricter with
data-centre addresses, so a sign-in from the host may be refused. Renewing an
existing session usually still works. If it's refused: sign in on a PC with
`kcal fetch`, copy `~/.kcal/garmin_tokens/garmin_tokens.json` into the
volume's `garmin_tokens/`, restart, and from then on don't use that session
on the PC (sign in again there if you need `kcal fetch`).

## Moving connected clients

`mcp_auth.sqlite` holds the registered clients and their (hashed) tokens.
Copying it to the host keeps Claude and ChatGPT connected **only if the URL
stays the same**: tokens are bound to `<URL>/mcp`. Coming from a quick tunnel,
the URL changes anyway, so start with an empty volume and add the connectors
again (Claude: "Sign in now", "Register automatically"; ChatGPT: OAuth).

## Running it

- **Upgrades:** rebuild and restart. Everything in the volume survives;
  sign-ins in progress don't.
- **Signing everyone out:** `docker exec kcal-mcp kcal-mcp --revoke-all`.
  This also invalidates sign-ins in progress and outstanding authorization codes.
- **Changing owners:** changing `KCAL_GARMIN_OWNER` revokes previous
  authorizations. Sign in as the new owner at `/login`, then reconnect clients.
  Stored Garmin credentials for a different account cannot serve health data.
- **Garmin session expired:** the tools reply with a link to `/login`; signing
  in there fixes it without reconnecting anything.
- **Logs** go to stdout: every request's status line, plus the reason for any
  rejected one (never payloads, passwords or tokens).

## Security

- The volume holds a Garmin refresh token, which is access to your Garmin
  account, and the OAuth database. Restrict who can read the disk, and
  encrypt any backup of it.
- Keep the server reachable only through the HTTPS front end; don't publish
  port 8000 to the internet directly.
- The remote exposure rules (allowlist, no location or identity data) apply
  over HTTP regardless of host.
