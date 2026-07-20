# Fakturace – Server Setup Runbook

> Ubuntu 22.04 / 24.04 · Static IP · Nginx · Gunicorn · Python 3.12

---

## Prerequisites

| What | Value |
|---|---|
| Project directory on server | `/weby/faktury.tmcm.cz/app` |
| Log directory | `/weby/faktury.tmcm.cz/logs` |
| Backup directory | `/data3/BACKUPS/faktury_app` |
| System user running the app | `faktury` |
| Python version | 3.12 |
| Service name | `faktury` |

---

## Step 1 — System Dependencies

Run once on the fresh server as root:

```bash
sudo bash /weby/faktury.tmcm.cz/app/deploy/system-deps.sh
```

Installs: Python 3.12, WeasyPrint native libs (Pango, Cairo, Fontconfig),
Nginx, Certbot, and the `faktury` system user.

---

## Step 2 — Deploy Project Files

Run from your **development machine**:

```bash
rsync -avz --exclude='.venv' --exclude='*.db' --exclude='.tools' --exclude='tests' \
  /path/to/2026-04-faktury/ faktury@YOUR_SERVER_IP:/weby/faktury.tmcm.cz/app/

sudo chown -R faktury:faktury /weby/faktury.tmcm.cz/app
sudo chmod 750 /weby/faktury.tmcm.cz/app
```

Create the log and backup directories (systemd will **not** create these for you)
and hand them to the `faktury` user:

```bash
sudo mkdir -p /weby/faktury.tmcm.cz/logs /data3/BACKUPS/faktury_app
sudo chown faktury:faktury /weby/faktury.tmcm.cz/logs /data3/BACKUPS/faktury_app
```

> The app's Gunicorn access/error logs are written to `/weby/faktury.tmcm.cz/logs/`,
> so that directory must exist and be writable before the service starts.

---

## Step 3 — Create the venv and Install Dependencies

```bash
sudo -u faktury bash -c "
  cd /weby/faktury.tmcm.cz/app
  python3 -m venv .venv   # 3.11+ (Debian 13 ships 3.13; deadsnakes not needed)
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install gunicorn
  .venv/bin/pip install -r requirements.txt
"
```

---

## Step 4 — Configure the `.env` File

```bash
sudo -u faktury cp /weby/faktury.tmcm.cz/app/deploy/.env.example /weby/faktury.tmcm.cz/app/.env
sudo chmod 600 /weby/faktury.tmcm.cz/app/.env   # readable only by the faktury user
```

Generate a cryptographically secure `SECRET_KEY`:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Open `.env` and fill in all values:

```bash
sudo -u faktury nano /weby/faktury.tmcm.cz/app/.env
```

Key variables to set:

| Variable | Description |
|---|---|
| `APP_ENV` | Set to `production` |
| `SECRET_KEY` | Paste the output of the command above |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Session length in minutes (default `480` = 8 h) |

> **Supplier / business details are NOT set here.** They live in the per-user
> `SupplierProfile` DB table and are entered through the web UI on the **Účet**
> page after you log in (see Step 5). The legacy `SUPPLIER_*` environment
> variables are no longer read by the app.

---

## Step 5 — First-Run: Initialise Database & Admin User

```bash
sudo -u faktury bash -c "
  cd /weby/faktury.tmcm.cz/app
  .venv/bin/python -m app.init_db
"
```

This creates the master admin user (username: **`master`**) — that is the
account you log in with. You will be prompted to set its password
interactively.  
Alternatively, pre-set it via environment variable (min 12 chars):

```bash
sudo -u faktury bash -c "
  cd /weby/faktury.tmcm.cz/app
  ADMIN_PASSWORD='your-secure-password' .venv/bin/python -m app.init_db
"
```

### First-Run Checklist

- [ ] `faktury.db` exists at `/weby/faktury.tmcm.cz/app/faktury.db`
- [ ] `init_db` output shows **"Master user created successfully."**
- [ ] Quick local smoke-test (optional):
  ```bash
  sudo -u faktury bash -c "cd /weby/faktury.tmcm.cz/app && .venv/bin/uvicorn app.main:app --port 8000"
  # Visit http://localhost:8000 in an SSH tunnel to confirm the login page loads
  ```

> **After first login:** open the **Účet** page and fill in your supplier
> details (name, IČO, DIČ, bank account, e-mail, logo/stamp). These are stored
> per-user in the database and are what appear on generated invoice PDFs.

---

## Step 6 — Install and Start the Systemd Service

```bash
sudo cp /weby/faktury.tmcm.cz/app/deploy/faktury.service /etc/systemd/system/faktury.service
sudo systemctl daemon-reload
sudo systemctl enable faktury    # auto-start on server reboot
sudo systemctl start faktury
```

Verify it is running:

```bash
sudo systemctl status faktury
sudo journalctl -u faktury -f    # follow systemd-level logs
tail -f /weby/faktury.tmcm.cz/logs/error.log   # Gunicorn error log
```

### Grant Nginx access to the Gunicorn socket

Gunicorn binds a Unix socket at `/run/faktury/gunicorn.sock`, owned
`faktury:faktury`. Nginx runs as its **own** user (`www-data` on Debian/Ubuntu),
so it cannot connect to that socket until it is a member of the `faktury` group.
Skip this and Nginx fails every request with
`connect() … failed (13: Permission denied)` in Step 7.

```bash
# Confirm the Nginx worker user (usually www-data):
grep -E '^\s*user' /etc/nginx/nginx.conf

# Add it to the faktury group:
sudo usermod -aG faktury www-data
```

This works together with two settings already in `faktury.service`:
`--umask 007` (so the socket is created `0770` = group-readable/writable) and
`RuntimeDirectoryMode=0750` (so group members can traverse `/run/faktury`).

> **Group membership only affects processes started afterward.** You must fully
> **restart** Nginx later (`systemctl restart nginx`, not `reload`) for its
> workers to pick up the new group — a plain reload keeps the old group set.
> Step 7 restarts Nginx anyway.

---

## Step 7 — Configure Nginx

The config is already pinned to **`faktury.tmcm.cz`** — no placeholder editing needed.

Copy the config and enable the site:

```bash
sudo cp /weby/faktury.tmcm.cz/app/deploy/nginx.conf /etc/nginx/sites-available/faktury

sudo ln -s /etc/nginx/sites-available/faktury /etc/nginx/sites-enabled/faktury
sudo rm -f /etc/nginx/sites-enabled/default   # remove the default placeholder (single-site servers only)
```

> **Chicken-and-egg:** the HTTPS server block references the Let's Encrypt cert
> at `/etc/letsencrypt/live/faktury.tmcm.cz/`, which does **not exist yet**. So
> a plain `sudo nginx -t` now fails with:
> `cannot load certificate "…/fullchain.pem": … No such file or directory`.
> And `certbot --nginx` can't help, because it needs the config to load first.
> Break the cycle with the three-step bootstrap below.

### Obtain a Free SSL Certificate (Let's Encrypt)

**Step 7a — temporarily disable the HTTPS block.** In
`/etc/nginx/sites-available/faktury`, comment out the entire
`# ── HTTPS (main) ──` `server { … }` block. Leave the **port-80** server block
intact — it serves the ACME challenge from `/var/www/html`. Then reload:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

**Step 7b — obtain the cert via the webroot** (this does **not** touch your
Nginx config, so the hand-tuned HTTPS block is preserved verbatim):

```bash
sudo certbot certonly --webroot -w /var/www/html -d faktury.tmcm.cz
```

`certonly --webroot` is preferred over `certbot --nginx` here — especially on a
**shared server hosting other sites** (e.g. an existing `tmcm.cz`) — because it
only drops the cert files in place and never rewrites any site's server blocks.
It also installs the auto-renewal timer; renewals reuse the same port-80 ACME
path and are never redirected to HTTPS.

**Step 7c — restore the HTTPS block and reload.** Simplest is to re-copy the
config (this restores the 443 block *and* keeps the modern `http2 on;` syntax
that's already in `nginx.conf`):

```bash
sudo cp /weby/faktury.tmcm.cz/app/deploy/nginx.conf /etc/nginx/sites-available/faktury
sudo nginx -t && sudo systemctl restart nginx
```

Now the cert exists, so `nginx -t` passes and HTTPS comes up. (Use `restart`,
not `reload`, so Nginx's workers pick up the `faktury` group added in Step 6 and
can reach the Gunicorn socket.)

> **Harmless warnings:** on a shared server you may still see
> `protocol options redefined for [::]:443 in <other-site>` — that comes from
> the *other* site's config sharing the `:443` listener, not ours, and is safe
> to ignore. (Our config already uses the non-deprecated `http2 on;` directive
> rather than `listen … http2`.)

> **No domain? Using a static IP only?**  
> Use a self-signed certificate instead:
> ```bash
> sudo openssl req -x509 -nodes -days 730 -newkey rsa:4096 \
>   -keyout /etc/ssl/private/faktury-selfsigned.key \
>   -out /etc/ssl/certs/faktury-selfsigned.crt
> ```
> Then update the two `ssl_certificate` lines in `nginx.conf` accordingly, and
> skip Steps 7a–7c (there's no ACME challenge to run).

---

## Step 8 — Schedule Automated Backups (systemd timer)

Backups run via a **systemd timer**, not cron — so a backup missed while the
machine was off (`Persistent=true`) is run on next boot, and all output lands in
the journal. The backup bundles **the SQLite DB + all uploaded files** into one
compressed, SHA-256-checksummed archive (`faktury_<timestamp>.tar.gz` plus a
`.sha256`) in `/data3/BACKUPS/faktury_app`, pruning anything older than 90 days.

Install and enable the timer:

```bash
sudo cp /weby/faktury.tmcm.cz/app/deploy/faktury-backup.service /etc/systemd/system/
sudo cp /weby/faktury.tmcm.cz/app/deploy/faktury-backup.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now faktury-backup.timer
```

Verify the schedule and run one immediately to confirm it works:

```bash
systemctl list-timers faktury-backup.timer       # shows NEXT / LAST run
sudo systemctl start faktury-backup.service      # run a backup right now
journalctl -u faktury-backup.service --since today
```

A successful run logs `Backup created: … checksum verified.` and leaves two
files in `/data3/BACKUPS/faktury_app`.

> **Change the schedule** by editing the `OnCalendar=` line in
> `faktury-backup.timer` (default `Sun *-*-* 02:00:00` = weekly, Sunday 02:00),
> then `sudo systemctl daemon-reload && sudo systemctl restart faktury-backup.timer`.
> Validate any new expression with `systemd-analyze calendar 'Sun *-*-* 02:00:00'`.

### Verifying / restoring a backup

```bash
cd /data3/BACKUPS/faktury_app
sha256sum -c faktury_<timestamp>.tar.gz.sha256   # integrity check
tar -xzf faktury_<timestamp>.tar.gz              # → faktury.db + uploads/
```

To restore, stop the app, drop `faktury.db` back at `/weby/faktury.tmcm.cz/app/`
and `uploads/` under `/weby/faktury.tmcm.cz/app/app/`, fix ownership
(`chown -R faktury:faktury`), then start the app.

---

## Useful Day-to-Day Commands

| Task | Command |
|---|---|
| Restart the app | `sudo systemctl restart faktury` |
| View app (systemd) logs | `sudo journalctl -u faktury -f` |
| View Gunicorn error log | `tail -f /weby/faktury.tmcm.cz/logs/error.log` |
| Reload Nginx config | `sudo nginx -t && sudo systemctl reload nginx` |
| Renew SSL certificate | `sudo certbot renew --dry-run` |
| Run a backup now | `sudo systemctl start faktury-backup.service` |
| When is the next backup? | `systemctl list-timers faktury-backup.timer` |
| View backup logs | `sudo journalctl -u faktury-backup.service` |
| Deploy code update | See "Deploying a code update" below |

---

## Deploying a Code Update

The project dir is owned `faktury:faktury` (mode 750), so an unprivileged SSH
login (e.g. `tomas`) **cannot** rsync straight into it — you'll get
`change_dir … failed: Permission denied (13)`. Stage into your own home dir,
then move it into place with sudo:

```bash
# 1. From your dev machine — push to a dir your SSH user owns:
rsync -avz --filter=':- .gitignore' --exclude='.claude' --exclude='tests' \
  ./ home:/home/tomas/faktury-stage/

# 2. On the server (ssh home) — sync into place and restore ownership:
sudo rsync -a /home/tomas/faktury-stage/ /weby/faktury.tmcm.cz/app/
sudo chown -R faktury:faktury /weby/faktury.tmcm.cz/app

# 3. Restart the app (always) and reload nginx (only if deploy/nginx.conf changed):
sudo systemctl restart faktury
sudo cp /weby/faktury.tmcm.cz/app/deploy/nginx.conf /etc/nginx/sites-available/faktury
sudo nginx -t && sudo systemctl reload nginx
```

Notes:
- `--filter=':- .gitignore'` makes rsync honor `.gitignore`, so your dev `.env`,
  `*.db`, `.venv`, `.tools`, `__pycache__`, etc. are never pushed — it will
  **not** clobber the server's `.env` or database. `.claude`/`tests` aren't
  gitignored, hence the explicit `--exclude`s.
- Do **not** add `--delete` — it would wipe the server's `faktury.db` and
  `app/uploads/` (they don't exist in your dev tree).
- If a migration was added, `create_db_and_tables()` runs `alembic upgrade head`
  on startup, so the restart in step 3 applies it.

---

## Security Notes

- `.env` must be `chmod 600` and owned by `faktury` — nobody else can read it.
- `faktury.db` lives in `/weby/faktury.tmcm.cz/app/` — Nginx is configured to deny `/.db` requests.
- Change the admin password via the API if you ever suspect it has been compromised:
  ```bash
  sudo -u faktury bash -c "
    cd /weby/faktury.tmcm.cz/app
    .venv/bin/python - <<'EOF'
  from app.database import engine
  from app.models import User
  from app.auth import get_password_hash
  from sqlmodel import Session, select
  with Session(engine) as s:
      u = s.exec(select(User).where(User.username=='master')).first()
      u.hashed_password = get_password_hash('NEW_PASSWORD_HERE')
      u.token_version += 1   # revoke all outstanding sessions
      s.add(u); s.commit(); print('Password updated.')
  EOF
  "
  ```
