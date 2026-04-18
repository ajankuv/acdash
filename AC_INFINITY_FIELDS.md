# AC Infinity cloud API — field notes (unofficial)

Reverse-engineered from HTTP responses and community integrations. **Not** official AC Infinity documentation.

## Classification — matched and usable for display

On **verified home rigs** (multiple controllers: flower 3×3, drying 3×3, 2×4, seedling/mom), we **matched physical gear to API fields** and **proved a consistent way to show what each port is**:

| Goal | How we derive it |
|------|------------------|
| **Fan vs humidifier vs grow light** | **`getDevSetting.data.loadType`**: `6` = UIS EC **fan**, `2` = **humidifier**, `1` = **grow light** (Evo-class in our samples). |
| **When the list lies** | `devInfoListAll` often has **`loadType: 0`** on a connected port; **`getDevSetting`** still returns the correct non-zero class for the same `devId` + `port`. |
| **Backup fingerprint** | **`portResistance`** on the live list repeatedly aligned: **`5100`** with fans, **`12000`** with humidifier, **`3300`** with Evo — same values with loads **off** or **on** (identity, not power state). |
| **Fine print** | Fan **size** (4″ vs 6″) is **not** a separate `loadType`; both mapped to **`6`**. Use **`portName`** for labels. **`deviceType`** stayed **`null`** in our dumps. |

This is **proven for the devices we actually tested**, not a guarantee for every future UIS SKU or firmware. For unknown products, collect **`loadType` + `portResistance` + product name** and extend the table.

Enum meanings for `loadType` remain **inferred** from that cross-check, not an AC Infinity published spec.

## Endpoints this project uses

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/user/appUserLogin` | POST | Auth; returns session `appId` used as `token` + `userId` |
| `/api/user/devInfoListAll` | POST | Live snapshot: all controllers, ports, sensors, env readings |
| `/api/dev/getdevModeSettingList` | POST | Per-port (and port `0` = controller) mode/automation settings |
| `/api/dev/getDevSetting` | POST | Per-port advanced settings; often has **`loadType`** when list shows `0` |

Form fields commonly include `userId` (login), `devId`, `port` (integer; `0` = controller-level for settings calls).

---

## `devInfoListAll` response shape

Top level (typical):

- `code` — `200` on success  
- `msg` — e.g. `success.`  
- `data` — array of controller objects  

### Controller object (root) — selected fields

| Field | Role |
|-------|------|
| `devId` | Controller id (string in JSON) |
| `devName` | User label |
| `devType` | Hardware family code (e.g. UIS controller variants) |
| `devPortCount` | Number of UIS ports |
| `online` | Cloud reachability |
| `firmwareVersion`, `hardwareVersion` | Versions |
| `wifiName` | Associated SSID name |
| `deviceInfo` | Nested live readings + `ports[]` |
| `zoneId` | IANA timezone id |

**Privacy:** root objects may also include account-related fields (e.g. email). Treat full dumps as sensitive.

### `deviceInfo` — selected fields

| Field | Role |
|-------|------|
| `temperature`, `humidity` | Often scaled ×100 in raw integer form |
| `temperatureF` | Companion F-scale raw |
| `vpdnums` | VPD-related scaled value (see app `normalize.py`) |
| `vpdstatus` | VPD sub-state |
| `curMode` | Controller-level mode |
| `ports` | Array of per-port live structs |
| `sensors` | Optional array of USB sensor readings |

### `deviceInfo.ports[]` — per-outlet / per-UIS-port

| Field | Role |
|-------|------|
| `port` | Port index (1-based in samples) |
| `portName` | User label in app |
| `speak` | Reported level / speed index (naming from upstream API) |
| `online` | Port/device online flag |
| `curMode` | Port mode |
| `remainTime` | Timer / schedule remainder where applicable |
| `portResistance` | Cabling / detection; `65535` often means “nothing detected” |
| **`loadState`** | **Load on/off style state** (e.g. `0` / `1`) — **not** `state` |
| **`loadType`** | Integer equipment class; **often `0` in list** even when a load is connected |
| **`deviceType`** | Often **`null`** in list payloads (not a friendly string like `"fan"`) |
| `loadId` | Opaque id / slot |
| `abnormalState`, `overcurrentStatus` | Fault indicators |
| `isOpenAutomation`, `advUpdateTime` | Automation metadata |

**acdash:** `_normalize_port` maps **`loadState`** (fallback `state`) → JSON field `state`, and passes **`loadType`** → `load_type`, **`deviceType`** → `device_type`.

---

## `loadType` — values with real hardware cross-check

Community / guesswork is not enough here: the same integers appeared when **known** gear was labeled in the app and compared across **`devInfoListAll`**, **`getDevSetting`**, and full debug bundles (including runs with loads **on** vs **idle**).

### Ground truth (3×3 flower tent — reference wiring)

User-reported wiring (port index → physical device):

| Port | Device |
|------|--------|
| 1 | 6″ inline fan (S6-class EC fan) |
| 2 | CLOUDFORGE T3 plant humidifier |
| 3 | 4″ inline fan (S4-class EC fan) |
| 4 | EVO-style LED grow light |

**`loadType` seen for those ports** (from API when non-zero — often `getDevSetting.data.loadType`; list sometimes still `0`):

| `loadType` | Matches these devices in this setup |
|------------|--------------------------------------|
| `6` | Both inline EC fans (6″ and 4″) — **size is not encoded** in `loadType`; only “fan class” |
| `2` | CLOUDFORGE / humidifier class |
| `1` | EVO / LED grow light class |
| `0` | “Unknown / not sent in this payload” — common on `devInfoListAll` even with gear connected |

So the cloud is not returning “S6 vs S4” as different `loadType` values; both fans were **`6`**. Differentiation would have to come from **`portName`** (user label), **`portResistance`** (see below), or something else we have not mapped.

**Cross-checks elsewhere:** Drying 3×3 (humidifier + exhaust), seedling/mom **4″ inline on port 4**, and 2×4 exhaust showed the same **`loadType` / `portResistance` pairings** where `getDevSetting` was available — so the mapping is **not** limited to a single tent.

### `portResistance` — UIS fingerprint (observed, not official)

When gear was connected, **`portResistance`** was **not** the same for every load, and values **did not change** simply because the load was toggled on or off:

| Approx. `portResistance` | Device class in our rigs |
|--------------------------|---------------------------|
| `5100` | UIS **EC inline fans** (4″ and 6″ samples) |
| `12000` | **Humidifier** / CloudForge-style |
| `3300` | **Evo / LED** grow light |

`65535` = nothing detected on that port in our samples. Other SKUs or firmware may add values; treat unknown codes as “unmapped” until sampled.

### `deviceType`

In that dump, **`deviceType` was `null`** on every port. No separate numeric “model id” showed up there; the useful discriminator was **`loadType`** (+ optional **`portResistance`**).

### Other values

More UIS products may use other `loadType` integers. If you capture **product + `loadType` + `portResistance`**, we can extend this table without guessing.

---

## `getDevSetting` / `getdevModeSettingList` — why they matter

- **`getDevSetting`** frequently returns a **non-zero `loadType`** when `devInfoListAll` still shows `loadType: 0` for the same port.  
- Both endpoints return large automation trees (`atType`, VPD/temp/humidity triggers, schedules, etc.). The Home Assistant `ac_infinity` integration’s `const.py` is a good key → name map for control fields.

---

## Redacted example (illustrative only)

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

Numeric scales (e.g. temperature ÷ 100) are handled in `app/normalize.py`.

---

## Related project files

- `app/normalize.py` — list → dashboard model  
- `app/client.py` — HTTP client  
- `app/debug_bundle.py` — optional full dump for support (contains **sensitive** data; do not commit real dumps)
