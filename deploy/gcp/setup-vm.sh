#!/usr/bin/env bash
# Runs on the VM (deploy.ps1 copies it there): installs Docker, gets the code
# at the given git ref, writes the settings and (re)starts the containers.
# Safe to run again; that's how updates happen.
#
# Usage: setup-vm.sh <domain> <allowed-emails,comma-separated> <git-ref>
set -euo pipefail

DOMAIN=${1:?domain}
EMAILS=${2:?allowed emails}
REF=${3:?git ref}
REPO=https://github.com/bartek-now/garmin-reader.git
DIR=/opt/garmin-reader

# Docker Engine and Compose from Docker's apt repository.
if ! command -v docker >/dev/null; then
    sudo apt-get update -q
    sudo apt-get install -y -q ca-certificates curl git
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \
        "https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
    sudo apt-get update -q
    sudo apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

# An e2-micro has 1 GB of memory: swap keeps image builds from running out.
if ! grep -q '^/swapfile ' /etc/fstab; then
    sudo fallocate -l 1G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi
grep -q '^/swapfile ' /proc/swaps || sudo swapon /swapfile

# The code, at exactly the requested ref. Before the rename it lived in
# /opt/kcal; compose.yaml pins the project name, so moving it keeps the
# containers and volumes.
if [ -d /opt/kcal ] && [ ! -e "$DIR" ]; then
    sudo mv /opt/kcal "$DIR"
fi
if [ ! -d "$DIR/.git" ]; then
    sudo git clone -q "$REPO" "$DIR"
fi
# Follow the repository if it moved, and drop branches deleted or renamed
# on GitHub, so a stale origin/<ref> is never deployed.
sudo git -C "$DIR" remote set-url origin "$REPO"
sudo git -C "$DIR" fetch -q --prune --tags origin
sudo git -C "$DIR" checkout -q --detach "origin/$REF" 2>/dev/null \
    || sudo git -C "$DIR" checkout -q --detach "$REF"
# Drop local branches (the clone made one): a ref that isn't on GitHub must
# fail above, not fall back to an old local branch of the same name.
sudo git -C "$DIR" for-each-ref --format='%(refname)' refs/heads \
    | xargs -r -n1 sudo git -C "$DIR" update-ref -d
echo "garmin-reader at $(sudo git -C "$DIR" log -1 --format='%h %s')"

# Settings, readable by root only.
sudo install -m 600 /dev/null "$DIR/deploy/gcp/.env"
COMMIT=$(sudo git -C "$DIR" rev-parse --short HEAD)
printf 'GARMIN_READER_DOMAIN=%s\nGARMIN_READER_ALLOWED_EMAILS=%s\nGARMIN_READER_GIT_COMMIT=%s\n' "$DOMAIN" "$EMAILS" "$COMMIT" \
    | sudo tee "$DIR/deploy/gcp/.env" >/dev/null

cd "$DIR/deploy/gcp"
# Recreate both containers every time: Compose doesn't reliably notice a
# rebuilt image (with Docker's containerd image store every build gets a
# new ID), nor a changed Caddyfile, which Caddy reads only when it starts.
# Sign-ins in progress are lost, as with any restart.
sudo docker compose up -d --build --force-recreate --remove-orphans
sudo docker image prune -f >/dev/null
sudo docker compose ps
