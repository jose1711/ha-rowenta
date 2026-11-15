# Rowenta Robots for Home Assistant

Unofficial cloud integration for **Rowenta / Tefal X-plorer** robot vacuums that use the
Groupe SEB cloud platform (the robots controlled by the **Rowenta Robots** app).

> [!WARNING]
> **No warranty. Use entirely at your own risk.**
> This is an independent, unofficial project. It is not made, endorsed or supported by
> Groupe SEB, Rowenta or Tefal. It talks to an undocumented cloud API that can change or stop
> working at any time without notice. The software is provided "as is", without any warranty
> of any kind (see [LICENSE](LICENSE)). **By installing and using it you accept full
> responsibility** for any consequences, including malfunction of your robot, damage to
> property, loss of data, account suspension or any other issue. The authors are not liable for
> anything that results from its use.

## Supported robots

| Status | Models |
|---|---|
| ✅ Tested | **X-Plorer Serie 60** (`VAC001`) |
| ❔ May work, untested | Other X-plorer models on the same Groupe SEB platform (`IOT_SEB`), e.g. Serie 40 / 45 / 50 |
| ❌ Not supported | Models on the Tuya platform (Serie 65 / 75 / 85 / 95, 1xx, 2xx, …) and RobArt models (Serie 80 / 120 / 130) |

The integration was tested with **one model only**. Other robots that show up in the
Rowenta Robots app may well work too, because they share the same cloud protocol – but there is
no guarantee. If you try it with another model, please open an issue and tell whether it works.

## Features

- `vacuum`: start, pause/stop, return to dock, spot cleaning, locate (beep), suction power
  (`silent` / `normal` / `strong`), `send_command`
- `select`: cleaning mode used by `vacuum.start` (smart, random, edges, …) and the cleaning shown
  on the map (latest or one of the last 20)
- `image`: cleaning map with the robot position, refreshed live while cleaning
- sensors: battery, status, error, current mode, cleaned area and time, cleaning start, brush and
  filter wear, totals, last cleaning (history of the last 20 cleanings in the attributes),
  active schedules
- switches: enable/disable each schedule; schedules can be added, edited and deleted under
  *Configure* or with the `rowenta.get_schedules` / `set_schedule` / `delete_schedule` actions
- binary sensor: robot cloud connectivity
- the state is pushed over MQTT; no regular REST polling

Requirements: **Home Assistant 2025.10 or newer**, a Rowenta account with the robot already
paired in the official Rowenta Robots app, and an internet connection (there is no local API).

## Installation

### Option A: HACS (recommended)

The integration is not (yet) part of the default HACS store, so it is added as a
**custom repository**. You need [HACS](https://hacs.xyz/docs/use/) installed.

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=jose1711&repository=ha-rowenta&category=integration)

Click the button above, or do it by hand:

1. In Home Assistant open **HACS**.
2. Click the **⋮** menu (top right) → **Custom repositories**.
3. Enter `https://github.com/jose1711/ha-rowenta` as *Repository*, choose **Integration** as
   *Type* and click **Add**. Close the dialog.
4. Search HACS for **Rowenta Robots**, open it and click **Download** (confirm the latest version).
5. **Restart Home Assistant** (*Settings → System → ⋮ → Restart Home Assistant*).
6. Continue with [Configuration](#configuration).

Updates then appear in HACS like for any other integration.

### Option B: manual

1. Download the latest release (or the repository as a ZIP) from GitHub.
2. Copy the folder `custom_components/rowenta` into your Home Assistant configuration
   directory, so that you end up with `config/custom_components/rowenta/manifest.json`.
3. Restart Home Assistant and continue with [Configuration](#configuration).

## Configuration

[![Open your Home Assistant instance and start setting up the integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=rowenta)

1. Go to **Settings → Devices & services → Add integration** and search for
   **Rowenta Robots** (or click the button above).
2. Enter the **e-mail and password** of your Rowenta account – the same as in the Rowenta Robots app.
3. Choose the **country** the app is set to. Robots are stored per country in the account, so
   it must match the country selected in the app.
4. Your robot(s) appear as devices with their entities.

The password is used only to log in; only a refresh token is stored. If Rowenta revokes it
(e.g. after a password change), Home Assistant asks for the password again.

Schedules can be managed under **Settings → Devices & services → Rowenta Robots → Configure**.

## `send_command`

```yaml
# cleaning method (+ optional power)
action: vacuum.send_command
target: { entity_id: vacuum.x_plorer_serie_60 }
data: { command: wall_follow, params: { power: strong } }

# raw JSON to the VAC001/<id>/command topic
action: vacuum.send_command
target: { entity_id: vacuum.x_plorer_serie_60 }
data: { command: raw, params: { beep: "ON" } }
```

## Troubleshooting

Enable debug logging and check *Settings → System → Logs*:

```yaml
logger:
  logs:
    custom_components.rowenta: debug
```

When reporting an issue, attach the diagnostics (*device page → ⋮ → Download diagnostics*);
tokens, e-mail and identifiers are removed from it.

## How it works

Salesforce (RCU) login → `refresh_token` → short-lived `id_token` → AWS Cognito
(via `consumers.api.groupe-seb.com`) → temporary AWS credentials → MQTT over WebSocket with SigV4
at `prod-new.iot.mqtt.groupe-seb.com`. Topics `VAC001/<id>/{status,history,command,getStatus,…}`
and the AWS device shadow (schedules, firmware). The connection is renewed before the credentials
expire (~1 h). Maps come from `iot.api.groupe-seb.com/vacuum/v1/map/…`.

## Development

```sh
uv venv .venv && uv pip install --python .venv/bin/python pytest-homeassistant-custom-component
.venv/bin/python -m pytest
```

## Disclaimer

Rowenta, Tefal and X-plorer are trademarks of Groupe SEB, used here only to describe
compatibility. This project is provided **without any warranty**; you use it **at your own risk
and take full responsibility** for its use.
