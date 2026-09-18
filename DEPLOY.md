# Deploying Replyer with Docker + Nginx Proxy Manager

Target: `replyer.broteach.com` → server `217.216.54.187`, app on host port
`8087`, NPM admin UI on port `81`.

## How it fits together

```
Browser → https://replyer.broteach.com
            │  (NPM: TLS termination, port 443/80)
            ▼
  NPM Proxy Host → 217.216.54.187:8087
            │
            ▼
  [frontend container] nginx :80 → published as host :8087
            │  serves index.html
            │  proxies /auth, /users, /rules, /admin
            ▼
  [backend container] uvicorn/FastAPI :8000  (internal only, not published)
            │
      backend_data volume → app.db + secret.key (persist across rebuilds)
```

The frontend's `API_BASE` is now relative (`""`), so every request goes to
whatever origin loaded the page — no hardcoded `localhost:8000` to break in
production, and no CORS to worry about since it's same-origin.

## 1. Copy the project to the server

```bash
scp -r replyer/ user@217.216.54.187:/opt/
ssh user@217.216.54.187
cd /opt/replyer
```

## 2. Set your admin phone number

```bash
cp .env.example .env
nano .env        # set ADMIN_PHONE=+8551234567 (your own number, with country code)
```

## 3. Build and start

Requires Docker + the Docker Compose plugin on the server.

```bash
docker compose up -d --build
docker compose ps          # both containers should show "healthy"/"Up"
docker compose logs -f backend   # watch for startup errors, Ctrl+C to exit
```

This publishes the site on **host port 8087** only. The backend is not
exposed to the host or the internet directly — only the frontend/nginx
container can reach it, over the internal `replyer_net` docker network.

Quick sanity check before touching NPM:
```bash
curl -I http://localhost:8087/
```
Should return `200 OK` with the frontend's HTML.

Your existing `app.db` and `secret.key` (from the uploaded project) are
baked into the image and copied onto the `backend_data` volume the first
time the backend container starts, so nothing is lost. After that first
boot, the volume is authoritative — rebuilding the image never touches it.

## 4. Point DNS at the server

In your DNS provider for `broteach.com`, add:
```
A    replyer.broteach.com    →    217.216.54.187
```
Wait for it to propagate (`dig replyer.broteach.com` should return that IP).

## 5. Nginx Proxy Manager — add the proxy host

Open NPM's admin UI at `http://217.216.54.187:81` (or your existing
`https://` admin URL if you've already put NPM's own UI behind a domain).

**Proxy Hosts → Add Proxy Host**
- **Domain Names**: `replyer.broteach.com`
- **Scheme**: `http`
- **Forward Hostname / IP**:
  - `217.216.54.187` if NPM is running standalone (not sharing a docker
    network with this app) — this is the normal case.
  - `replyer-frontend` instead, *only if* you attach NPM's container to the
    same `replyer_net` docker network (then you could also skip publishing
    port 8087 to the host entirely). Not necessary unless you want it.
- **Forward Port**: `8087`
- **Cache Assets**: off (it's a dynamic app)
- **Block Common Exploits**: on
- **Websockets Support**: on — harmless to enable even though this app
  doesn't currently use websockets, and future-proofs it.

**SSL tab**
- **SSL Certificate**: Request a new SSL Certificate (Let's Encrypt)
- **Force SSL**: on
- **HTTP/2 Support**: on
- Agree to the Let's Encrypt TOS, Save.

NPM will issue the certificate (needs port 80 reachable from the internet
for the HTTP-01 challenge) and start proxying `https://replyer.broteach.com`
straight to your app.

## 6. Try it

Visit `https://replyer.broteach.com`, log in with a phone number, and walk
through the Telegram login flow from the README.

## Day-to-day operations

```bash
docker compose logs -f backend     # tail backend logs (Telethon events, errors)
docker compose restart backend     # restart just the backend
docker compose up -d --build       # rebuild after pulling code changes
docker compose down                # stop everything (backend_data volume persists)
```

**Back up `backend_data`** periodically — it holds `app.db` (all users,
rules, encrypted Telegram sessions) and `secret.key` (without it, every
saved session becomes unreadable):
```bash
docker run --rm -v replyer_backend_data:/data -v $(pwd):/backup alpine \
  tar czf /backup/replyer-backup-$(date +%F).tar.gz -C /data .
```

## Notes / things worth knowing

- **Firewall**: make sure ports `80`, `443`, and `81` are open on the
  server (81 ideally restricted to your own IP if possible, since it's the
  NPM admin panel). Port `8087` only needs to be reachable from NPM itself —
  if NPM runs on the same box, `localhost`/`127.0.0.1` access is enough and
  you can bind it there instead of `0.0.0.0` by changing the compose ports
  line to `"127.0.0.1:8087:80"` for extra safety.
- **ToS reminder from the README still applies**: this automates personal
  Telegram accounts via Telethon, which is outside Telegram's bot-API terms.
  Hosting it behind Docker/NPM doesn't change that risk — keep reply volume
  and delays sane.
