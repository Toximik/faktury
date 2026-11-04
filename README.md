# Fakturace

A small self-hosted invoicing and expense tracker for Czech freelancers (OSVČ,
neplátci DPH). It issues Czech-format invoice PDFs with a QR payment code,
keeps track of expenses and estimates the yearly income tax and social/health
insurance.

The user interface and all business rules are Czech (IČO/DIČ, variabilní
symbol, Czech bank account format); the code and docs are in English.

## Features

- **Invoices (faktury)** – PDF in Czech format with a SPAYD QR code for
  payment, sequential variable symbol per year (`YYYYNNNN`), payment terms per
  client, mark as paid, duplicate for recurring invoices, custom accent colour,
  logo and stamp.
- **Credit notes (dobropisy)** – issued from an existing invoice, with their own
  number series (`YYYY9NNN`).
- **Expenses (náklady)** – with an optional scan (PDF/image) attached.
- **Contacts** – autofill name, address and DIČ from the Czech business register
  [ARES](https://ares.gov.cz) by IČO; IČO and bank accounts are checksum-validated,
  IBAN is computed.
- **Statistiky** – monthly income/expenses for a year, by issue or payment date,
  with CSV export.
- **Daně** – estimate of income tax and social and health insurance; picks the
  better of real expenses and the flat-rate allowance (paušál) automatically.
- **Multiple users** with strictly separated data; an admin can create, block
  and delete users, reset passwords, and export/restore a user's complete data
  as a single `.zip`.
- Issued documents never change: supplier and customer details are frozen into
  each invoice when it is created.

## Tech stack

Python · [FastAPI](https://fastapi.tiangolo.com) · SQLModel/SQLAlchemy ·
SQLite (any SQLAlchemy database via `DATABASE_URL`) · Alembic migrations ·
Jinja2 templates with self-hosted Tailwind CSS · WeasyPrint for PDFs ·
[qrplatba](https://pypi.org/project/qrplatba/) for SPAYD QR codes.
There is also a JSON API (`/api/contacts`, `/invoices`, `/expenses`,
`/export`), documented at `/docs` when not running in production mode.

## Running it

### Docker

```bash
docker build -t faktury .

mkdir -p data uploads backups
echo "APP_ENV=production" > .env
echo "SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> .env
# the container runs as UID 10001, so it must be able to write here
sudo chown 10001:10001 data uploads backups

# create the database and the admin user "master" (asks for a password, min. 12 chars)
docker run --rm -it --env-file .env -v ./data:/data faktury python -m app.init_db

docker run -d --name faktury --env-file .env -p 127.0.0.1:8000:8000 \
  -v ./data:/data -v ./uploads:/app/app/uploads -v ./backups:/backups faktury
```

Put it behind a reverse proxy with HTTPS (`deploy/nginx.conf` is an example);
the port is meant to be reachable only from localhost.

### Without Docker

[deploy/SERVER_SETUP.md](deploy/SERVER_SETUP.md) describes a plain Debian
setup with Gunicorn, systemd and Nginx, including the backup timer.

### Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.init_db              # creates faktury.db and the "master" user
.venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/python -m pytest tests/ -q         # test suite
```

WeasyPrint needs Pango system libraries; see `deploy/system-deps.sh`.
After changing Tailwind classes in templates, rebuild `app/static/css/tailwind.css`
with the Tailwind v3 standalone CLI (instructions in [CLAUDE.md](CLAUDE.md)).

## Configuration

Environment variables (template: [deploy/.env.example](deploy/.env.example)):

| Variable | Meaning |
|---|---|
| `SECRET_KEY` | Signs login tokens. **Required** in production. |
| `APP_ENV` | `production` turns off `/docs`, enables secure cookies and requires `SECRET_KEY`. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Login lifetime, default 480 (8 hours). |
| `DATABASE_URL` | Defaults to SQLite `faktury.db` in the project root. |

Schema migrations run automatically on start-up. Supplier details (your name,
address, bank account, logo, stamp) are set per user in the app under *Účet*.

## Backups

`deploy/backup.sh` makes a consistent copy of the SQLite database plus uploads
(used by the systemd timer in `deploy/`). Per-user export/import is available to
the admin in the app.

## Credits

Built with [Claude Code](https://claude.com/claude-code): the code, tests and
documentation were written by Claude (Anthropic's AI model), directed, reviewed
and used in production by [Toximik](https://github.com/Toximik).

Bundled third-party code: [flatpickr](https://flatpickr.js.org) 4.6.13 (MIT).

## License

[MIT](LICENSE)
