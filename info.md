# Cheapest Energy Windows

## What is this?

This integration optimizes your energy consumption and battery storage by automatically identifying the cheapest charging windows and most expensive discharging periods based on dynamic electricity prices.

Perfect for users with:
- Variable electricity pricing (spot prices)
- Home battery systems
- Solar installations with feed-in tariffs
- Electric vehicle charging needs

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=cheapest-energy-windows&repository=cheapest_energy_windows&category=integration)

## Key Features

- **Automatic Window Detection** - Finds optimal charge/discharge times
- **Statistical Analysis** - Uses percentile-based selection for accuracy
- **Battery Optimization** - Considers round-trip efficiency
- **PV Forecast Optimization** - Reduces grid charging when forecast solar can cover demand
- **Dual-Day Management** - Different settings for today/tomorrow
- **Time Overrides** - Force charging during specific periods
- **Full Dashboard** - Complete control interface included

## Quick Start

> ✨ Click the badge above for one-click HACS installation, or follow these steps:

1. Open HACS in your Home Assistant instance
2. Click on "Integrations"
3. Search for "Cheapest Energy Windows"
4. Click "Download"
5. Restart Home Assistant
6. Go to Settings > Devices & Services > "Add Integration"
7. Search for "Cheapest Energy Windows"
8. Follow guided setup wizard
9. Start saving on energy costs!

## Requirements

- Home Assistant 2024.1.0 or newer
- Electricity price sensor (Nordpool, ENTSO-E, Tibber, etc.)

## Support

- [Documentation](https://github.com/cheapest-energy-windows/cheapest_energy_windows)
- [Report Issues](https://github.com/cheapest-energy-windows/cheapest_energy_windows/issues)
- [Community Discussion](https://community.home-assistant.io/)

{% if installed %}
## Installed Version: {{ version }}

Thank you for using Cheapest Energy Windows!

### Quick Actions
- Call service `cheapest_energy_windows.install_dashboard` to install dashboard
- Check `sensor.cew_today` for current state
- Configure settings through the dashboard
{% endif %}
