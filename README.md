# AC Dash

A small web dashboard for **AC Infinity** UIS controllers: tent temps, humidity, VPD, and ports on one screen you can open in a browser—handy on your laptop or any machine on your LAN.

I **vibe-coded** this for myself. I was checking my winter veggie tent on my phone all the time and wanted the same live picture on my computer, running in **Docker**, managed through **Portainer**, and reachable from other devices on the network without digging through the phone app every time. The UI is **Tailwind + dark theme** and I tried to keep the **feel close to AC Infinity’s app** (cards, typography-ish hierarchy)—not a pixel-perfect clone, but familiar enough that my brain doesn’t fight it.

---

## Credits

The cloud API shape and a lot of the mental model (especially around VPD and sensor scaling) lean on community work. **Huge thanks to [LukeEvansTech/acinfinity-exporter](https://github.com/LukeEvansTech/acinfinity-exporter)**—that Prometheus exporter was the base reference that made poking at the same HTTP API sane. This project is a **dashboard**, not an exporter; same rough API family, different goal.

---

## Reverse-engineered API notes (important)

AC Infinity doesn’t publish a public spec for these endpoints. I captured **full debug dumps** from the app and mapped fields like **`loadType`** (equipment class), **`portResistance`** (UIS “fingerprint” on the wire), **`loadState`** vs stale **`state`**, and when **`getDevSetting`** disagrees with the live list.

**Caveat:** I don’t own every UIS device. The mappings in [**`AC_INFINITY_FIELDS.md`**](AC_INFINITY_FIELDS.md) are grounded in **my** tents (fans, humidifier, Evo light). **Other SKUs might reuse the same numbers** or introduce new ones—treat the doc as **observed behavior**, not a guarantee. If you plug in something weird and want to extend the table, the dashboard includes an optional **API debug dump** you can use (don’t commit those JSON files; they can contain account and device identifiers).

---

## Requirements

- Docker (or any container runtime Portainer uses)
- AC Infinity **cloud** login (same email/password as the mobile app)

---

## Run with Docker (local)

Persist the setup wizard’s saved credentials with a volume on **`/app/data`** (the app writes `ACINFINITY_EMAIL` / `ACINFINITY_PASSWORD` there after first login).

```bash
docker build -t acdash:latest .

docker run -d \
  --name acdash \
  -p 8080:8080 \
  -v acdash_data:/app/data \
  --restart unless-stopped \
  acdash:latest
```

Open **http://localhost:8080** (or `http://<your-machine>:8080` from another device on the LAN). First visit runs a **setup** form; after that you get the dashboard.

**Health:** `GET /health` → `OK`

---

## Deploy with Portainer (or similar)

1. **Image:** build from this repo’s `Dockerfile`, or use your registry image if you push one.
2. **Port mapping:** container **`8080`** → host port of your choice (e.g. `8080`).
3. **Volume:** bind mount or named volume **`/app/data`** so credentials survive container recreate.
4. **Environment (optional):**
   - `PORT` — listen port inside the container (default `8080`).
   - `CACHE_SECONDS` — how long to cache the cloud snapshot (default `45`).
   - `LOG_LEVEL` — e.g. `INFO`, `DEBUG`.
   - **Headless / no wizard:** set `ACDASH_USE_ENV_CREDENTIALS=1` and provide `ACINFINITY_EMAIL` + `ACINFINITY_PASSWORD` in the stack env; leave `/app/data` empty or omit the wizard file so env wins.

That’s enough to run it alongside the rest of your homelab and hit it from any browser on the network.

---

## What it is / isn’t

- **Is:** read-only style dashboard (polls AC Infinity’s cloud API, shows normalized controllers/ports/sensors).
- **Isn’t:** official AC Infinity software, Prometheus metrics, or a complete enum of every device type in the ecosystem.

Not affiliated with AC Infinity—just a fan of their hardware who wanted a big-screen tent view.

---

## License

If you fork or ship this, keep the credit to **acinfinity-exporter** and respect AC Infinity’s terms for API use. Add a `LICENSE` file here if you want something explicit for your fork.
