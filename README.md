# wg-frigate-back

Dockerized backup for Frigate recordings: brings up a WireGuard tunnel, then
syncs recordings to an SFTP host on a cron schedule (with an initial sync at
container start).

## How it works

On container start, [entrypoint.sh](entrypoint.sh):

1. Validates `BACKUP_SCHEDULE` (5-field crontab expression or `@daily`-style shortcut).
2. Copies the WireGuard config from `WG_CONFIG_PATH` to a tmp dir, **stripping any
   `DNS =` lines** (the slim image has no `resolvconf`, which `wg-quick` would
   otherwise require), and brings the tunnel up with `wg-quick`. All traffic is
   routed through the tunnel when the config's `AllowedIPs` covers `0.0.0.0/0`.
3. Installs the cron job and snapshots the container's environment so scheduled
   runs see the same variables (cron otherwise starts jobs with an empty env).
4. Runs `/app/sync.py` once as the initial backup (a failure is logged, not fatal).
5. Hands off to `cron -f`, which keeps the container running and fires the
   scheduled backups. Job output goes to `docker logs`.

## Requirements

- Linux host with WireGuard kernel support (any kernel ≥ 5.6; the container
  uses the **host's** kernel module).
- The container must run with `--cap-add NET_ADMIN`.
- For full-tunnel routing (`AllowedIPs = 0.0.0.0/0`) inside a container:
  `--sysctl net.ipv4.conf.all.src_valid_mark=1`.
- Your WireGuard `.conf` mounted into the container (read-only is fine; any
  `DNS =` lines are stripped automatically at startup).

## Network isolation

**Only this container's traffic goes through the tunnel.** The WireGuard
interface, routes, and policy rules are created inside the container's own
network namespace; the host and all other containers keep their normal
routing (verified: `wg0` and its rules are visible only inside this
container, and other containers' egress is unaffected).

The tunnel is also **fail-closed** for this container: wg-quick installs
kill-switch rules, so if the tunnel is down the container has no network
access at all — backups can never leak over the raw network.

Do **not** run the container with:

- `--network host` / `network_mode: host` — the tunnel would be created in
  the host's namespace and capture the whole machine's traffic.
- `--network container:<other>` — the tunnel would capture that other
  container's traffic too.

The default bridge network (or any user-defined bridge) is what you want.
The `--sysctl` flag is per-container and does not touch the host.

One caveat: on a *user-defined* bridge network, DNS lookups are answered by
Docker's embedded resolver (127.0.0.11), which forwards them from the host —
so name resolution happens outside the tunnel (the actual data transfer still
goes through it). On the default bridge, DNS goes through the tunnel like
everything else. Use an IP for `SFTP_HOST` if DNS privacy matters.

## Environment variables

REQUIRED OS VARS:
```
WG_CONFIG_PATH=/path/to/wg0.conf
LOCAL_RECORDINGS_PATH=/path/to/frigate/recordings
SFTP_HOST=sftp.example.com
SFTP_PORT=22
SFTP_USERNAME=your-username
SFTP_PRIVATE_KEY_PATH=/path/to/your/private_key  # or use password
SFTP_PASSWORD=your-password  # Set if not using key
REMOTE_BASE_PATH=/backup/frigate
DAYS_TO_KEEP=7
BACKUP_SCHEDULE=0 0 * * *  # Cron schedule for backup (default: daily at midnight)
LOG_FILE=/logs/sync.csv  # CSV run log: timestamp, success/failure, files uploaded/removed, error info
USE_FTP=false  # Set to true to upload over plain FTP instead of SFTP
```

Notes:

- `WG_CONFIG_PATH` defaults to `/etc/wireguard/wg0.conf` in the image — mount
  your config there and you don't need to set it.
- The paths (`WG_CONFIG_PATH`, `LOCAL_RECORDINGS_PATH`, `SFTP_PRIVATE_KEY_PATH`)
  are paths **inside the container**, so each needs a matching volume mount.
- `BACKUP_SCHEDULE` can also be passed as the container command
  (`docker run ... image "*/30 * * * *"`), which overrides the env var.
- **FTP mode** (`USE_FTP=true`): uses the same `SFTP_HOST` / `SFTP_PORT` /
  `SFTP_USERNAME` / `SFTP_PASSWORD` variables, but `SFTP_PORT` defaults to 21
  and `SFTP_PASSWORD` is required — key auth is SFTP-only. Plain FTP is
  unencrypted, but all traffic rides the WireGuard tunnel, so it stays
  encrypted on the wire end to end. If the FTP server doesn't support the
  `MFMT` command, uploaded files keep their upload time as mtime, so the
  `DAYS_TO_KEEP` retention counts from upload day instead of recording day.

## Running

```bash
docker run -d \
  --name wg-frigate-back \
  --cap-add NET_ADMIN \
  --sysctl net.ipv4.conf.all.src_valid_mark=1 \
  --restart unless-stopped \
  -v /path/to/wg0.conf:/etc/wireguard/wg0.conf:ro \
  -v /path/to/frigate/recordings:/recordings:ro \
  -v /path/to/your/private_key:/keys/sftp_key:ro \
  -v /path/to/logs:/logs \
  -e LOCAL_RECORDINGS_PATH=/recordings \
  -e SFTP_HOST=sftp.example.com \
  -e SFTP_PORT=22 \
  -e SFTP_USERNAME=your-username \
  -e SFTP_PRIVATE_KEY_PATH=/keys/sftp_key \
  -e REMOTE_BASE_PATH=/backup/frigate \
  -e DAYS_TO_KEEP=7 \
  -e BACKUP_SCHEDULE="0 0 * * *" \
  -e LOG_FILE=/logs/sync.csv \
  your-dockerhub-user/wg-frigate-back:latest
```

Using a password instead of a key: drop the key mount and
`SFTP_PRIVATE_KEY_PATH`, and set `-e SFTP_PASSWORD=your-password`.

Or with docker compose:

```yaml
services:
  wg-frigate-back:
    image: your-dockerhub-user/wg-frigate-back:latest
    cap_add:
      - NET_ADMIN
    sysctls:
      net.ipv4.conf.all.src_valid_mark: 1
    restart: unless-stopped
    volumes:
      - /path/to/wg0.conf:/etc/wireguard/wg0.conf:ro
      - /path/to/frigate/recordings:/recordings:ro
      - /path/to/your/private_key:/keys/sftp_key:ro
      - /path/to/logs:/logs
    environment:
      LOCAL_RECORDINGS_PATH: /recordings
      SFTP_HOST: sftp.example.com
      SFTP_PORT: "22"
      SFTP_USERNAME: your-username
      SFTP_PRIVATE_KEY_PATH: /keys/sftp_key
      REMOTE_BASE_PATH: /backup/frigate
      DAYS_TO_KEEP: "7"
      BACKUP_SCHEDULE: "0 0 * * *"
      LOG_FILE: /logs/sync.csv
```

Check on it with:

```bash
docker logs -f wg-frigate-back   # sync output + cron activity
docker exec wg-frigate-back wg show   # tunnel status
```

## Building

```bash
make build            # builds with Python 3.11 (make build X=12 for 3.12)
# or directly:
docker build --build-arg PYTHON_VERSION=3.11 -f DockerFile -t wg-frigate-back .
```

## CI/CD

[.github/workflows/deploy.yml](.github/workflows/deploy.yml) builds and pushes
the image to Docker Hub on every push to `main` (tags: `latest` and the Python
version). It needs two repository secrets: `DOCKER_USERNAME` and
`DOCKER_PASSWORD` (use a Docker Hub access token, not your password).
