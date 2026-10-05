"""What a device still needs set up before it's worth showing."""

from ..models import DeviceConfig


def setup_missing(config: DeviceConfig) -> list[str]:
    missing = []
    if not config.weather_location.strip():
        missing.append("a weather location")
    if not config.postcode.strip():
        missing.append("a postcode")
    if not config.tfl_stop_ids:
        missing.append("a bus or tube stop")
    return missing
