#!/usr/bin/env bash
# Runs on the VM (deploy.ps1 copies it there): installs Docker, gets the code
# at the given git ref, writes the settings and (re)starts the containers.
# Safe to run again; that's how updates happen.
#
# Usage: setup-vm.sh <domain> <garmin-owner-id> <git-ref>
set -euo pipefail

DOMAIN=${1:?domain}
OWNER=${2:?Garmin owner ID}
REF=${3:?git ref}
REPO=https://github.com/bartek-now/kcal.git
DIR=/opt/kcal

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

# The code, at exactly the requested ref.
if [ ! -d "$DIR/.git" ]; then
    sudo git clone -q "$REPO" "$DIR"
fi
sudo git -C "$DIR" fetch -q --tags origin
sudo git -C "$DIR" checkout -q --detach "origin/$REF" 2>/dev/null \
    || sudo git -C "$DIR" checkout -q --detach "$REF"
echo "kcal at $(sudo git -C "$DIR" log -1 --format='%h %s')"

# Settings, readable by root only.
sudo install -m 600 /dev/null "$DIR/deploy/gcp/.env"
printf 'KCAL_DOMAIN=%s\nKCAL_GARMIN_OWNER=%s\n' "$DOMAIN" "$OWNER" | sudo tee "$DIR/deploy/gcp/.env" >/dev/null

cd "$DIR/deploy/gcp"
# Recreate both containers every time: Compose doesn't reliably notice a
# rebuilt image (with Docker's containerd image store every build gets a
# new ID), nor a changed Caddyfile, which Caddy reads only when it starts.
# Sign-ins in progress are lost, as with any restart.
sudo docker compose up -d --build --force-recreate --remove-orphans
sudo docker image prune -f >/dev/null
sudo docker compose ps
