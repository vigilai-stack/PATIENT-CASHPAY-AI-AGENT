## **Deploy, AWS Lightsail**

Data note: runs entirely on synthetic data; no real patient or clinic data is used.

This is the deployment used for the live demo at https://cashpay.vigilaisolutions.com. It is a single Lightsail instance running a Docker Compose stack, with Caddy in front for automatic HTTPS.

#### **What it is**

- **One Lightsail instance**, running Docker Compose.
- **Services:** `api` (FastAPI, port 8000), `db` (Postgres 16), `fhir` (the FHIR stub, port 8090), and `caddy` (Caddy 2, ports 80/443).
- **Caddy** terminates HTTPS automatically for `$DOMAIN` and proxies to `api:8000`, with HSTS, `nosniff`, and a strict referrer policy (see `deploy/lightsail/Caddyfile`).
- The Lightsail overlay is `deploy/lightsail/docker-compose.lightsail.yml`. It removes the direct `api` port mapping (only Caddy is exposed), adds `restart: unless-stopped` to `api`, `db`, and `fhir`, and adds the `caddy` service with a `caddy_data` volume.

#### **Files**

| File | Purpose |
|---|---|
| `deploy/lightsail/docker-compose.lightsail.yml` | Compose overlay: Caddy + restart policy + no direct API port |
| `deploy/lightsail/Caddyfile` | Caddy config: `{$DOMAIN}`, gzip, security headers, `reverse_proxy api:8000` |
| `deploy/lightsail/lightsail.env.example` | Template for the server `.env`: copy it, fill it in, and never commit the filled-in copy |
| `deploy/lightsail/bootstrap.sh` | Runs **on** the Lightsail server; brings the stack up. It does not create any AWS resources. |

#### **Steps**

##### **1. Prepare the server `.env`**

On the Lightsail instance, in the repo root, copy the template and fill it in:

```bash
cp deploy/lightsail/lightsail.env.example .env
```

Values in the template (the OpenAI lines are optional):

```env
DOMAIN=demo.example.com
SESSION_SECRET=
PAYMENT_MODE=sandbox
PAYPAL_CLIENT_ID=
PAYPAL_CLIENT_SECRET=
PAYPAL_WEBHOOK_ID=
PAYPAL_BASE_URL=https://api-m.sandbox.paypal.com
LLM_MODE=mock
# Optional. Leave mock for offline tests. See docs/openai.md before setting openai.
# OPENAI_API_KEY=
# OPENAI_MODEL=gpt-6-luna
# OPENAI_TIMEOUT_SECONDS=8
# OPENAI_MAX_OUTPUT_TOKENS=120
# OPENAI_MAX_REQUESTS_PER_MINUTE=12
# OPENAI_MAX_CONCURRENCY=2
# OPENAI_BASE_URL=https://api.openai.com/v1
```

- `DOMAIN` is required (Caddy uses it), and `bootstrap.sh` refuses to continue without a real `SESSION_SECRET` set.
- `PAYMENT_MODE=sandbox` with sandbox keys. Live PayPal URLs are refused at startup, so a wrong base URL fails loudly instead of charging real money.
- The template leaves `LLM_MODE=mock` (the rule-based engine). To enable the model, as the live demo does, set `LLM_MODE=openai` and `OPENAI_API_KEY` in the server `.env`.
- Never commit the filled-in `.env`. The template is the only copy in the repo.

##### **2. Bring the stack up**

```bash
bash deploy/lightsail/bootstrap.sh
```

That runs, from the repo root on the server:

```bash
docker compose -f docker-compose.yml -f deploy/lightsail/docker-compose.lightsail.yml up -d --build
docker compose ps
```

Or run the compose command directly. `bootstrap.sh` first checks that `.env` exists and that `SESSION_SECRET` has been changed from the placeholder.

##### **3. Check it**

```bash
docker compose -f docker-compose.yml -f deploy/lightsail/docker-compose.lightsail.yml ps
curl -s https://$DOMAIN/health
```

`/health` returns `ok`, the payment mode, the LLM mode, whether a FHIR base URL is set, and `openai_configured`. When the mode is `openai` it also returns `openai_model`. It does not return a key. Then open `https://$DOMAIN` in a browser; the home page should load, with the one-click Patient / Staff / Billing lead tabs in the role bar.

#### **Updating**

Use `git pull` only when the server directory is a git checkout:

```bash
git pull
docker compose -f docker-compose.yml -f deploy/lightsail/docker-compose.lightsail.yml up -d --build
```

If the files were copied onto the server without git, copy the new tree in place and rebuild. Do not treat `git pull` as the update step for that directory.

The Dockerfile runs `alembic upgrade head` before starting Uvicorn, so schema migrations apply on startup.

#### **Stopping / wiping**

```bash
docker compose -f docker-compose.yml -f deploy/lightsail/docker-compose.lightsail.yml down
```

`down -v` deletes the database volume. Do not run it on the live demo.

```bash
# Only on a disposable database:
docker compose -f docker-compose.yml -f deploy/lightsail/docker-compose.lightsail.yml down -v
```

#### **Notes and limits**

- This is a single-instance deployment for the hackathon demo, not a high-availability setup.
- The demo logins (`/demo/login/{patient,staff,billing_lead}`) are one-click entries for the judging demo. Real deployments would replace them with proper sign-in.
- Secrets live only in the server `.env`, never in files in the repo. See `app/config.py`: settings come from environment variables.
- The app refuses to start against live PayPal (`PAYPAL_BASE_URL` must contain `sandbox`).
