# AC Dash

A **read-only** web dashboard for **AC Infinity** UIS controllers. It pulls the same live picture you get in the official app—**temperature**, **humidity**, **VPD**, and what’s happening on each **port**—and puts it in a browser tab on your computer or any device on your LAN.

The goal isn’t to replace the AC Infinity app for programming; it’s to **monitor** the environment you’re already controlling with their hardware. Think of the kind of at-a-glance grow-room view products like **Pulse Grow** offer, except here you’re not adding another sensor hub—you’re **reusing the controller and cloud data you already have**.

I **vibe-coded** this for my own use: I kept opening the phone app to sanity-check heat and VPD. I wanted that same snapshot on a big screen, running in **Docker**, easy to manage in **Portainer**, and reachable from other machines on the network without another login flow every time. The UI uses **Tailwind** with a **dark theme** and is deliberately **in the spirit of AC Infinity’s app** (cards, hierarchy, calm greens)—not a pixel-perfect copy, but familiar enough that it feels like the same ecosystem.

---

## Credits

The cloud API shape and a lot of the mental model (especially around VPD and sensor scaling) lean on community work. **Huge thanks to [LukeEvansTech/acinfinity-exporter](https://github.com/LukeEvansTech/acinfinity-exporter)**—that Prometheus exporter was the reference that made talking to the same HTTP API approachable. This project is a **dashboard**, not an exporter; same rough API family, different goal.

---

## How your details are stored

AC Dash needs your **AC Infinity cloud email and password** (the same ones as the mobile app) so it can call AC Infinity’s servers on your behalf.

**After setup (default Docker layout):**

- Credentials are written to a **single file** on disk: by default **`/app/data/.env`** inside the container (`ENV_FILE_PATH` overrides this).
- That file holds `ACINFINITY_EMAIL` and `ACINFINITY_PASSWORD` in standard dotenv form.
- **Nothing is sent to the author of this project** or to any third party except **AC Infinity’s own API** (`acinfinityserver.com`), same as the app.
- **Mount a volume** on `/app/data` so a container recreate doesn’t wipe the file; otherwise you’ll see the setup wizard again.

**Optional “headless” mode (Portainer / compose):**

- Set **`ACDASH_USE_ENV_CREDENTIALS=1`** and provide **`ACINFINITY_EMAIL`** and **`ACINFINITY_PASSWORD`** in the container environment.
- The wizard is skipped when those are present (and you’re not relying on a conflicting saved file—see `app/main.py` for the exact precedence).

**Login transport:** acdash tries **form-body** login first (same as early releases and many community clients), then **query-string** login with **`fcmToken=Android_…`** like the Android Retrofit client—so either server behavior still works. To try query first instead: **`ACINFINITY_LOGIN_TRANSPORT=query`**. Optional: **`ACINFINITY_FCM_TOKEN`** (suffix after `Android_` or full `Android_…` string) for the query-style call.

**Security hygiene:** don’t commit `.env`, don’t paste **debug JSON dumps** into public issues (they can include account fields, device IDs, Wi‑Fi names, etc.). This repo’s `.gitignore` is set up to steer clear of those.

---

## Reverse-engineered API notes (important)

AC Infinity does **not** publish a public HTTP spec for their cloud API. Everything below comes from **watching real responses**, comparing them to **equipment we could identify port-by-port in the app**, and cross-checking with the optional **full debug bundle** this dashboard can export.

**What we figured out**

- The live list endpoint (`devInfoListAll`) is a **snapshot**: great for “right now,” not historical graphs.
- **On/off** for a load is **`loadState`**, not always the older **`state`** field—reading the wrong one made ports look blank.
- **`loadType`** is meant to describe the **class** of device (fan, humidifier, light), but the list often shows **`0`** even when something is plugged in. The **`getDevSetting`** call for the same controller and port usually still returns the **real** non-zero `loadType`.
- **`portResistance`** behaves like a **UIS electrical / detection fingerprint**: in testing we saw **stable** values for e.g. EC inline fans vs humidifier-class loads vs LED-class loads—with the same readings whether the load was idle or running (identity, not “motor on”). Exact numbers vary by hardware; see **`AC_INFINITY_FIELDS.md`** for the values we sampled.
- **`deviceType`** was **`null`** everywhere in our samples, so it wasn’t useful for labeling gear.

**How we matched names to numbers**

We labeled ports in the app, exported JSON, and compared **known device types** (fan, humidifier, light, etc.) to **`portResistance`** and **`getDevSetting.loadType`**. The same patterns showed up when comparing **one labeled setup to another** (e.g. **controller / tent A vs. controller / tent B**), which suggested the mapping wasn’t a single-location quirk. Fan **size** (e.g. smaller vs larger inline) did **not** show up as different `loadType` values—only **fan class**—so friendly names still come from **`portName`** in the app.

**Honest limits**

We don’t have every UIS SKU on the bench. **Other devices might share those numbers or use new ones.** The full write-up with tables lives in [**`AC_INFINITY_FIELDS.md`**](AC_INFINITY_FIELDS.md); treat it as **observed behavior**, not a guarantee from AC Infinity. If you discover new combinations, extending that doc helps everyone.

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

## Pre-built images (GitHub → GHCR)

The repo includes **GitHub Actions** (`.github/workflows/release-docker.yml`) that push to **GitHub Container Registry**:

| Event | Image tags (examples) |
|--------|------------------------|
| Push to **`main`** | `ghcr.io/<owner>/<repo>:latest` and a short **SHA** tag |
| Push git tag **`v1.2.3`** | `ghcr.io/<owner>/<repo>:1.2.3` (semver) + a **GitHub Release** with notes |

**One-time GitHub setup:** **Settings → Actions → General → Workflow permissions** → **Read and write** (needed to push packages and create releases).

**Pulling the image:** GHCR is **not** the same URL as `github.com/releases` assets—the container lives at **`ghcr.io`**. If the package is **private**, add **Portainer → Registries → ghcr.io** with a GitHub PAT (`read:packages`), or set the package to **Public** under the repo’s **Packages** settings.

**Portainer:** use **`docker-compose.yml`** in this repo: replace `YOUR_GITHUB_USER` / `YOUR_REPO_NAME` in the `image:` line with your real GitHub path (same as in the browser URL, usually lowercase). After a green workflow run, **Stacks → Pull and redeploy** to update when you push new **`main`** or change the tag.

---

## Deploy with Portainer (or similar)

1. **Image:** use **`ghcr.io/...`** from the workflow above, build from this repo’s `Dockerfile`, or use another registry.
2. **Port mapping:** container **`8080`** → host port of your choice (e.g. `8080`).
3. **Volume:** bind mount or named volume **`/app/data`** so credentials survive container recreate.
4. **Environment (optional):**
   - `PORT` — listen port inside the container (default `8080`).
   - `CACHE_SECONDS` — how long to cache the cloud snapshot (default `45`).
   - `LOG_LEVEL` — e.g. `INFO`, `DEBUG`.
   - **Headless / no wizard:** set `ACDASH_USE_ENV_CREDENTIALS=1` and provide `ACINFINITY_EMAIL` + `ACINFINITY_PASSWORD` in the stack env; leave `/app/data` empty or omit the wizard file if you want env to win.

That’s enough to run it alongside the rest of your homelab and hit it from any browser on the network.

---

## What it is / isn’t

- **Is:** a **read-only** monitoring dashboard—it **displays** what the API returns; it doesn’t push setpoints or replace the official app for automation.
- **Isn’t:** official AC Infinity software, a Pulse Grow competitor hardware-wise, a Prometheus exporter, or a complete map of every device type in the wild.

Not affiliated with AC Infinity—just a project for a calm, big-screen environmental view without extra hardware in the room.

---

## License

**MIT** — see [`LICENSE`](LICENSE).

If you fork or ship this, keep the credit to **acinfinity-exporter** and respect AC Infinity’s terms for API use.
