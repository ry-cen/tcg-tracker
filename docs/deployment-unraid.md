# Dev deployment on Unraid

Every push to `dev` that passes the verification gate deploys PrizeCheck to an Unraid server. The app is reachable only on your Tailscale tailnet, at an HTTPS address such as `https://unraid.<tailnet>.ts.net`. There's no port forwarding, Funnel, or public reverse proxy.

This guide covers the dev environment. A future production environment reuses the same images, `compose.deploy.yml` and deploy script, but with its own project name, data directory, port, secrets, GitHub environment and SSH key (see [Adding production later](#adding-production-later)).

## How it works

```
push to dev
  └─ GitHub Actions: Deploy dev (.github/workflows/deploy-dev.yml)
       ├─ verify   the fast tier of the gate, split over parallel jobs (.github/workflows/verify.yml)
       ├─ images   build api, auth and web; push to GHCR as :<commit> (plus a moving :dev pointer)
       └─ deploy   join the tailnet as tag:ci (ephemeral) → SSH to Unraid with a forced command
                     └─ prizecheck-deploy.sh deploy <commit> <host-file digest>
                          ├─ pull the three :<commit> images (one-time GHCR login, deleted afterwards)
                          ├─ start PostgreSQL and list pending migrations
                          ├─ if any are pending: pg_dump, stop the app, run scripts/migrate.py
                          ├─ new install only: initialize the public projection store
                          ├─ start api → auth → web and wait for their health checks
                          ├─ smoke checks over loopback (below)
                          └─ pass: record the release · fail: bring the previous release back, fail the job
```

Containers (`compose.deploy.yml`, Compose project `prizecheck-dev`):

| Service | Image                                     | Role                                                                                    | Network reach                                     |
| ------- | ----------------------------------------- | --------------------------------------------------------------------------------------- | ------------------------------------------------- |
| `web`   | `ghcr.io/ry-cen/prizecheck-web:<commit>`  | nginx (non-root) serving the Vite build; proxies `/auth/*` and `/api/*` to `auth`       | The only published port: `127.0.0.1:8088`         |
| `auth`  | `ghcr.io/ry-cen/prizecheck-auth:<commit>` | The unchanged Better Auth gateway (`auth/src`, compiled to JS), signs requests to `api` | Internal; outbound allowed for an HTTP mail relay |
| `api`   | `ghcr.io/ry-cen/prizecheck-api:<commit>`  | FastAPI under uvicorn: one process, no reload, no access log                            | Internal; outbound allowed for TCGdex imports     |
| `db`    | `postgres:16-alpine`                      | PostgreSQL                                                                              | Internal-only network, never published            |
| `tools` | the `api` image                           | One-off runs: migrations, store initialization, `bind_owner.py`                         | Started only by the deploy script or operator     |

Containers find each other by service name (`db`, `api`, `auth`). The browser talks to one origin, as it does in local development. The session, request-signing and public-read paths are the existing code, unchanged.

Persistent data lives on the host under `PRIZECHECK_DATA_DIR`:

| Directory        | Contents                                                                | Owner (uid) |
| ---------------- | ----------------------------------------------------------------------- | ----------- |
| `postgres/`      | PostgreSQL cluster                                                      | postgres    |
| `public/`        | Public projection and shared records store (published decks, discovery) | 10001       |
| `price-runners/` | Price-runner lock files and host id                                     | 10001       |
| `mail/`          | `codes.jsonl`, the sign-in and approval codes while `MAIL_MODE=file`    | 1000        |

Deploy state lives under `PRIZECHECK_HOME` (`state/current`, `state/previous`, `state/history.log`, `backups/`).

### What the repository provides and what you set up

| Implemented in this repository                                                         | One-time manual steps (this guide)                                      |
| -------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `deploy/docker/{api,auth,web}.Dockerfile`, `deploy/docker/nginx.conf`, `.dockerignore` | Install Docker Compose on Unraid (step 2)                               |
| `compose.deploy.yml`                                                                   | Create the directories and the env file on Unraid (steps 3 to 5)        |
| `deploy/unraid/prizecheck-deploy.sh` (deploy, rollback, backup, operator helpers)      | Generate the deploy SSH key and install it as a forced command (step 6) |
| `deploy/unraid/prizecheck.env.example`                                                 | Tailscale tag, policy, OAuth client and Serve (steps 7 and 9)           |
| `.github/workflows/deploy-dev.yml`; `verify.yml` made reusable                         | GitHub environment, secrets and variables (step 8)                      |
| Health checks, smoke checks, pre-migration backups, automatic rollback                 | First deploy, owner binding, catalog import (steps 10 and 11)           |

Nothing in this guide has been run against your server yet. The images were not built locally (there was no Docker on the development machine), so the first run of the `images` job is also their first build.

## Behaviour to know about

- **Sign-in codes go to a file, not to email.** No email provider has been selected yet (see [signup readiness](signup-readiness.md)), and the gateway refuses console and file mail under `NODE_ENV=production`. So dev runs with `NODE_ENV=development` and `MAIL_MODE=file`. Codes are written with mode 0600 to `mail/codes.jsonl` and never to container logs; read them with `prizecheck-deploy.sh sign-in-code`. `SIGNUP_MODE` is fixed to `owner-only`. Production must use `MAIL_MODE=http` with `NODE_ENV=production`.
- **Deploys with pending migrations cause downtime.** The deploy script stops the old release before migrating, so the old application never serves against a half-migrated schema (see the migration 026 notes in [release](operations/release.md)). Deploys without pending migrations replace containers with only a few seconds' interruption.
- **A deploy interrupts running imports.** Catalog imports and price runners are child processes of the `api` container, so recreating it ends them. Avoid deploying during a full catalog pull. A failed or interrupted pull keeps the previous successful catalog; check **Owner controls** after the deploy and start the pull again. Price-runner locks and the host id persist in `price-runners/`, so the next launch can prove the old runner has ended and reclaim its slot.
- **Migrations are never reversed.** Rollback starts the previous images against the current schema. The repository expects each migration to stay compatible with the previous release ([release](operations/release.md) has an opt-in check), but this pipeline doesn't run that check, and no check can cover every data state. To return to an older schema, restore a pre-migration dump (see [Restoring the database](#restoring-the-database)).
- **One rate-limit bucket.** `TRUSTED_PROXY_IPS` is empty, so the gateway ignores forwarding headers and sees every browser as the `web` container. The per-IP limits (code sends, public reads) are shared by everyone using dev. That's fine for a single owner.
- **No URLs in logs.** Published-deck URLs are capability links. nginx and uvicorn access logs are off, and nginx logs only its own critical failures. Container logs rotate at 3 × 10 MB per service.

## 1. Prepare the Unraid host

Requirements:

- Unraid 6.12 or later with Docker enabled.
- A pool for app data. These examples use `/mnt/cache`; substitute your pool name.
- Tailscale running on the host (step 9 checks this).
- SSH enabled: **Settings → Management Access → Use SSH**.

Choose the install directory. The examples use:

```bash
export PRIZECHECK_HOME=/mnt/cache/appdata/prizecheck-dev
```

Use a pool path such as `/mnt/cache/...`, not the `/mnt/user/...` FUSE path. PostgreSQL, the projection store's atomic renames and the price-runner `flock` locks need a real filesystem. If your appdata share uses Unraid's Exclusive mode, `/mnt/user/appdata` bypasses FUSE and also works; a plain cache-only share still goes through FUSE.

## 2. Install Docker Compose

Check on the Unraid terminal:

```bash
docker compose version
```

If that fails, install the **Docker Compose Manager** plugin from **Apps** (Community Applications). It installs Compose v2, which provides `docker compose`. The deploy script also accepts a standalone `docker-compose` v2. It needs Compose 2.20 or later for `up --wait --wait-timeout`.

## 3. Create the persistent directories and install the host files

From your workstation, in a checkout of the `dev` commit you're going to deploy, copy the two host files. Replace `unraid` with the host's tailnet name or IP:

```bash
ssh root@unraid 'mkdir -p /mnt/cache/appdata/prizecheck-dev/bin'
```

```bash
scp compose.deploy.yml root@unraid:/mnt/cache/appdata/prizecheck-dev/compose.deploy.yml
```

```bash
scp deploy/unraid/prizecheck-deploy.sh deploy/unraid/prizecheck.env.example root@unraid:/mnt/cache/appdata/prizecheck-dev/bin/
```

The script finds its home as the parent of its `bin/` directory, so keep that layout. Steps 4 and 5 finish the setup.

## 4. Authenticate Unraid with GHCR

The workflow needs no stored registry credential on Unraid. Each deploy pipes the job's short-lived `GITHUB_TOKEN` (`packages: read` and `contents: read`) over SSH. The host logs in to a temporary Docker config, pulls, and deletes the config.

The packages `prizecheck-api`, `prizecheck-auth` and `prizecheck-web` are created private and linked to the repository by the first `images` run. If a deploy fails with `Image pull failed`, open each package on GitHub, go to **Package settings → Manage Actions access**, and give `ry-cen/prizecheck` **Read**.

Optionally, for manual pulls (deploying a specific older commit by hand), store a persistent read-only login. Use a classic personal access token with only the `read:packages` scope:

```bash
DOCKER_CONFIG=/mnt/cache/appdata/prizecheck-dev/docker-config docker login ghcr.io -u YOUR_GITHUB_USER
```

The script uses this config only when CI hasn't supplied a token. Don't log in as plain `root`: `/root` lives in RAM on Unraid and is lost on reboot.

## 5. Configure the deployment environment file

On Unraid:

```bash
cd /mnt/cache/appdata/prizecheck-dev && cp bin/prizecheck.env.example prizecheck.env && chmod 600 prizecheck.env
```

Generate three distinct secrets, one per command run:

```bash
openssl rand -hex 32
```

Edit `prizecheck.env` (for example `nano prizecheck.env`):

| Variable               | Value                                                                                                            |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `PRIZECHECK_DATA_DIR`  | `/mnt/cache/appdata/prizecheck-dev/data`                                                                         |
| `PRIZECHECK_WEB_PORT`  | A free loopback port. Default `8088`; check with `ss -ltn \| grep 8088`                                          |
| `PUBLIC_ORIGIN`        | The exact URL from step 9, e.g. `https://unraid.tail1234.ts.net`. Include `:8443` if you serve on a non-443 port |
| `OWNER_EMAIL`          | The address you sign in with                                                                                     |
| `POSTGRES_PASSWORD`    | A secret (letters and digits only, at least 32 characters; it's embedded in a connection URL)                    |
| `BETTER_AUTH_SECRET`   | A secret                                                                                                         |
| `INTERNAL_AUTH_SECRET` | A secret                                                                                                         |
| `MAIL_MODE`            | Keep `file`, unless you have an HTTP mail relay (then set the three `MAIL_*` relay values)                       |

These values stay in this file only. They aren't in the repository or GitHub, and the deploy script reads individual keys without printing them. Don't reuse them for production.

Then create the data directories with the ownership the non-root containers need:

```bash
bash /mnt/cache/appdata/prizecheck-dev/bin/prizecheck-deploy.sh setup
```

## 6. Generate the SSH deployment key

On your workstation, create a key used only for this:

```bash
ssh-keygen -t ed25519 -N '' -C prizecheck-dev-deploy -f prizecheck-dev-deploy
```

Install the public key on Unraid as a **forced command**. With `restrict`, the key can't open a shell, forward ports or allocate a terminal. It can only ask the deploy script for `deploy <commit> <digest>`, `rollback` or `status`; anything else is refused. Add this single line (with your public key) to root's authorized keys:

```text
restrict,command="/bin/bash /mnt/cache/appdata/prizecheck-dev/bin/prizecheck-deploy.sh --ssh-gate" ssh-ed25519 AAAA...your-key... prizecheck-dev-deploy
```

On Unraid the root filesystem is in RAM, so the line must go where Unraid persists keys. On recent releases you can paste it into **Users → root → SSH authorized keys**, which writes `/boot/config/ssh/root/authorized_keys`. Check what your version does with `ls -la /root/.ssh` and confirm the line survives a reboot.

Record the host key for CI's verification. On the Unraid console, read its fingerprint:

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

From a tailnet machine, fetch the key using the **same address CI will use** (`UNRAID_SSH_HOST` in step 8). The tailnet IP is the most stable choice:

```bash
ssh-keyscan -t ed25519 100.x.y.z > unraid_known_hosts && ssh-keygen -lf unraid_known_hosts
```

The two fingerprints must match. The content of `unraid_known_hosts` becomes `UNRAID_SSH_KNOWN_HOSTS`. CI connects with `StrictHostKeyChecking=yes`. Unraid keeps its host keys on the flash drive, so they survive reboots.

Delete the private key file from your workstation after storing it in GitHub (step 8).

If Tailscale SSH is enabled for the Unraid node (`tailscale set --ssh`), tailscaled answers port 22 on the tailnet address itself and never consults `authorized_keys`. The policy in step 7 doesn't grant `tag:ci` Tailscale SSH, so the deploy is denied rather than let in. Keep Tailscale SSH off on this node, so CI reaches Unraid's OpenSSH.

## 7. Configure Tailscale for CI

In the [Tailscale admin console](https://login.tailscale.com/admin):

1. **Access controls**: make CI a tag that may reach only SSH on Unraid. Merge this into your policy. Use the Unraid tailnet IP from `tailscale ip -4` on the host.

   ```jsonc
   {
     "tagOwners": {
       "tag:ci": ["autogroup:admin"],
     },
     "hosts": {
       "unraid": "100.x.y.z",
     },
     "grants": [
       // Your own devices keep their access. Do NOT keep a rule with "src": ["*"]:
       // it would give tag:ci (and every tagged device) access to everything.
       { "src": ["autogroup:member"], "dst": ["*"], "ip": ["*"] },
       // CI runners: SSH to Unraid only.
       { "src": ["tag:ci"], "dst": ["unraid"], "ip": ["tcp:22"] },
     ],
   }
   ```

   If your policy still has the default allow-all rule, replace it as shown. Tailscale's policy preview lets you check that `tag:ci` reaches only `unraid:22`.

2. **Trust credentials** (Settings → OAuth clients): create an OAuth client with the **Auth Keys → Write** scope, limited to tag `tag:ci`. Copy the client ID and secret into GitHub (step 8). The action uses them to mint a short-lived, ephemeral, pre-authorized node for each run, which disappears when the run ends. The `tailscale/github-action` also supports OIDC workload identity federation (`audience`), which needs no stored secret; switch to it later if you want.

3. **DNS**: enable **MagicDNS** and **HTTPS Certificates** (needed by Serve in step 9). Certificate issuance publishes the machine name (for example `unraid.tail1234.ts.net`) in public Certificate Transparency logs. The app itself stays unreachable from the internet.

## 8. Configure GitHub

In `ry-cen/prizecheck` → **Settings**:

1. **Environments → New environment `dev`**: under deployment branches, allow only `dev`. Optionally add yourself as a required reviewer for manual approval of each deploy.
2. **Environment secrets** (in `dev`):

   | Secret                   | Value                                           |
   | ------------------------ | ----------------------------------------------- |
   | `TS_OAUTH_CLIENT_ID`     | Tailscale OAuth client ID (step 7)              |
   | `TS_OAUTH_SECRET`        | Tailscale OAuth client secret (step 7)          |
   | `UNRAID_SSH_PRIVATE_KEY` | The content of `prizecheck-dev-deploy` (step 6) |

3. **Environment variables** (in `dev`):

   | Variable                 | Value                                                  |
   | ------------------------ | ------------------------------------------------------ |
   | `UNRAID_SSH_HOST`        | The Unraid tailnet IP (or name) used for `ssh-keyscan` |
   | `UNRAID_SSH_USER`        | `root` (the default when unset)                        |
   | `UNRAID_SSH_KNOWN_HOSTS` | The content of `unraid_known_hosts` (step 6)           |

4. **Repository variables** (Secrets and variables → Actions → Variables). These are read before the job enters the environment, so they must be repository-level:

   | Variable             | Value                                                                               |
   | -------------------- | ----------------------------------------------------------------------------------- |
   | `DEV_PUBLIC_URL`     | The `PUBLIC_ORIGIN` from step 5 (shown as the deployment link)                      |
   | `DEV_DEPLOY_ENABLED` | `true`. Set this last; until then `verify` and `images` run but `deploy` is skipped |

The workflow uses the job's own `GITHUB_TOKEN` for GHCR. Grants are per job: `contents: read` for `verify`, `images` and `deploy`, `packages: write` only for `images`, `packages: read` for `deploy`, and none for `rollback`. Third-party actions are pinned to commit SHAs.

## 9. Enable Tailscale Serve

First find out where Tailscale runs. On the Unraid terminal:

```bash
command -v tailscale && tailscale status --self
```

**If this prints the host's tailnet identity**, tailscaled runs in the host network namespace (the Unraid Tailscale plugin, or Unraid's built-in integration) and can reach the loopback-bound `web` port. Check what is already served, because the Unraid web UI may already be on 443:

```bash
tailscale serve status
```

If 443 is free:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8088
```

If 443 is taken, use another HTTPS port and put it in `PUBLIC_ORIGIN` (`https://unraid.<tailnet>.ts.net:8443`):

```bash
tailscale serve --bg --https=8443 http://127.0.0.1:8088
```

`--bg` stores the configuration, so it survives tailscaled restarts and reboots. Confirm that Funnel is off for this port (the app must not be public):

```bash
tailscale funnel status
```

**If `tailscale` isn't available on the host** and Tailscale runs in a container on a Docker bridge network, it can't see the host's loopback. Bind the web port to the Docker bridge gateway instead. That address is reachable from containers on the host, not from your LAN or the internet. In `prizecheck.env`:

```text
PRIZECHECK_WEB_BIND=172.17.0.1
```

Confirm the address with `ip -4 addr show docker0`. Then inside the Tailscale container:

```bash
tailscale serve --bg --https=443 http://172.17.0.1:8088
```

A Tailscale container started with `--network=host` behaves like the host case. A Tailscale container on a custom `br0` (macvlan/ipvlan) network can't reach `172.17.0.1`; run it with host networking instead. The deploy script accepts only `127.0.0.1` or docker0's own address for `PRIZECHECK_WEB_BIND` and refuses to deploy with anything else (for example `0.0.0.0` or a LAN address).

## 10. Perform the initial deployment

1. Confirm the host files match the commit you'll deploy. On Unraid:

   ```bash
   bash /mnt/cache/appdata/prizecheck-dev/bin/prizecheck-deploy.sh bundle-digest
   ```

   On your workstation, in the same commit:

   ```bash
   cat compose.deploy.yml deploy/unraid/prizecheck-deploy.sh | sha256sum
   ```

2. Trigger the workflow by pushing to `dev`, or run it manually: **Actions → Deploy dev → Run workflow → branch `dev`, operation `deploy`**. The fast tier of the verification gate runs first, split over parallel jobs (about 5 minutes), then the images are built and the deploy runs. CI runs only the fast tier; run the full gate locally with `npm run verify`, or on CI from **Actions → Verify → Run workflow** with **full** ticked.
3. On a new installation the script applies all migrations and initializes the public projection store. It initializes the store only when the store is empty **and** nothing has been deployed before. A missing store on an existing install stops the deploy instead of silently creating an empty one; see [public projections](public-projections.md) for recovery.

## 11. Verify the application

On Unraid:

```bash
bash /mnt/cache/appdata/prizecheck-dev/bin/prizecheck-deploy.sh status
```

```bash
curl -s http://127.0.0.1:8088/api/health
```

Use `172.17.0.1` instead of `127.0.0.1` if you set `PRIZECHECK_WEB_BIND` to the bridge address.

The deploy has already checked all of the following. A failure in any one fails the job and triggers the automatic rollback:

- every container reached `healthy`; `web` checks `/api/health` through nginx → gateway → API
- `GET /api/health` → 200, `GET /api/locations` without a session → 401, `GET /api/public/decks/unknown-smoke-token` → 404 (proves the projection store is readable)
- the served `index.html` is byte-identical to the one in the new `web` image, and every asset it references returns 200
- inside the `api` container, PostgreSQL is reachable and every migration file in the image is recorded in `schema_migration`
- each service runs the image tagged with the deployed commit

Then sign in from a tailnet device:

1. Open `PUBLIC_ORIGIN` and request a code for `OWNER_EMAIL`.
2. Read the code on Unraid:

   ```bash
   bash /mnt/cache/appdata/prizecheck-dev/bin/prizecheck-deploy.sh sign-in-code
   ```

3. After the first sign-in, bind the owner once. Find the account id:

   ```bash
   bash /mnt/cache/appdata/prizecheck-dev/bin/prizecheck-deploy.sh compose exec db psql -U prizecheck -d prizecheck -c 'select id, email from "user";'
   ```

   Then bind it:

   ```bash
   bash /mnt/cache/appdata/prizecheck-dev/bin/prizecheck-deploy.sh tool /app/scripts/bind_owner.py 'ID-FROM-THE-QUERY'
   ```

4. Refresh, open **Owner controls**, and start the full English catalog pull.

From a device that is **not** on the tailnet, the URL must not resolve or connect.

## Updating the host files

`compose.deploy.yml` and `prizecheck-deploy.sh` run on Unraid as root, so CI never ships them. A stolen deploy key can only deploy images from GHCR with the files you installed. CI sends the digest of the commit's copies, and the host refuses to deploy if its own files differ:

```text
ERROR: Host deployment files differ from commit ...; reinstall compose.deploy.yml and prizecheck-deploy.sh
```

When a commit changes either file, repeat the two `scp` commands in step 3 from that commit, then re-run the failed workflow run.

## Viewing logs

| What                      | Command (on Unraid)                                                                                            |
| ------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Deploy output             | The **Deploy on Unraid** step of the workflow run                                                              |
| Deploy history            | `cat /mnt/cache/appdata/prizecheck-dev/state/history.log`                                                      |
| Full deploy script output | `tail -n 200 /mnt/cache/appdata/prizecheck-dev/state/deploy.log` (kept even if the CI job lost its connection) |
| Application containers    | `bash /mnt/cache/appdata/prizecheck-dev/bin/prizecheck-deploy.sh compose logs -f --tail 200 api auth web`      |
| Database                  | `bash /mnt/cache/appdata/prizecheck-dev/bin/prizecheck-deploy.sh compose logs --tail 200 db`                   |
| Container state           | `bash /mnt/cache/appdata/prizecheck-dev/bin/prizecheck-deploy.sh status`                                       |

The application logs fixed operational messages, not request URLs, codes or credentials. The containers also appear on the Unraid **Docker** tab.

## Troubleshooting

| Symptom                                          | Cause and fix                                                                                                                                                                                                                                                         |
| ------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `deploy` job skipped                             | `DEV_DEPLOY_ENABLED` isn't the repository variable `true`, or a newer commit is now `dev`'s head (that run deploys)                                                                                                                                                   |
| Tailscale step fails                             | OAuth client lacks **Auth Keys: Write** or isn't allowed `tag:ci`; `tag:ci` missing from `tagOwners`                                                                                                                                                                  |
| `tailscale ping` or SSH times out                | The policy doesn't grant `tag:ci` → `unraid:22`, `UNRAID_SSH_HOST` is wrong, or SSH is disabled on Unraid                                                                                                                                                             |
| `Host key verification failed`                   | `UNRAID_SSH_KNOWN_HOSTS` doesn't match the address in `UNRAID_SSH_HOST`; rescan the same address (step 6). Never disable checking                                                                                                                                     |
| `Permission denied (publickey)`                  | The authorized-keys line didn't persist (check after reboot) or has a typo; the secret must contain the whole private key, including the BEGIN/END lines                                                                                                              |
| `Refused`                                        | Something other than `deploy`, `rollback` or `status` was sent through the forced command                                                                                                                                                                             |
| `Host deployment files differ`                   | See [Updating the host files](#updating-the-host-files)                                                                                                                                                                                                               |
| `Image pull failed`                              | The package doesn't grant the repository Read access (step 4)                                                                                                                                                                                                         |
| `Data directory ... is missing`                  | The array or pool isn't started, or `PRIZECHECK_DATA_DIR` is wrong. The deploy refuses to create data in the wrong place                                                                                                                                              |
| `Public projection store ... missing`            | An existing install lost `data/public`: restore it (`backup` archives it) or follow [public projections](public-projections.md). On a first install whose store initialization failed, run `prizecheck-deploy.sh init-store <commit>` on Unraid and re-run the deploy |
| `Migration failed`                               | `scripts/migrate.py` runs all pending migrations in one transaction, so nothing was committed and the previous release was restarted. Fix forward with a new commit                                                                                                   |
| `Services did not become healthy` / `Smoke: ...` | The log shows the last 50 lines of each service and the previous release is restored. Typical causes: a wrong `PUBLIC_ORIGIN`, a port already in use, an invalid `prizecheck.env` value                                                                               |
| Sign-in says the code was sent but none appears  | The address isn't `OWNER_EMAIL`, or the send limits were hit (one per minute, five per day). Check `sign-in-code`                                                                                                                                                     |
| Browser gets `403 Origin required`               | `PUBLIC_ORIGIN` doesn't exactly match the URL in the address bar (scheme, host and port)                                                                                                                                                                              |

## Rolling back

Don't cancel a workflow run while its **Deploy on Unraid** step is running. The host script keeps going after the connection drops (its output continues in `state/deploy.log`), but the job then reports nothing about the outcome. Check `status` and `deploy.log` on Unraid instead.

- **From GitHub:** Actions → **Deploy dev** → Run workflow → branch `dev`, operation `rollback`.
- **On Unraid:**

  ```bash
  bash /mnt/cache/appdata/prizecheck-dev/bin/prizecheck-deploy.sh rollback
  ```

Rollback starts the previous successful release's images (kept locally), runs the same health and smoke checks, and swaps `current` and `previous`. It does **not** reverse migrations. A failed deploy already rolls back automatically.

To deploy a specific older commit by hand, use the persistent GHCR login from step 4 and the host-file digest of that commit's files:

```bash
bash /mnt/cache/appdata/prizecheck-dev/bin/prizecheck-deploy.sh deploy FULL_40_CHAR_COMMIT "$(bash /mnt/cache/appdata/prizecheck-dev/bin/prizecheck-deploy.sh bundle-digest)"
```

### Restoring the database

Use this when an old release can't run against the new schema, or after data damage. It replaces the dev database with a dump, and anything written after the dump is lost. Run these on Unraid with `P=/mnt/cache/appdata/prizecheck-dev/bin/prizecheck-deploy.sh`:

1. Stop the app:

   ```bash
   bash $P compose stop api auth web
   ```

2. Recreate the empty database:

   ```bash
   bash $P compose exec -T db dropdb -U prizecheck prizecheck && bash $P compose exec -T db createdb -U prizecheck prizecheck
   ```

3. Restore the dump taken before the deploy you're undoing (`backups/<time>-pre-<commit>.dump`):

   ```bash
   bash $P compose exec -T db pg_restore -U prizecheck -d prizecheck --no-owner < /mnt/cache/appdata/prizecheck-dev/backups/FILE.dump
   ```

4. Start the release that matches that schema: `rollback` if it's the previous release, otherwise the manual `deploy` command above.

If publications changed after the dump, also restore the matching `public` archive, or follow the [public projection recovery](public-projections.md).

## Backing up persistent data

- **Automatic:** before applying migrations to an existing install, each deploy writes `backups/<time>-pre-<commit>.dump` (`pg_dump -Fc`) and keeps the newest ten pre-deploy dumps. Pre-deploy and manual dumps are pruned separately (ten of each), so daily manual backups never push out the pre-migration dump a restore needs.
- **On demand:** this dumps PostgreSQL and archives the public projection store into `backups/`:

  ```bash
  bash /mnt/cache/appdata/prizecheck-dev/bin/prizecheck-deploy.sh backup
  ```

- **Scheduled:** add that command as a daily job in the Unraid **User Scripts** plugin.
- **Off the server:** copy `backups/` and `prizecheck.env` to separate storage. Without the env file's secrets, existing sessions and the database password are lost. The backups contain private collection data; keep them out of any shared or public location.

Don't run `backup` while a deploy is migrating; the deploy lock prevents this when both use the script. For the application's own logical backup and restore drill, which also enforces the public/private storage separation, see [local recovery](operations/local-recovery.md).

## Adding production later

Copy this setup with separate values everywhere:

- a second `PRIZECHECK_HOME` (e.g. `/mnt/cache/appdata/prizecheck-prod`)
- `PRIZECHECK_PROJECT=prizecheck-prod`, another data directory and port, and new secrets
- `NODE_ENV=production` with `MAIL_MODE=http` (`PRIZECHECK_NODE_ENV=production` in its env file)
- a `prod` GitHub environment restricted to `main`, with required reviewers, a second deploy key whose forced command points at the prod script, and a `deploy-prod.yml` that deploys an already-built commit image rather than rebuilding

The release gate and launch checks in [release](operations/release.md) and [deployment boundaries](deployment-boundaries.md) still apply to production.
