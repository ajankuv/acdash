# AC Infinity cloud API — field & protocol reference (unofficial)

This document is the **published source of truth** for this repository (e.g. GitHub): everything you need to interpret AC Infinity **cloud JSON**, **authentication**, and **history/control** fields should live here or in **`app/`** (dashboard code). It is **not** official AC Infinity documentation; they can change behavior without notice.

**What is *not* on GitHub:** we maintain an internal **`RND/`** directory locally (APK decompile under `work/jadx`, probe scripts, env files, generated reports). The parent **`.gitignore` excludes `RND/*` except `RND/.gitignore`**, so clones do not get that tree. References below to `RND/...` paths describe **our** layout when reproducing the research—copy the ignore rules from **`RND/.gitignore`** if you create your own sandbox.

**Also in this repo (application code):**

| Path | Focus |
|------|--------|
| `app/normalize.py` | Dashboard scaling + sensor type mapping |
| `app/client.py` | Minimal authenticated client (login, device list, settings) |

**Typical filenames in a private `RND/` tree (not shipped):**

| Path | Focus |
|------|--------|
| `RND/USABLE_API.md` | Endpoint tables, header notes, CLI entry points |
| `RND/PRIORITIES.md` | Operator workflows (history presets, refresh, guarded writes) |
| `RND/MASTER_INVENTORY.md` | Hosts, IPC/chat URLs, embedded client constants |
| `RND/API_PROBE_FINDINGS.md` | Live probe outcomes (when regenerated) |
| `RND/tools/acinfinity_client.py` | Full RE CLI (history paging, refresh, writes) |

---

## 1. How we learned this (methodology)

Findings come from **layered evidence**. Stronger claims cite **multiple** of the following.

### 1.1 Live HTTP traffic and JSON

- We call the same **production-shaped** endpoints the Android app uses (default base `http://www.acinfinityserver.com/api/`).
- Responses use a repeated envelope (see §3). We record **field names**, **numeric scales**, and **error codes** from real bodies.
- **Caveat:** Your account, firmware, and SKU may return fields we have not seen; treat absent keys as normal.

### 1.2 Decompiled Android app (primary structural ground truth)

We reverse-engineered **AC Infinity Android v2.0.0 (build 108)** with **jadx** (in our environment, output lives under **`RND/work/jadx/sources/`** — that folder is gitignored). The app uses **Retrofit** interfaces; Gson maps JSON to Java/Kotlin types.

**High-value sources (jadx paths are the same relative layout wherever you decompile):**

| Area | Typical path under `work/jadx/sources/` | What it proves |
|------|---------------------------------------------|----------------|
| Device HTTP API | `defpackage/bb1.java` (Retrofit `DeviceApi`) | Exact paths, HTTP verbs, query names for controller calls (`dev/outletSwitch`, `version=2.0/dev/updateGroupsIsOn`, `PUT dev/modeAndSetting`, etc.) |
| Account / auth HTTP | `defpackage/b3.java` (`AccountApi`) | `user/appUserLogin`, `auth/refresh`, `auth/newToken`, `app/deviceTypeList` |
| History row shape | `com/eternal/base/concat/NetHistory.java` | Canonical **field names** and how rows map into the local DB entity `History` (temps, humidity, VPD, fan, sensors) |
| History paging | `defpackage/vf2.java` (and callers) | Client uses **`log/dataPage`** and interprets **`NetHistoryData`** (`rows`, `validFrom`, etc.) |
| Headers / signing | `defpackage/ln.java` (`BaseApplicationLifecycle` OkHttp interceptor) | When **`REFRESH_TOKEN`** feature is on: **`token`**, **`sign`**, **`requestId`**, **`version`**, split **`minversion`** ↔ **`devType`**; **`auth/refresh`** and **`auth/newToken`** are exempt from pre-refresh hooks |
| Signing algorithm | `defpackage/m36.java` (`TokenManager`, `computeSign`) | How **`sign`** is derived from token + version key + `secretId` + `requestApp` + `requestId` |
| Firmware family rules | `com/eternal/base/protocol/a.java` | **`getMinVersionHeader(int devType)`** → string `"devType|suffix"` where suffix is often **`3.5`** for certain device families; **`isHDevice`**, **`isAC8K`**, **`isRoomToRoomFan`**, etc. |
| Public device taxonomy | `com/eternal/base/concat/DeviceSeriesGroup.java`, `DeviceSeriesData.java` | **Series group** ids vs per-product **`devType`** integers |
| IPC / camera HTTP | `com/eternal/common/data/api/IpcApiService.java` | Absolute `/api/ipc/...` and `/api/dev/ml/...` routes (separate from “short” `dev/...` paths on the same host) |
| API base URLs | `defpackage/td.java` (`AppUrlConfig`) | Production vs UAT vs internal test IPs |

**What this means for you:** When JSON keys match **`NetHistory`** or **`NetDevice`** fields, we treat the **app’s Gson names** as authoritative for spelling (`vpdNums`, `allSpead` typo in Java, etc.). When only the server sends a key (e.g. some `deviceInfo` fields), we document it from samples and cross-check the app’s consumers where possible.

### 1.3 Hardware ground truth (UIS rigs)

For **load classification** (`loadType`, `portResistance`), we **did not** rely on guesswork alone. On **multiple physical controllers** with **ports labeled in the official app**, we correlated:

- `user/devInfoListAll` live port objects  
- `dev/getDevSetting` for the same `devId` + `port`  
- Exported **debug bundles** (`app/debug_bundle.py`) with loads **on** and **idle**

That produced the **observed** `loadType` / `portResistance` table in §8. Those integers are **inferred labels**, not a published AC Infinity enum.

### 1.4 Community integrations (secondary)

- **acinfinity-exporter** / **homeassistant-acinfinity**: sensor type numbering (0-based vs 1-based) and VPD scaling hints — useful for **USB sensor blobs**, not for trusting every cloud key.
- We treat community repos as **hints** until they match our APK + live JSON.

### 1.5 Project automation (private `RND/tools/` tree)

| Artifact | Role |
|----------|------|
| `RND/tools/acinfinity_client.py` | Login, refresh, `devInfoListAll`, **`log/dataPage`** paging, CSV presets, guarded writes |
| `RND/tools/ipc_probe.py` | Read-only **`/api/ipc/*`** + **`GET /api/dev/ml/byDevId/{devId}`** report |
| `RND/tools/auth_probe.py` / `auth-debug` | Login variants, base URL discovery |
| `RND/generated/api_catalog.json` | Scraped Retrofit route catalog (when regenerated; gitignored) |

---

## 2. Base URL and environments

- **Default production API root:** `http://www.acinfinityserver.com/api/` (HTTP cleartext is intentional in shipping `network_security_config`; see `td.java`).
- **UAT** (accounts **not** shared with prod): `https://uat-www.acinfinityserver.com/api/` — if login works in-app but not in scripts, **base URL mismatch** is a common cause (see internal `RND/USABLE_API.md` if you maintain it).
- All **relative** paths below are under that **`/api/`** root unless noted as **absolute** from the host (e.g. `/api/ipc/...`).

---

## 3. Response envelope (how to read every call)

Matching `com.eternal.framework.http.bean.BaseData` / Rx handling (`defpackage.dw0`):

| JSON field | Meaning |
|------------|--------|
| `code` | **200** = business success for most cloud calls; non-200 = failure |
| `msg` | Human-readable error or `success.` |
| `data` | Payload type varies by endpoint |

**Critical:** HTTP status is often **200** even when `code` is **500** or auth failed. Always inspect **`code`** in the JSON body.

---

## 4. Authentication (detailed)

### 4.1 Login

| Item | Detail |
|------|--------|
| **Method / path** | `POST user/appUserLogin` |
| **Body (form)** | `appEmail`, `appPasswordl` (**lowercase L** — typo preserved by API), optional `fcmToken` (app often sends `Android_` + FCM or `Android_`) |
| **Password encoding** | **Plain text** from the UI in the APK (`LoginModel`); no client-side MD5/SHA in the decompile we used |

**Success `data`:** `UserInfo` Gson object. The **session string** is JSON key **`appId`** (historical name; it is the bearer token).

### 4.2 Using the session on subsequent calls

Two patterns exist in the wild:

1. **Minimal “dashboard” style** (what `app/client.py` uses):  
   - Header **`Content-Type: application/x-www-form-urlencoded`**  
   - Header **`token: <appId>`** on POSTs  
   - Form body fields as documented per endpoint  

2. **Full Android OkHttp style** (optional in internal `acinfinity_client.py` via env):  
   - Additional headers: `appVersion`, `phoneType`, `languageType`, `languageVersion`, time-out report fields, and when signing is enabled: `token`, `requestApp`, `version`, `requestId`, `sign`, plus `devType` / `minversion` as computed per request.  
   - Signing matches **`m36.computeSign`**; requires **`secretId`** and **`requestApp`** from login when the refresh/signing feature path is active.

**Important:** For **`user/devInfoListAll`**, form field **`userId`** must be the **same string** as the session token (`appId`), not your email.

### 4.3 Refresh token (when present)

| Item | Detail |
|------|--------|
| **Path** | `POST auth/refresh` |
| **Query** | `refreshToken=<string>` |
| **Returns** | New `UserInfo` (new `appId`, possibly new `refreshToken`) when enabled for that account/build |

**Discovery:** `b3.java` defines `refresh(@Query("refreshToken") String)`. **`ln.java`** treats `/api/auth/refresh` as special: it skips the “fetch new token if needed” hook that runs on normal routes.

**Reality check:** Many logins return **no** `refreshToken` in JSON. Then the only recovery when the session dies is **login again** with email/password. The CLI prints a stderr hint when `refreshToken` is absent.

### 4.4 `auth/newToken` (diagnostics only)

`POST auth/newToken` with `appEmail`, `fcmToken` appears in **`b3.java`**. On default production it is often **404** or otherwise unused; keep it as a **diagnostic**, not a dependency.

### 4.5 Error semantics

- **`msg`** strings (e.g. incorrect password) are shown directly in the app for many codes; **`502`** / **`100001`** may be mapped to generic user strings (see §3 and internal notes if available).
- Use **`POST user/getByUserEmail`** with `appEmail` as a cheap “does this account exist **on this base URL**?” check.

---

## 5. Endpoints the project cares about (and how we found them)

**Discovery column:** where the contract was confirmed.

| Path | Method | Role | Discovery |
|------|--------|------|-----------|
| `user/appUserLogin` | POST | Obtain `appId` session | `b3.java`, live login |
| `auth/refresh` | POST | Rotate session with `refreshToken` | `b3.java`, `ln.java` |
| `auth/newToken` | POST | Alternate token bootstrap | `b3.java` (often dead on prod) |
| `user/devInfoListAll` | POST | All controllers + live `deviceInfo` | `bb1.java`, dashboard |
| `dev/getdevModeSettingList` | POST | Modes/automation settings per `devId`+`port` | `bb1.java`, `debug_bundle.py` |
| `dev/getDevSetting` | POST | Advanced per-port settings; reliable **`loadType`** | `bb1.java`, hardware cross-check |
| `log/dataPage` | POST | **Paged history** for charts | `bb1.java`, `vf2.java`, `NetHistory.java` |
| `log/logdataByAll` | POST | **`NetLog`** / event-style rows (Retrofit shares query keys with `dataPage` in `a63`, but **not** the same model as chart **`NetHistory`**) | **`RND/history_alternate_probe.py`** — often **`code` 999999** if you reuse env-chart windows blindly |
| `app/deviceTypeList` | GET | Public taxonomy (`DeviceSeriesGroup` list) | `b3.java` |
| `dev/outletSwitch` | POST | Outlet-style on/off (`devId`, `status`) | `bb1.java`, `ic3.java` |
| `version=2.0/dev/updateGroupsIsOn` | POST | Automation group on/off (`advId`, `isDel`, `isflag`) + headers | `bb1.java`, `advance/a.java` |
| `/api/dev/ml/updateLoadType` | POST | Set `loadType` class for `devId`+`port` | `bb1.java` (absolute path on host) |
| `PUT dev/modeAndSetting` | PUT | Schedules / setpoints (`QueryMap` + `modeAndSettingIdStr`) | `bb1.java`, `ControlHModel.java` |
| `/api/ipc/*`, `/api/dev/ml/*` | mixed | IPC camera / ML sidecar APIs | `IpcApiService.java` |

**Naming trap — history window:** On `log/dataPage`, query params **`time`** and **`endTime`** are **both unix seconds**, but **`time`** is the **newer** end of the window and **`endTime`** the **older** start. Implementations should match that ordering (our internal `acinfinity_client.py` does). The parameter names are easy to reverse.

---

## 6. Historical rows (`log/dataPage`) — field semantics

### 6.1 Container: `NetHistoryData`

Typical `data` object:

| Field | Meaning |
|-------|--------|
| `rows` | List of `NetHistory` |
| `total` | Server-reported row count for the query (if present). When this is only ~1–2k while you request a week, **you are usually seeing the full cloud dataset** for that device; span in hours stays short because the service does not keep dense multi-day series for all SKUs/accounts. |
| `validFrom` | Cutoff timestamp (seconds) — app may delete local data older than this when syncing |

**Retention:** Do not assume infinite cloud retention; long lookbacks may return partial data.

**Dash chart fetch:** AC Dash may run **two** `orderDirection` values (**`1`** and **`0`**) and merge deduped rows — the official app only sends **`1`**, but the cloud sometimes returns overlapping or sort-dependent pages; merging can widen the usable span. For ranges over **24 h**, the dash steps **`log/dataPage`** in **~168 h (7 d) calendar segments** (then paginates inside each segment). **Too-narrow segments (~16 h)** caused **`edge <= window_lo` after the first page**, so pagination never exhausted the window despite large `total` — see **`RND/HISTORY_DATA_PAGE.md`** (coarse scan, `history_cloud_scan.py`). The mobile app still benefits from **local DB** accumulation for instant multi-year scroll. Check JSON meta `order_directions`, `chunk_hours`, `fetch_chunks`, `api_total_max`, `span_hours_rounded`.

### 6.2 Row: `NetHistory` (Gson field names)

From `com/eternal/base/concat/NetHistory.java` (paraphrased). **Integers are often ×100** for physical units — apply the same scaling rules as live `deviceInfo` / `app/normalize.py` when presenting °C, °F, %RH, kPa VPD.

| Field | Meaning |
|-------|--------|
| `createTime` | Unix **seconds** |
| `devId` | Controller id |
| `temperature` | Primary temp channel (see `toHistory`: usage depends on `devType` / F vs C path) |
| `fTemperature` | Alternate temp (F-route in mapping) |
| `humidity` | Humidity integer (scaled) |
| `insideTemp` / `outsideTemp` | Used for IPC / room-to-room / AC8K style mappings in `toHistory` |
| `vpdNums` | VPD-related integer (maps to `History.vpd` / power on some models) |
| `dataStatus` | Encodes on/off or mode-related state per `toHistory` branches |
| `allSpead` | Fan-related level (**spelling matches APK**) |
| `portSpead` | Port fan / speed channel |
| `portSpeedMin` | Secondary storage (e.g. outside temp for AC8K, or min speed) |
| `portStatus` | Port state byte |
| `power` | Power channel on some branches |
| `sensors` | `List<NetSensor>` — USB / probe readings; may be filtered (`s55.filterInconsistentNetSensorPorts`) |
| `setId`, `number` | Opaque / sequencing |

**CLI preset “env”:** an internal tool (`acinfinity_client.py` in `RND/tools/`) exports these keys for CSV/JSON filtering if you use that script.

---

## 7. Live snapshot (`devInfoListAll`) — field semantics

### 7.1 Controller root (selected)

| Field | Meaning |
|-------|--------|
| `devId` | Cloud controller id (string in JSON) |
| `devName` | User label |
| `devType` | **Integer product family** for protocol branching (not the same as UI “series group”; see §9) |
| `devPortCount` | UIS port count |
| `online` / `isOnline` | Reachability (either may appear depending on payload shape) |
| `firmwareVersion`, `hardwareVersion` | Version strings |
| `wifiName` | SSID |
| `deviceInfo` | Nested live readings + `ports[]` |
| `zoneId` | Time zone |

**Privacy:** Payloads may include account identifiers. Treat dumps as sensitive.

### 7.2 `deviceInfo` (selected)

| Field | Meaning |
|-------|--------|
| `temperature`, `humidity` | Often **×100** integers |
| `temperatureF` | Fahrenheit raw companion |
| `vpdnums` / `vpdNums` | VPD-related (spelling varies by layer; normalize carefully) |
| `vpdstatus` | Sub-state |
| `curMode` | Controller mode |
| `ports` | Per-port array |
| `sensors` | USB sensors |

### 7.3 `deviceInfo.ports[]` (selected)

| Field | Meaning |
|-------|--------|
| `port` | Port index (typically **1-based** in samples) |
| `portName` | User label |
| `speak` | Level / speed index (upstream naming) |
| `loadState` | **Preferred** on/off style state for loads (**not** ambiguous `state`) |
| `loadType` | Equipment class int — **often `0` here even when connected** |
| `deviceType` | Often **`null`** in list payloads |
| `portResistance` | Detection fingerprint (see §8) |
| `online`, `curMode`, `remainTime` | Status / timers |
| `isOpenAutomation`, `advUpdateTime` | Automation metadata |

**acdash mapping:** `app/normalize.py` maps `loadState` → display `state`, and exposes `load_type`, `device_type` for templates.

---

## 8. `loadType` and `portResistance` (hardware cross-check)

This section restates the **evidence-based** classification table. Values are **observed correlations**, not official specs.

### 8.1 `loadType` (when non-zero, often from `getDevSetting`)

| `loadType` | Observed on our labeled gear |
|------------|-------------------------------|
| `6` | UIS **EC inline fans** (multiple sizes → **same** code) |
| `2` | **Humidifier-class** load |
| `1` | **LED / grow-light-class** load |
| `0` | Unknown or **not present** in this payload — **common** on `devInfoListAll` despite connected gear |

### 8.2 `portResistance` (fingerprint)

Stable across **on vs idle** in our samples:

| Approx. value | Observed class |
|---------------|----------------|
| `5100` | EC inline fans |
| `12000` | Humidifier-class |
| `3300` | LED-class |
| `65535` | Often “nothing detected” |

### 8.3 Why `getDevSetting` matters

`devInfoListAll` may lie with **`loadType: 0`**. **`dev/getDevSetting`** for the same `devId` and **`port`** frequently returns the **true** non-zero `loadType`. The dashboard’s **debug bundle** (`/api/debug/ac-infinity-dump`) calls this to enrich cards.

---

## 9. Device taxonomy: two different “type” systems

Do **not** conflate these:

| Concept | Source | Meaning |
|---------|--------|---------|
| **Series group** | `DeviceSeriesGroup.id` in `app/deviceTypeList` | UI grouping (e.g. `SMART_CONTROLLER = 1`, `AI_GROW_BOX = 41` in `DeviceSeriesGroup.java`) |
| **Product `devType`** | Integer on each controller in `devInfoListAll` | Drives protocol branches in `com.eternal.base.protocol.a` (H devices, AC8K, room-to-room fan, IPC, …) |
| **Per-port `loadType`** | Port / settings objects | **Equipment class** on that UIS port (fan vs humidifier vs light) |

**Exploration:** Diff `app/deviceTypeList` JSON against firmware capabilities you care about; new SKUs appear there before docs exist.

---

## 10. Control writes and `minversion` / `devType` headers

Many **`version=2.0/dev/*`** POSTs use Retrofit `@Header("minversion")` with the return value of **`a.getMinVersionHeader(devType)`**, which is a string **`"<devType>|<suffix>"`** (suffix often **`3.5`** for certain families). The OkHttp stack in **`ln.java`** **splits** that into outbound headers **`devType`** and **`minversion`** (suffix only).

**Implication:** Scripts that omit these headers may get **403**, silent failure, or wrong behavior. Our internal **`control_headers()`** helper mirrors the split using the same family rules as **`getMinVersionHeader`**.

**Examples (from `bb1.java`):**

- `dev/outletSwitch` — query `devId`, `status` (minimal headers in interface).  
- `version=2.0/dev/updateGroupsIsOn` — `advId`, `isDel`, `isflag` + minversion header chain.  
- `/api/dev/ml/updateLoadType` — `devId`, `loadType`, `port` (absolute path).

**Risk:** Any write can affect real hardware. The RND CLI requires **`--i-know`**.

---

## 11. IPC / ML APIs (separate surface)

Camera / SD card / alarm / ML routes live under **`/api/ipc/...`** and **`/api/dev/ml/...`** on the **same host** as `.../api/` (`IpcApiService.java`). They use the **session token** for many calls plus a **static `Authorization` header** for the vendor chat/SSE host (see `MASTER_INVENTORY.md`).

**Not required** to interpret grow-controller **temps/VPD/humidity** from `devInfoListAll` or `log/dataPage`, but relevant if you integrate IPC hardware.

---

## 12. Illustrative JSON (redacted)

```json
{
  "code": 200,
  "msg": "success.",
  "data": [
    {
      "devId": "<CONTROLLER_ID>",
      "devName": "Grow tent",
      "devType": 11,
      "deviceInfo": {
        "temperature": 2305,
        "humidity": 5207,
        "ports": [
          {
            "port": 1,
            "portName": "Exhaust",
            "speak": 5,
            "online": 1,
            "portResistance": 5100,
            "loadType": 6,
            "loadState": 1,
            "deviceType": null
          }
        ]
      }
    }
  ]
}
```

Apply ÷100 (or `normalize.py`) before showing units.

---

## 13. What is intentionally not fully mapped (more to explore)

| Area | Why it is open |
|------|------------------|
| **`PUT dev/modeAndSetting`** | Large `QueryMap`; every key is a maintenance burden — capture real traffic or trace `ControlHModel` for parity |
| **Full `NetSensor` binary/layout** | Probe blobs + HA/exporter disagree on 0- vs 1-based `sensorType` in some eras |
| **Every `loadType` int** | We only certified the set on **our** gear; new UIS SKUs may add values |
| **Outlet `status` semantics** | `ic3.outletSwitch` maps UI boolean to `0`/`1` — confirm per firmware before automation |
| **IPC + ML feature matrix** | Many routes return errors without IPC hardware; see `ipc_probe_report.json` |
| **Signed-only accounts** | If `secretId` / `requestApp` are required for your account, minimal-header clients need the full header stack |
| **Alternate bases** | `td.java` lists non-prod IPs — behavior may differ |

**Process for extending this doc:** (1) capture JSON, (2) find Gson type or Retrofit method in jadx, (3) confirm with a second account or firmware if possible, (4) update §5–§7 or §8 tables with provenance.

---

## 14. Related project files

**Published in this repository:**

| File | Role |
|------|------|
| `app/client.py` | Minimal-auth client: login, `devInfoListAll`, settings |
| `app/normalize.py` | Scaling + sensor interpretation for dashboard |
| `app/debug_bundle.py` | Enriched dump (sensitive) |

**Private `RND/` tree (not cloned from GitHub; typical layout):**

| File | Role |
|------|------|
| `RND/tools/acinfinity_client.py` | Full RE client (history paging, refresh, guarded writes) |
| `RND/USABLE_API.md` | Endpoint-first reference |
| `RND/PRIORITIES.md` | Operator-focused workflows |
| `RND/.gitignore` | **Tracked** — patterns for what to exclude inside `RND/` |

---

*This file is the public reference; internal RND artifacts stay local per root `.gitignore`.*
