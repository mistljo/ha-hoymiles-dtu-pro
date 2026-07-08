"""
Hoymiles DTU-Pro sensor platform for Home Assistant.

Reads inverter data (Modbus FC03) and DTSU666 smart meter data (Modbus FC04)
directly from the Hoymiles DTU-Pro via Modbus TCP over LAN.

Inverter register layout (FC03):
  Base address: 0x1000 + (port - 1) * stride
  stride = 20 (0x14) for firmware >= V00.07.xx  → dtu_type: 1  (default)
  stride = 40 (0x28) for firmware <  V00.07.xx  → dtu_type: 0

DTSU666 meter register layout (FC04 Input Registers):
  Discovered via Modbus TCP scanning of a DTU-Pro running firmware V00.07.02.
  All slave-IDs (1-255) return identical data; the DTU ignores the unit identifier.
  Base block: 0x315C  (read count=60 in one request)

  Offset  Address  Content                     Type    Scale
  ------  -------  --------------------------  ------  -----
  5       0x3161   Grid frequency              uint16  /100 → Hz
  17-18   0x316D   Import energy total EP+     uint32  /1000 → kWh
  19-20   0x316F   Import energy Phase 1       uint32  /1000 → kWh
  21-22   0x3171   Import energy Phase 2       uint32  /1000 → kWh
  23-24   0x3173   Import energy Phase 3       uint32  /1000 → kWh
  25-26   0x3175   Export energy total EP-     uint32  /1000 → kWh
  27-28   0x3177   Export energy Phase 1       uint32  /1000 → kWh
  29-30   0x3179   Export energy Phase 2       uint32  /1000 → kWh
  31-32   0x317B   Export energy Phase 3       uint32  /1000 → kWh
  33-34   0x317D   Voltage Phase 1             uint32  /1000 → V
  35-36   0x317F   Voltage Phase 2             uint32  /1000 → V
  37-38   0x3181   Voltage Phase 3             uint32  /1000 → V
  50      0x318E   Active power total (signed) int16   1 → W  (neg = export)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import voluptuous as vol
from homeassistant.components.sensor import (
    PLATFORM_SCHEMA,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.util import Throttle

_LOGGER = logging.getLogger(__name__)

# ── Configuration keys ────────────────────────────────────────────────────────
CONF_PANELS = "panels"
CONF_DTU_TYPE = "dtu_type"
CONF_METER = "meter"
CONF_MONITORED_CONDITIONS = "monitored_conditions"
CONF_MONITORED_CONDITIONS_PV = "monitored_conditions_pv"
CONF_MONITORED_CONDITIONS_METER = "monitored_conditions_meter"

DEFAULT_NAME = "Hoymiles"
DEFAULT_PORT = 502
DEFAULT_SCAN_INTERVAL = timedelta(minutes=2)
DEFAULT_DTU_TYPE = 1   # stride=20, firmware ≥ V00.07
DEFAULT_PANELS = 0
DEFAULT_METER = True

# ── Plant-level (aggregated) sensor types ─────────────────────────────────────
# [friendly_name, unit, device_class, state_class]
PLANT_TYPES: dict[str, list] = {
    "pv_power": [
        "PV Power", UnitOfPower.WATT,
        SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT,
    ],
    "today_production": [
        "PV Today", UnitOfEnergy.WATT_HOUR,
        SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING,
    ],
    "total_production": [
        "PV Total", UnitOfEnergy.WATT_HOUR,
        SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING,
    ],
    "alarm_flag": ["Alarm Flag", None, None, None],
}

# ── Per-port inverter sensor types ────────────────────────────────────────────
# [register_offset, friendly_name, unit, device_class, state_class, scale]
PV_TYPES: dict[str, list] = {
    "pv_voltage":       [4,  "PV Voltage",     UnitOfElectricPotential.VOLT,  SensorDeviceClass.VOLTAGE,      SensorStateClass.MEASUREMENT,      10.0],
    "pv_current":       [5,  "PV Current",     UnitOfElectricCurrent.AMPERE,  SensorDeviceClass.CURRENT,      SensorStateClass.MEASUREMENT,     100.0],
    "grid_voltage":     [6,  "Grid Voltage",   UnitOfElectricPotential.VOLT,  SensorDeviceClass.VOLTAGE,      SensorStateClass.MEASUREMENT,      10.0],
    "grid_frequency":   [7,  "Grid Frequency", UnitOfFrequency.HERTZ,         None,                           SensorStateClass.MEASUREMENT,     100.0],
    "pv_power":         [8,  "PV Power",       UnitOfPower.WATT,              SensorDeviceClass.POWER,        SensorStateClass.MEASUREMENT,      10.0],
    "today_production": [9,  "Today",          UnitOfEnergy.WATT_HOUR,        SensorDeviceClass.ENERGY,       SensorStateClass.TOTAL_INCREASING,  1.0],
    "total_production": [10, "Total",          UnitOfEnergy.WATT_HOUR,        SensorDeviceClass.ENERGY,       SensorStateClass.TOTAL_INCREASING,  1.0],  # uint32
    "temperature":      [12, "Temperature",    UnitOfTemperature.CELSIUS,     SensorDeviceClass.TEMPERATURE,  SensorStateClass.MEASUREMENT,      10.0],  # signed
    "operating_status": [13, "Status",         None,                          None,                           None,                               1.0],
    "alarm_code":       [14, "Alarm Code",     None,                          None,                           None,                               1.0],
    "alarm_count":      [15, "Alarm Count",    None,                          None,                           None,                               1.0],
    "link_status":      [16, "Link Status",    None,                          None,                           None,                               1.0],
}

# ── DTSU666 smart meter sensor types ─────────────────────────────────────────
# [friendly_name, unit, device_class, state_class]
METER_TYPES: dict[str, list] = {
    "grid_import_energy": [
        "Grid Import",   UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING,
    ],
    "grid_export_energy": [
        "Grid Export",   UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING,
    ],
    "grid_power": [
        "Grid Power",    UnitOfPower.WATT,
        SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT,
    ],
    "grid_frequency": [
        "Grid Frequency", UnitOfFrequency.HERTZ,
        None, SensorStateClass.MEASUREMENT,
    ],
    "grid_voltage_l1": [
        "Grid Voltage L1", UnitOfElectricPotential.VOLT,
        SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT,
    ],
    "grid_voltage_l2": [
        "Grid Voltage L2", UnitOfElectricPotential.VOLT,
        SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT,
    ],
    "grid_voltage_l3": [
        "Grid Voltage L3", UnitOfElectricPotential.VOLT,
        SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT,
    ],
    "grid_import_energy_l1": [
        "Grid Import L1", UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING,
    ],
    "grid_import_energy_l2": [
        "Grid Import L2", UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING,
    ],
    "grid_import_energy_l3": [
        "Grid Import L3", UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING,
    ],
    "grid_export_energy_l1": [
        "Grid Export L1", UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING,
    ],
    "grid_export_energy_l2": [
        "Grid Export L2", UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING,
    ],
    "grid_export_energy_l3": [
        "Grid Export L3", UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING,
    ],
}

# ── Platform schema ───────────────────────────────────────────────────────────
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_HOST): cv.string,
    vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_PANELS, default=DEFAULT_PANELS): cv.positive_int,
    vol.Optional(CONF_DTU_TYPE, default=DEFAULT_DTU_TYPE): vol.In([0, 1]),
    vol.Optional(CONF_METER, default=DEFAULT_METER): cv.boolean,
    vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): cv.time_period,
    vol.Optional(CONF_MONITORED_CONDITIONS, default=list(PLANT_TYPES)):
        vol.All(cv.ensure_list, [vol.In(PLANT_TYPES)]),
    vol.Optional(CONF_MONITORED_CONDITIONS_PV, default=list(PV_TYPES)):
        vol.All(cv.ensure_list, [vol.In(PV_TYPES)]),
    vol.Optional(CONF_MONITORED_CONDITIONS_METER, default=list(METER_TYPES)):
        vol.All(cv.ensure_list, [vol.In(METER_TYPES)]),
})


# ── Setup ─────────────────────────────────────────────────────────────────────
def setup_platform(hass, config, add_entities, discovery_info=None):
    host = config[CONF_HOST]
    port = config[CONF_PORT]
    name = config[CONF_NAME]
    panels = config[CONF_PANELS]
    dtu_type = config[CONF_DTU_TYPE]
    meter_enabled = config[CONF_METER]
    scan_interval = config[CONF_SCAN_INTERVAL]

    updater = HoymilesDTUUpdater(host, port, scan_interval, dtu_type, meter_enabled)
    updater.update()

    if updater.data is None:
        raise RuntimeError(
            f"Cannot connect to Hoymiles DTU-Pro at {host}:{port}. "
            "Check host/port and ensure the DTU is reachable via Ethernet."
        )

    entities: list[SensorEntity] = []

    # Plant-level sensors
    for condition in config[CONF_MONITORED_CONDITIONS]:
        entities.append(HoymilesPlantSensor(name, condition, panels, updater))

    # Per-port sensors
    for port_num in range(1, panels + 1):
        for condition in config[CONF_MONITORED_CONDITIONS_PV]:
            entities.append(HoymilesPortSensor(name, port_num, condition, updater))

    # DTSU666 meter sensors
    if meter_enabled:
        for condition in config[CONF_MONITORED_CONDITIONS_METER]:
            entities.append(HoymilesMeterSensor(name, condition, updater))

    add_entities(entities, True)


# ── Data updater ──────────────────────────────────────────────────────────────
class HoymilesDTUUpdater:
    """Fetches inverter and meter data from the DTU-Pro via Modbus TCP."""

    METER_BASE = 0x315C
    METER_COUNT = 60

    def __init__(self, host: str, port: int, scan_interval, dtu_type: int, meter: bool):
        self.host = host
        self.port = port
        self.dtu_type = dtu_type
        self.meter_enabled = meter
        self.data: dict | None = None
        self.update = Throttle(scan_interval)(self._update)

    def _connect(self):
        try:
            from pymodbus.client import ModbusTcpClient
            client = ModbusTcpClient(self.host, port=self.port, timeout=5)
            if not client.connect():
                return None
            return client
        except Exception as exc:
            _LOGGER.error("Modbus connect failed: %s", exc)
            return None

    def _read_hr(self, client, address: int, count: int):
        """FC03 Read Holding Registers."""
        return client.read_holding_registers(address, count=count, device_id=1)

    def _read_ir(self, client, address: int, count: int):
        """FC04 Read Input Registers."""
        return client.read_input_registers(address, count=count, device_id=1)

    @staticmethod
    def _u16(regs, offset: int) -> int:
        return regs[offset]

    @staticmethod
    def _s16(regs, offset: int) -> int:
        v = regs[offset]
        return v if v < 0x8000 else v - 0x10000

    @staticmethod
    def _u32(regs, offset: int) -> int:
        return (regs[offset] << 16) | regs[offset + 1]

    def _read_ports(self, client, num_ports: int) -> list[dict | None]:
        stride = 20 if self.dtu_type == 1 else 40
        ports = []
        for port_num in range(1, num_ports + 1):
            addr = 0x1000 + (port_num - 1) * stride
            r = self._read_hr(client, addr, 17)
            if r.isError():
                _LOGGER.debug("Port %d: Modbus error %s", port_num, r)
                ports.append(None)
                continue
            regs = r.registers
            data_type = regs[0] >> 8
            if data_type == 0:
                ports.append(None)
                continue
            ports.append({
                "data_type":       data_type,
                "pv_voltage":      self._u16(regs, 4)  / 10.0,
                "pv_current":      self._u16(regs, 5)  / 100.0,
                "grid_voltage":    self._u16(regs, 6)  / 10.0,
                "grid_frequency":  self._u16(regs, 7)  / 100.0,
                "pv_power":        self._u16(regs, 8)  / 10.0,
                "today_production": self._u16(regs, 9),
                "total_production": self._u32(regs, 10),
                "temperature":     self._s16(regs, 12) / 10.0,
                "operating_status": self._u16(regs, 13),
                "alarm_code":       self._u16(regs, 14),
                "alarm_count":      self._u16(regs, 15),
                "link_status":      regs[16] >> 8,
            })
        return ports

    def _read_meter(self, client) -> dict | None:
        r = self._read_ir(client, self.METER_BASE, self.METER_COUNT)
        if r.isError():
            _LOGGER.warning("Meter read failed: %s", r)
            return None
        rv = r.registers

        def off(addr): return addr - self.METER_BASE

        return {
            "grid_frequency":         self._u16(rv, off(0x3161)) / 100.0,
            "grid_import_energy":     self._u32(rv, off(0x316D)) / 1000.0,
            "grid_import_energy_l1":  self._u32(rv, off(0x316F)) / 1000.0,
            "grid_import_energy_l2":  self._u32(rv, off(0x3171)) / 1000.0,
            "grid_import_energy_l3":  self._u32(rv, off(0x3173)) / 1000.0,
            "grid_export_energy":     self._u32(rv, off(0x3175)) / 1000.0,
            "grid_export_energy_l1":  self._u32(rv, off(0x3177)) / 1000.0,
            "grid_export_energy_l2":  self._u32(rv, off(0x3179)) / 1000.0,
            "grid_export_energy_l3":  self._u32(rv, off(0x317B)) / 1000.0,
            "grid_voltage_l1":        self._u32(rv, off(0x317D)) / 1000.0,
            "grid_voltage_l2":        self._u32(rv, off(0x317F)) / 1000.0,
            "grid_voltage_l3":        self._u32(rv, off(0x3181)) / 1000.0,
            "grid_power":             self._s16(rv, off(0x318E)),
        }

    def _update(self):
        client = self._connect()
        if client is None:
            self.data = None
            return
        try:
            # Always read at least one port to check connectivity
            r = self._read_hr(client, 0x1000, 2)
            if r.isError():
                self.data = None
                return

            # Determine how many ports to scan (up to 16 max)
            max_ports = 16
            ports = self._read_ports(client, max_ports)
            active = [p for p in ports if p is not None]

            meter = None
            if self.meter_enabled:
                meter = self._read_meter(client)

            # Aggregate plant-level data
            total_power = sum(p["pv_power"] for p in active)
            total_today = sum(p["today_production"] for p in active)
            total_total = sum(p["total_production"] for p in active)
            alarm_flag = any(p["alarm_code"] != 0 for p in active)

            self.data = {
                "ports": ports,
                "pv_power": round(total_power, 1),
                "today_production": total_today,
                "total_production": total_total,
                "alarm_flag": 1 if alarm_flag else 0,
                "meter": meter,
            }
        except Exception as exc:
            _LOGGER.error("DTU update error: %s", exc)
            self.data = None
        finally:
            try:
                client.close()
            except Exception:
                pass


# ── Sensor entities ───────────────────────────────────────────────────────────
class HoymilesPlantSensor(SensorEntity):
    """Aggregated plant-level sensor (sum of all active ports)."""

    def __init__(self, name: str, sensor_type: str, panels: int, updater: HoymilesDTUUpdater):
        self._name = name
        self._type = sensor_type
        self._panels = panels
        self._updater = updater
        self._state = None
        info = PLANT_TYPES[sensor_type]
        self._attr_name = f"{name} {info[0]}"
        self._attr_unique_id = f"hoymiles_{name.lower()}_{sensor_type}"
        self._attr_native_unit_of_measurement = info[1]
        self._attr_device_class = info[2]
        self._attr_state_class = info[3]

    @property
    def native_value(self):
        if self.data is None:
            return None
        return self.data.get(self._type)

    @property
    def data(self):
        return self._updater.data

    def update(self):
        self._updater.update()


class HoymilesPortSensor(SensorEntity):
    """Per-port inverter sensor."""

    def __init__(self, name: str, port: int, sensor_type: str, updater: HoymilesDTUUpdater):
        self._name = name
        self._port = port
        self._type = sensor_type
        self._updater = updater
        info = PV_TYPES[sensor_type]
        self._reg_offset = info[0]
        self._scale = info[5]
        self._attr_name = f"{name} Port{port} {info[1]}"
        self._attr_unique_id = f"hoymiles_{name.lower()}_port{port}_{sensor_type}"
        self._attr_native_unit_of_measurement = info[2]
        self._attr_device_class = info[3]
        self._attr_state_class = info[4]

    @property
    def native_value(self):
        if self._updater.data is None:
            return None
        ports = self._updater.data.get("ports", [])
        if self._port > len(ports) or ports[self._port - 1] is None:
            return None
        port_data = ports[self._port - 1]
        return port_data.get(self._type)

    def update(self):
        self._updater.update()


class HoymilesMeterSensor(SensorEntity):
    """DTSU666 smart meter sensor (read via FC04 Input Registers)."""

    def __init__(self, name: str, sensor_type: str, updater: HoymilesDTUUpdater):
        self._name = name
        self._type = sensor_type
        self._updater = updater
        info = METER_TYPES[sensor_type]
        self._attr_name = f"{name} {info[0]}"
        self._attr_unique_id = f"hoymiles_{name.lower()}_meter_{sensor_type}"
        self._attr_native_unit_of_measurement = info[1]
        self._attr_device_class = info[2]
        self._attr_state_class = info[3]

    @property
    def native_value(self):
        if self._updater.data is None:
            return None
        meter = self._updater.data.get("meter")
        if meter is None:
            return None
        return meter.get(self._type)

    def update(self):
        self._updater.update()
