"""Binary sensor platform for the Tech Sterowniki integration.

Three flavours of binary entity are emitted:

* **Relays** -- TYPE_RELAY (=11) tiles ("Pompa CO", "Pompa CWU",
  "Podajnik" etc.) and TYPE_ADDITIONAL_PUMP (=21) tiles, both backed by
  :class:`RelaySensor`. The on/off state comes from the tile's
  ``workingStatus`` boolean and is refreshed by the coordinator on the
  60-second polling cadence (see :data:`const.SCAN_INTERVAL`).
* **Fire/motion sensors** -- TYPE_FIRE_SENSOR (=2) tiles, also handled by
  :class:`RelaySensor` but with the MOTION device class so the HA UI
  shows the correct iconography.
* **Contact widgets** -- TYPE_WIDGET (=6) sub-payloads with the
  contact-shape marker (``unit==-1, type==0, txtId!=0``), backed by
  :class:`TileWidgetContactSensor`. EU-i-3+ extension modules expose all
  four of their voltage / potential-free inputs this way.

Contact widgets share their parent tile with numeric widgets that go to
:mod:`sensor`. Both modules use the same :func:`_is_contact_widget`
predicate to decide which platform owns each widget; without that
agreement the same widget could be emitted twice (or not at all).
"""

import logging

from homeassistant.components import binary_sensor
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ID,
    CONF_PARAMS,
    CONF_TYPE,
    STATE_OFF,
    STATE_ON,
    EntityCategory,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType, UndefinedType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TechCoordinator, assets
from .const import (
    CONTROLLER,
    DOMAIN,
    MENU_DEPTH_DEFAULT_ENABLED_LIMIT,
    MANUFACTURER,
    TYPE_ADDITIONAL_PUMP,
    TYPE_FIRE_SENSOR,
    TYPE_RELAY,
    TYPE_WIDGET,
    UDID,
    VALUE,
    VISIBILITY,
)
from .entity import TileEntity

_LOGGER = logging.getLogger(__name__)


def _is_contact_widget(widget: dict) -> bool:
    """Return ``True`` for widgets that should be exposed as binary contacts."""
    return (
        widget.get("unit") == -1
        and widget.get(CONF_TYPE) == 0
        and widget.get("txtId", 0) != 0
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tech binary sensors for a newly created config entry.

    Args:
        hass: Home Assistant instance.
        config_entry: Integration entry containing controller metadata.
        async_add_entities: Callback used to register entities with Home Assistant.

    """
    _LOGGER.debug("Setting up entry for sensors…")
    controller = config_entry.data[CONTROLLER]
    coordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities = []
    # for controller in controllers:
    controller_udid = controller[UDID]
    tiles = await coordinator.api.get_module_tiles(controller_udid)
    menus = await coordinator.api.get_module_menus(controller_udid)

    zones = await coordinator.api.get_module_zones(controller_udid)
    ctx = assets.build_menu_context(
        menus, zones, coordinator.translations
    )
    # _LOGGER.debug("Setting up entry for binary sensors...tiles: %s", tiles)
    for t in tiles:
        tile = tiles[t]
        if tile[VISIBILITY] is False:
            continue
        if tile[CONF_TYPE] == TYPE_RELAY:
            entities.append(RelaySensor(tile, coordinator, config_entry))
        if tile[CONF_TYPE] == TYPE_FIRE_SENSOR:
            entities.append(
                RelaySensor(
                    tile,
                    coordinator,
                    config_entry,
                    binary_sensor.BinarySensorDeviceClass.MOTION,
                )
            )
        if tile[CONF_TYPE] == TYPE_ADDITIONAL_PUMP:
            entities.append(RelaySensor(tile, coordinator, config_entry))
        if tile[CONF_TYPE] == TYPE_WIDGET:
            params = tile.get(CONF_PARAMS, {})
            for widget_key in ("widget1", "widget2"):
                widget = tile.get(CONF_PARAMS, {}).get(widget_key)
                if widget and widget.get("txtId", 0) != 0 and "statusId" in params:
                    entities.append(
                        TileStatusSensor(tile, coordinator, config_entry, widget_key)
                    )                
                    break 
            for widget_key in ("widget1", "widget2"):
                widget = tile.get(CONF_PARAMS, {}).get(widget_key)
                if widget and _is_contact_widget(widget):
                    entities.append(
                        TileWidgetContactSensor(
                            tile, coordinator, config_entry, widget_key
                        )
                    )

    for key, item in menus.items():
        if "duringChange" in item:
            entities.append(
                MenuDuringChangeSensor(
                    item,
                    key,
                    coordinator,
                    config_entry,
                    ctx.group_names,
                    depth=ctx.depths[key],
                    zone_id=ctx.zone_assignments.get(key),
                )
            )

    async_add_entities(entities, True)


class TileBinarySensor(TileEntity, binary_sensor.BinarySensorEntity):
    """Base class for Tech tiles that expose binary sensor semantics."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def get_state(self, device):
        """Return the raw binary state extracted from ``device`` data."""

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return f"{self._unique_id}_tile_binary_sensor"

    @property
    def name(self) -> str | UndefinedType | None:
        """Return the name of the device."""
        return self._name

    @property
    def state(self) -> str | int | float | StateType | None:
        """Get the state of the binary sensor."""
        return STATE_ON if self._state else STATE_OFF


class RelaySensor(TileBinarySensor):
    """Representation of a RelaySensor."""

    def __init__(
        self, device, coordinator: TechCoordinator, config_entry, device_class=None
    ) -> None:
        """Initialize the relay-backed binary sensor tile.

        Args:
            device: Tile payload returned from the Tech API.
            coordinator: Shared Tech data coordinator instance.
            config_entry: Config entry providing controller metadata.
            device_class: Optional Home Assistant device class for the sensor.

        """
        TileBinarySensor.__init__(self, device, coordinator, config_entry)
        self._attr_device_class = device_class
        self._coordinator = coordinator
        icon_id = device[CONF_PARAMS].get("iconId")
        if icon_id:
            self._attr_icon = assets.get_icon(icon_id)
        else:
            self._attr_icon = assets.get_icon_by_type(device[CONF_TYPE])

    def get_state(self, device):
        """Return the on/off working status for the provided ``device`` payload."""
        return device[CONF_PARAMS]["workingStatus"]


class TileStatusSensor(TileBinarySensor):
    """Binary sensor representing a tile's statusId."""

    _attr_has_entity_name = True

    def __init__(
        self,
        device,
        coordinator: TechCoordinator,
        config_entry,
        widget_key: str
    ) -> None:
        """Initialize the status sensor."""
        self._widget_key = widget_key
        TileBinarySensor.__init__(self, device, coordinator, config_entry)
        self._unique_id = f"{self._udid}_{device[CONF_ID]}_status"
        widget = device[CONF_PARAMS][widget_key]
        self._name = coordinator.translations.get_text(widget["txtId"])
        self._attr_translation_key = "tile_status_entity"
        self._attr_translation_placeholders = {"entity_name": self._name}

        icon_id = device[CONF_PARAMS].get("iconId")
        if icon_id:
            self._attr_icon = assets.get_icon(icon_id)

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return f"{self._unique_id}_tile_status"

    def get_state(self, device):
        """Return the tile status from statusId."""
        return device[CONF_PARAMS].get("statusId", 0) == 1


class TileWidgetContactSensor(TileBinarySensor):
    """A widget-shaped contact (e.g. EU-i-3+ voltage / potential-free input).

    Detected by ``unit == -1`` and ``type == 0`` on a TYPE_WIDGET tile widget.
    Exposed as an opening device-class binary sensor; ``value == 1`` means open.
    """

    _attr_device_class = binary_sensor.BinarySensorDeviceClass.OPENING

    def __init__(
        self,
        device,
        coordinator: TechCoordinator,
        config_entry,
        widget_key: str,
    ) -> None:
        """Initialise the contact widget binary sensor.

        Args:
            device: Tile payload returned from the Tech API.
            coordinator: Shared Tech data coordinator instance.
            config_entry: Config entry providing controller metadata.
            widget_key: ``"widget1"`` or ``"widget2"`` - which widget within the
                tile this entity represents.

        """
        self._widget_key = widget_key
        TileBinarySensor.__init__(self, device, coordinator, config_entry)
        widget = device[CONF_PARAMS][widget_key]
        # Contact widgets carry their label inside the widget payload, not the
        # tile params, so override the name TileEntity computed from tile-level
        # txtId. ``_attr_has_entity_name = True`` lets HA prepend the device.
        self._name = coordinator.translations.get_text(widget["txtId"])
        icon_id = device[CONF_PARAMS].get("iconId")
        if icon_id:
            self._attr_icon = assets.get_icon(icon_id)

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return f"{self._unique_id}_tile_widget_contact_{self._widget_key}"

    def get_state(self, device):
        """Return the contact state from the widget value."""
        return device[CONF_PARAMS][self._widget_key][VALUE] == 1


class MenuDuringChangeSensor(
    CoordinatorEntity, binary_sensor.BinarySensorEntity
):
    """Diagnostic binary sensor indicating that a menu value is being updated."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        item,
        menu_key: str,
        coordinator: TechCoordinator,
        config_entry: ConfigEntry,
        group_names,
        depth: int = 0,
        zone_id: int | None = None,
    ) -> None:
        """Initialize the during-change sensor."""
        super().__init__(coordinator)

        self._config_entry = config_entry
        self._coordinator = coordinator
        self._udid = config_entry.data[CONTROLLER][UDID]
        self._menu_key = menu_key
        self._zone_id = zone_id

        self._unique_id = f"{self._udid}_menu_during_change_{menu_key}"

        self._name = assets.menu_entity_name(
            item, group_names, coordinator.translations
        )
        self._attr_translation_key = "menu_during_change_entity"
        self._attr_translation_placeholders = {"entity_name": self._name}

        self._disabled = depth > MENU_DEPTH_DEFAULT_ENABLED_LIMIT
        self._attr_is_on = item.get("duringChange") == "t"

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return self._unique_id

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Return whether the entity should be enabled by default."""
        return not self._disabled

    @callback
    def _handle_coordinator_update(self, *args) -> None:
        """Handle updated data from the coordinator."""
        menus = self._coordinator.data.get("menus", {})
        item = menus.get(self._menu_key)

        if item:
            self._attr_is_on = item.get("duringChange") == "t"

        self.async_write_ha_state()