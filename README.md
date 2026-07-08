# Hoymiles DTU-Pro – Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Home Assistant sensor platform for the **Hoymiles DTU-Pro** gateway.  
Reads **inverter data** (all ports) and **DTSU666 smart meter data** directly via **Modbus TCP** over your local network — no cloud required.

---

## Features

| Data source | Sensors |
|---|---|
| **Inverter ports** (FC03) | PV voltage, current, power, today/total production, grid voltage, grid frequency, temperature, status, alarms |
| **DTSU666 meter** (FC04) | Grid import/export energy (total + per phase), grid power (net), grid voltage (L1/L2/L3), grid frequency |

### Key discovery

The DTSU666 meter data is **not** available in the standard Modbus FC03 (holding registers) address space.  
It was found via systematic scanning and is accessible via **FC04 (Input Registers)** in the address range `0x315C–0x318E`.  
This was reverse-engineered on firmware **V00.07.02** and confirmed with live meter readings.

---

## Supported hardware

| Device | Status |
|---|---|
| Hoymiles DTU-Pro (Ethernet) | ✅ Tested |
| Firmware V00.07.xx (stride=20) | ✅ Tested (`dtu_type: 1`) |
| Firmware < V00.07.xx (stride=40) | ✅ Supported (`dtu_type: 0`) |
| Hoymiles DTSU666 smart meter | ✅ Tested |
| HM-300 / HM-600 / HM-1200 / HM-1500 | ✅ Compatible |

---

## Installation

### HACS (recommended)

1. Open HACS → Integrations → ⋮ → Custom repositories
2. Add URL: `https://github.com/YOUR_USERNAME/ha-hoymiles-dtu-pro`  Category: `Integration`
3. Install **Hoymiles DTU-Pro**
4. Restart Home Assistant

### Manual

Copy `custom_components/hoymiles_dtu_pro/` into your HA `config/custom_components/` directory and restart.

---

## Configuration

Add to your `configuration.yaml`:

```yaml
sensor:
  - platform: hoymiles_dtu_pro
    host: 192.168.1.100       # IP of your DTU-Pro (Ethernet, not WiFi!)
    name: Hoymiles
    panels: 12                # Total number of DC inputs (e.g. 3× HM-1500 = 12)
    dtu_type: 1               # 1 = firmware ≥ V00.07 (stride 20)  |  0 = older (stride 40)
    meter: true               # Read DTSU666 meter via FC04 (default: true)
    scan_interval: "00:02:00"

    monitored_conditions:
      - pv_power
      - today_production
      - total_production
      - alarm_flag

    monitored_conditions_pv:
      - pv_power
      - pv_voltage
      - pv_current
      - grid_voltage
      - grid_frequency
      - today_production
      - total_production
      - temperature
      - operating_status
      - alarm_code
      - link_status

    monitored_conditions_meter:
      - grid_import_energy
      - grid_export_energy
      - grid_power
      - grid_frequency
      - grid_voltage_l1
      - grid_voltage_l2
      - grid_voltage_l3
      - grid_import_energy_l1
      - grid_import_energy_l2
      - grid_import_energy_l3
      - grid_export_energy_l1
      - grid_export_energy_l2
      - grid_export_energy_l3
```

> **Important:** Use the DTU's **Ethernet IP**, not the WiFi IP. The port is `502` (default).

---

## HA Energy Dashboard

For the Energy Dashboard, use these sensors:

| Dashboard field | Sensor |
|---|---|
| Solar production | `sensor.hoymiles_total_production` |
| Grid consumption (import) | `sensor.hoymiles_grid_import` |
| Return to grid (export) | `sensor.hoymiles_grid_export` |

---

## Register map reference

### Inverter ports (FC03 Holding Registers)

Base address: `0x1000 + (port - 1) × stride`  
Stride: **20** (0x14) for `dtu_type: 1` · **40** (0x28) for `dtu_type: 0`

| Offset | Content | Scale |
|---|---|---|
| +4 | PV voltage | ÷10 V |
| +5 | PV current | ÷100 A |
| +6 | Grid voltage | ÷10 V |
| +7 | Grid frequency | ÷100 Hz |
| +8 | PV power | ÷10 W |
| +9 | Today production | Wh |
| +10–11 | Total production (uint32) | Wh |
| +12 | Temperature (signed) | ÷10 °C |
| +13 | Operating status | — |
| +14 | Alarm code | — |
| +15 | Alarm count | — |
| +16 | Link status (high byte) | — |

### DTSU666 meter (FC04 Input Registers)

Base read: `address=0x315C, count=60`

| Address | Content | Type | Scale |
|---|---|---|---|
| 0x3161 | Grid frequency | uint16 | ÷100 Hz |
| 0x316D–0x316E | Import energy EP+ total | uint32 | ÷1000 kWh |
| 0x316F–0x3170 | Import energy Phase 1 | uint32 | ÷1000 kWh |
| 0x3171–0x3172 | Import energy Phase 2 | uint32 | ÷1000 kWh |
| 0x3173–0x3174 | Import energy Phase 3 | uint32 | ÷1000 kWh |
| 0x3175–0x3176 | Export energy EP- total | uint32 | ÷1000 kWh |
| 0x3177–0x3178 | Export energy Phase 1 | uint32 | ÷1000 kWh |
| 0x3179–0x317A | Export energy Phase 2 | uint32 | ÷1000 kWh |
| 0x317B–0x317C | Export energy Phase 3 | uint32 | ÷1000 kWh |
| 0x317D–0x317E | Voltage Phase 1 | uint32 | ÷1000 V |
| 0x317F–0x3180 | Voltage Phase 2 | uint32 | ÷1000 V |
| 0x3181–0x3182 | Voltage Phase 3 | uint32 | ÷1000 V |
| 0x318E | Active power total (net) | int16 | W (negative = export) |

> The meter registers are accessible on **all slave IDs 1–255** — the DTU ignores the unit identifier.

---

## Credits

- Inspired by [ArekKubacki/Hoymiles-Plant-DTU-Pro](https://github.com/ArekKubacki/Hoymiles-Plant-DTU-Pro) (MIT License)
- DTSU666 FC04 register layout discovered via systematic Modbus TCP scanning
