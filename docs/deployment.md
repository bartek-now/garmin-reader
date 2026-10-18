# Deploying the HTTP MCP server

How to run `garmin-reader-mcp --http` on an always-on host instead of a PC behind a
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
docker build -t garmin-reader-mcp .
docker run -d --name garmin-reader-mcp --restart unless-stopped \
  -p 8000:8000 \
  -v garmin-reader-data:/data \
  -e GARMIN_READER_PUBLIC_URL=https://garmin-reader.example.com \
  -e GARMIN_READER_ALLOWED_EMAILS=you@example.com,friend@example.com \
  garmin-reader-mcp
```

Put the HTTPS front end (the platform's, a reverse proxy such as Caddy, or a
named Cloudflare Tunnel) in front of port 8000. The container's health check
calls `/healthz`, which needs no sign-in and reports only that the server is
up. It doesn't check Garmin, so a Garmin outage doesn't get the server
restarted.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `GARMIN_READER_PUBLIC_URL` | required | The HTTPS URL clients use, without a path. |
| `GARMIN_READER_ALLOWED_EMAILS` | required | Garmin login emails allowed to sign in, comma-separated. Each user sees only their own data. |
| `GARMIN_READER_HOST` | `127.0.0.1` (`0.0.0.0` in the image) | Address to listen on. |
| `GARMIN_READER_PORT` | `$PORT`, else `8000` | Port to listen on. Platforms such as Cloud Run set `PORT`; don't set `GARMIN_READER_PORT` there. |
| `GARMIN_READER_STATE_DIR` | `~/.garmin-reader` (`/data` in the image) | Where `users/` (each user's Garmin session) and `mcp_auth.sqlite` live. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Read by uvicorn: the front end's address, so logs show the real client address from `X-Forwarded-For` instead of the front end's. Only affects logs. Never `*` if the server can be reached without the front end. |

## Signing in to Garmin on the host

Each user signs in to Garmin once at `<GARMIN_READER_PUBLIC_URL>/login` (with the
verification code if asked), usually when first adding the connector. The
host then has its own Garmin session for them, stored under
`users/<Garmin profile ID>/`. Do this rather than copying a PC's tokens: a
copied session is shared, and when either side renews it the other side's
copy can stop working.

Garmin's sign-in sits behind bot protection that can be stricter with
data-centre addresses, so a sign-in from the host may be refused. Renewing an
existing session usually still works. If it's refused: sign in on a PC with
`garmin-reader fetch`, copy the folder with `garmin_tokens.json` to the host and run
`garmin-reader-mcp --import-garmin-session <email> <folder>` there (see the Google
Cloud VM section for the exact commands). It stores the session as that
user's, after checking with Garmin which account it is. From then on don't
use that session on the PC (sign in again there if you need `garmin-reader fetch`).

## Moving connected clients

`mcp_auth.sqlite` holds the registered clients and their (hashed) tokens.
Copying it to the host keeps Claude and ChatGPT connected **only if the URL
stays the same**: tokens are bound to `<URL>/mcp`. Coming from a quick tunnel,
the URL changes anyway, so start with an empty volume and add the connectors
again (Claude: "Sign in now", "Register automatically"; ChatGPT: OAuth).

## Running it

- **Upgrades:** rebuild and restart. Everything in the volume survives;
  sign-ins in progress don't.
- **Signing everyone out:** `docker exec garmin-reader-mcp garmin-reader-mcp --revoke-all`.
  This also invalidates sign-ins in progress and outstanding authorization codes.
- **Adding or removing users:** change `GARMIN_READER_ALLOWED_EMAILS` and restart.
  A new user adds the connector and signs in at `/login`. A removed user is
  signed out at the start, and their stored Garmin session is deleted.
- **Garmin session expired:** the tools reply with a link to `/login`; signing
  in there fixes it without reconnecting anything.
- **Logs** go to stdout: every request's status line, plus the reason for any
  rejected one (never payloads, passwords or tokens).

## Google Cloud VM

[deploy/gcp/](../deploy/gcp/) runs the server on a free-tier e2-micro VM, with
[Caddy](https://caddyserver.com) in front for HTTPS (Let's Encrypt
certificates, renewed automatically):

- `deploy.ps1` runs on your PC, in PowerShell. It creates what's missing in
  the Google Cloud project (a static IP `garmin-mcp-ip`, the firewall rules
  below, and the VM `garmin-mcp`), then runs `setup-vm.sh` on the VM.
- `setup-vm.sh` installs Docker, checks out the requested commit from GitHub
  into `/opt/garmin-reader`, writes the settings to `deploy/gcp/.env` there and
  starts [compose.yaml](../deploy/gcp/compose.yaml): the server (port 8000,
  reachable only by Caddy) and Caddy (ports 80 and 443). The Compose
  project is still called `kcal`, since the volumes are named after it.

The VM runs Debian 12 with a 30 GB standard disk and no service account (it
needs no Google Cloud access). The free tier covers one e2-micro in
`us-east1`, `us-central1` or `us-west1` and its disk. The static IP may be
billed separately; check the project's billing.

**Firewall.** Use a project dedicated to garmin-reader: `deploy.ps1` removes the
default network's rules that open SSH, RDP and ping to the internet
(`default-allow-ssh`, `default-allow-rdp`, `default-allow-icmp`), which
would affect any other VM in the project. What stays open:

- `garmin-mcp-web`: ports 80 and 443 from anywhere. You sign in at `/login`
  from whatever network you're on, and Let's Encrypt checks the hostname
  from addresses it doesn't publish. Caddy refuses requests for any other
  hostname; the MCP and OAuth endpoints require a token or a Garmin
  sign-in. The AI providers' published address ranges aren't used to
  restrict them further: ChatGPT's list changes and has gaps.
- `garmin-mcp-ssh-iap`: SSH only from Google's Identity-Aware Proxy
  (`35.235.240.0/20`), which checks your Google sign-in first. gcloud goes
  through it with `--tunnel-through-iap`.

**First deployment:**

1. `gcloud auth login` with the Google account that owns the project, then
   `gcloud config set project <project>` (the project needs billing enabled,
   even for the free tier).
2. Copy `deploy/gcp/settings.example.json` to `settings.local.json` and
   fill in the hostname, project, zone and the Garmin login emails allowed
   to sign in. It's gitignored: the repository is public. (Parameters such
   as `-Domain` or `-AllowedEmails` override it.)
3. `.\deploy\gcp\deploy.ps1`.
4. Add a DNS `A` record for the hostname pointing at the IP the script
   prints. If the DNS is on Cloudflare, leave the record unproxied (grey
   cloud), so Caddy can get its certificate.
5. Each user adds the connector in Claude or ChatGPT with
   `https://<hostname>/mcp` and signs in to Garmin when asked.

**Updating:** push to GitHub, then run `deploy.ps1` again (`-Ref` picks a
branch, tag or commit; default `main`). It rebuilds and restarts; the
volumes survive. **Adding or removing a user:** edit `allowedEmails` in
`settings.local.json` and run `deploy.ps1`.

**On the VM** (`gcloud compute ssh garmin-mcp --zone us-east1-b --tunnel-through-iap`), from
`/opt/garmin-reader/deploy/gcp`:

- logs: `sudo docker compose logs -f garmin-reader`;
- signing everyone out: `sudo docker compose exec garmin-reader garmin-reader-mcp --revoke-all`;
- if Garmin refuses a user's sign-in from the VM (see above), copy their
  `garmin_tokens.json` to the VM (`gcloud compute scp --tunnel-through-iap`),
  then import it as theirs:

  ```bash
  sudo docker compose exec -u root garmin-reader sh -c 'mkdir -p /tmp/import && chown garmin-reader /tmp/import'
  sudo docker compose cp garmin_tokens.json garmin-reader:/tmp/import/
  sudo docker compose exec -u root garmin-reader chown garmin-reader /tmp/import/garmin_tokens.json
  sudo docker compose exec garmin-reader garmin-reader-mcp --import-garmin-session you@example.com /tmp/import
  sudo docker compose exec -u root garmin-reader rm -rf /tmp/import
  rm garmin_tokens.json
  ```

## Security

- The volume holds each user's Garmin refresh token, which is access to
  their Garmin account, and the OAuth database. Restrict who can read the
  disk, and encrypt any backup of it. Whoever runs the server can read every
  user's data: only allow people who trust them.
- Keep the server reachable only through the HTTPS front end; don't publish
  port 8000 to the internet directly.
- The remote exposure rules (allowlist, no location or identity data) apply
  over HTTP regardless of host.
