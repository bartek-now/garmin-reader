# Deploying the HTTP MCP server

How to run `kcal-mcp --http` on an always-on host instead of a PC behind a
quick tunnel. Most of this applies to any host; [Google Cloud VM](#google-cloud-vm)
is the ready-made setup for one.
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

## Google Cloud VM

[deploy/gcp/](../deploy/gcp/) runs the server on a free-tier e2-micro VM, with
[Caddy](https://caddyserver.com) in front for HTTPS (Let's Encrypt
certificates, renewed automatically):

- `deploy.ps1` runs on your PC, in PowerShell. It creates what's missing in
  the Google Cloud project (a static IP `kcal-mcp-ip`, a firewall rule
  `kcal-mcp-web` for ports 80 and 443, and the VM `kcal-mcp`), then runs
  `setup-vm.sh` on the VM.
- `setup-vm.sh` installs Docker, checks out the requested commit from GitHub
  into `/opt/kcal`, writes the settings to `deploy/gcp/.env` there and
  starts [compose.yaml](../deploy/gcp/compose.yaml): the server (port 8000,
  reachable only by Caddy) and Caddy (ports 80 and 443).

The VM runs Debian 12 with a 30 GB standard disk and no service account (it
needs no Google Cloud access). The free tier covers one e2-micro in
`us-east1`, `us-central1` or `us-west1` and its disk. The static IP may be
billed separately; check the project's billing.

**First deployment:**

1. `gcloud auth login` with the Google account that owns the project, then
   `gcloud config set project <project>` (the project needs billing enabled,
   even for the free tier).
2. `.\deploy\gcp\deploy.ps1 -Domain garminmcp.example.com -GarminOwner <id>`
   (`kcal whoami` prints the ID; `-Project` picks a project other than
   gcloud's current one).
3. Add a DNS `A` record for the hostname pointing at the IP the script
   prints. If the DNS is on Cloudflare, leave the record unproxied (grey
   cloud), so Caddy can get its certificate.
4. Sign in to Garmin at `https://<hostname>/login`, then add the connector in
   Claude or ChatGPT with `https://<hostname>/mcp`.

**Updating:** push to GitHub, then run `deploy.ps1` again with the same
arguments (`-Ref` picks a branch, tag or commit; default `master`). It rebuilds and restarts; the volumes survive.

**On the VM** (`gcloud compute ssh kcal-mcp --zone us-east1-b`), from
`/opt/kcal/deploy/gcp`:

- logs: `sudo docker compose logs -f kcal`;
- signing everyone out: `sudo docker compose exec kcal kcal-mcp --revoke-all`;
- if Garmin refuses the sign-in from the VM (see above), copy the token file
  to the VM (`gcloud compute scp`), then into the volume, owned by the
  server's user:

  ```bash
  sudo docker compose exec -u root kcal mkdir -p /data/garmin_tokens
  sudo docker compose cp garmin_tokens.json kcal:/data/garmin_tokens/
  sudo docker compose exec -u root kcal sh -c 'chown -R kcal:kcal /data/garmin_tokens && chmod 600 /data/garmin_tokens/*'
  sudo docker compose restart kcal
  rm garmin_tokens.json
  ```

## Security

- The volume holds a Garmin refresh token, which is access to your Garmin
  account, and the OAuth database. Restrict who can read the disk, and
  encrypt any backup of it.
- Keep the server reachable only through the HTTPS front end; don't publish
  port 8000 to the internet directly.
- The remote exposure rules (allowlist, no location or identity data) apply
  over HTTP regardless of host.
