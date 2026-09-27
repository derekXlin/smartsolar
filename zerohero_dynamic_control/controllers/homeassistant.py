"""Home Assistant controller, driven over the REST API.

The most likely near-term integration path: whatever the battery turns out to be,
if it has a Home Assistant integration it will expose a mode `select` and a power
`number`. Map those entity ids in config.yaml and this works unchanged.

    controller:
      type: homeassistant
      options:
        base_url: http://homeassistant.local:8123
        token: ${HA_TOKEN}
        mode_entity: select.battery_operating_mode
        power_entity: number.battery_discharge_power
        mode_map:
          self_consumption: "Self Consumption"
          force_export: "Forced Discharge"
          force_charge: "Forced Charge"
          hold: "Stop"
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..models import BatteryMode
from .base import BatteryController, ControllerCapabilities, ControllerError

log = logging.getLogger(__name__)


class HomeAssistantController(BatteryController):
    name = "homeassistant"

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__()
        self.base_url = str(options.get("base_url", "")).rstrip("/")
        self.token = options.get("token")
        self.mode_entity = options.get("mode_entity")
        self.power_entity = options.get("power_entity")
        self.soc_entity = options.get("soc_target_entity")
        self.mode_map: dict[str, str] = options.get("mode_map", {})
        self.power_unit_w = bool(options.get("power_entity_in_watts", False))
        self._client = None

    def capabilities(self) -> ControllerCapabilities:
        return ControllerCapabilities(
            supports_power_setpoint=self.power_entity is not None,
            supports_soc_target=self.soc_entity is not None,
            min_command_interval_seconds=5.0,
        )

    async def _client_or_raise(self):
        if self._client is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover
                raise ControllerError("httpx is required for the Home Assistant controller") from exc
            if not self.base_url or not self.token:
                raise ControllerError("Home Assistant base_url and token must be configured")
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10.0,
            )
        return self._client

    async def _service(self, domain: str, service: str, payload: dict[str, Any]) -> None:
        client = await self._client_or_raise()
        resp = await client.post(f"/api/services/{domain}/{service}", json=payload)
        if resp.status_code >= 400:
            raise ControllerError(f"HA {domain}.{service} failed: {resp.status_code} {resp.text}")

    async def set_mode(self, mode: BatteryMode, *, now: datetime, reason: str = "") -> None:
        if not self.mode_entity:
            raise ControllerError("mode_entity is not configured")
        option = self.mode_map.get(mode.value)
        if option is None:
            raise ControllerError(f"no mode_map entry for {mode.value}")
        log.info("HA set_mode %s -> %s (%s)", self.mode_entity, option, reason)
        await self._service("select", "select_option",
                            {"entity_id": self.mode_entity, "option": option})

    async def set_power(self, power_kw: float, *, now: datetime, reason: str = "") -> None:
        if not self.power_entity:
            return
        value = abs(power_kw) * (1000.0 if self.power_unit_w else 1.0)
        log.info("HA set_power %s -> %.2f (%s)", self.power_entity, value, reason)
        await self._service("number", "set_value",
                            {"entity_id": self.power_entity, "value": round(value, 2)})

    async def set_soc_target(self, soc_pct: float, *, now: datetime, reason: str = "") -> None:
        if not self.soc_entity:
            raise NotImplementedError
        await self._service("number", "set_value",
                            {"entity_id": self.soc_entity, "value": round(soc_pct, 1)})

    async def health_check(self) -> bool:
        try:
            client = await self._client_or_raise()
            resp = await client.get("/api/")
            return resp.status_code < 400
        except Exception as exc:  # noqa: BLE001
            log.warning("HA health check failed: %s", exc)
            return False

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
