"""Calculation engine for Cheapest Energy Windows."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from homeassistant.util import dt as dt_util

from .const import (
    LOGGER_NAME,
    PRICING_15_MINUTES,
    PRICING_1_HOUR,
    MODE_CHARGE,
    MODE_DISCHARGE,
    MODE_DISCHARGE_AGGRESSIVE,
    MODE_IDLE,
    MODE_OFF,
    STATE_CHARGE,
    STATE_DISCHARGE,
    STATE_DISCHARGE_AGGRESSIVE,
    STATE_IDLE,
    STATE_OFF,
)

_LOGGER = logging.getLogger(LOGGER_NAME)


class WindowCalculationEngine:
    """High-performance window selection engine."""

    def __init__(self) -> None:
        """Initialize the calculation engine."""
        pass

    def calculate_windows(
        self,
        raw_prices: List[Dict[str, Any]],
        config: Dict[str, Any],
        is_tomorrow: bool = False
    ) -> Dict[str, Any]:
        """Calculate optimal charging/discharging windows.

        Args:
            raw_prices: List of price data from NordPool or similar
            config: Configuration from input entities
            is_tomorrow: Whether calculating for tomorrow

        Returns:
            Dictionary with calculated windows and attributes
        """
        # Debug logging for calculation window
        _LOGGER.debug(f"=== CALCULATION ENGINE CALLED for {'tomorrow' if is_tomorrow else 'today'} ===")
        _LOGGER.debug(f"Config keys received: {list(config.keys())}")
        _LOGGER.debug(f"calculation_window_enabled in config: {config.get('calculation_window_enabled', 'NOT PRESENT')}")
        _LOGGER.debug(f"calculation_window_start: {config.get('calculation_window_start', 'NOT PRESENT')}")
        _LOGGER.debug(f"calculation_window_end: {config.get('calculation_window_end', 'NOT PRESENT')}")

        # Get configuration values
        pricing_mode = config.get("pricing_window_duration", PRICING_15_MINUTES)

        # Use tomorrow's config if applicable
        suffix = "_tomorrow" if is_tomorrow and config.get("tomorrow_settings_enabled", False) else ""

        num_charge_windows = int(config.get(f"charging_windows{suffix}", 4))
        num_discharge_windows = int(config.get(f"expensive_windows{suffix}", 4))
        cheap_percentile = config.get(f"cheap_percentile{suffix}", 25)
        expensive_percentile = config.get(f"expensive_percentile{suffix}", 25)
        min_spread = config.get(f"min_spread{suffix}", 10)
        min_spread_discharge = config.get(f"min_spread_discharge{suffix}", 20)
        aggressive_spread = config.get(f"aggressive_discharge_spread{suffix}", 40)
        min_price_diff = config.get(f"min_price_difference{suffix}", 0.05)
        if not config.get(f"min_buy_price_diff_enabled{suffix}", True):
            min_price_diff = 0.0

        # Cost calculations
        vat = config.get("vat", 0.21)
        tax = config.get("tax", 0.12286)
        additional_cost = config.get("additional_cost", 0.02398)

        # Process prices based on mode
        processed_prices = self._process_prices(
            raw_prices, pricing_mode, vat, tax, additional_cost
        )

        if not processed_prices:
            _LOGGER.debug("No prices to process")
            return self._empty_result(is_tomorrow)

        # Apply calculation window filter if enabled (use suffix for tomorrow settings)
        calc_window_enabled = config.get(f"calculation_window_enabled{suffix}", False)
        if calc_window_enabled:
            calc_window_start = config.get(f"calculation_window_start{suffix}", "00:00:00")
            calc_window_end = config.get(f"calculation_window_end{suffix}", "23:59:59")
            _LOGGER.debug(f"Calculation window ENABLED: {calc_window_start} - {calc_window_end}, filtering {len(processed_prices)} prices")
            processed_prices = self._filter_prices_by_calculation_window(
                processed_prices,
                calc_window_start,
                calc_window_end
            )
            _LOGGER.debug(f"After calculation window filter: {len(processed_prices)} prices remain")
            if not processed_prices:
                _LOGGER.debug("No prices after calculation window filter")
                return self._empty_result(is_tomorrow)
        else:
            _LOGGER.debug("Calculation window disabled")

        # Pre-filter prices based on time override to prevent idle/off periods from being selected
        # This ensures that windows calculations respect time overrides from the start
        time_override_enabled = config.get(f"time_override_enabled{suffix}", False)
        prices_for_charge_calc = processed_prices
        prices_for_discharge_calc = processed_prices

        if time_override_enabled:
            override_mode = config.get(f"time_override_mode{suffix}", MODE_IDLE)

            # Get time values and ensure they're in string format
            override_start = config.get(f"time_override_start{suffix}", "")
            override_end = config.get(f"time_override_end{suffix}", "")

            # Convert to string format if needed
            if hasattr(override_start, 'strftime'):
                override_start_str = override_start.strftime("%H:%M:%S")
            elif override_start:
                override_start_str = str(override_start)
            else:
                override_start_str = ""

            if hasattr(override_end, 'strftime'):
                override_end_str = override_end.strftime("%H:%M:%S")
            elif override_end:
                override_end_str = str(override_end)
            else:
                override_end_str = ""

            if override_start_str and override_end_str:
                _LOGGER.debug(f"Time override enabled: {override_start_str} - {override_end_str}, mode: {override_mode}")

                # For idle/off modes, exclude override periods from window calculations
                if override_mode in [MODE_IDLE, MODE_OFF]:
                    filtered_prices = []
                    for price_data in processed_prices:
                        if not self._is_in_time_range(price_data["timestamp"], override_start_str, override_end_str):
                            filtered_prices.append(price_data)
                    prices_for_charge_calc = filtered_prices
                    prices_for_discharge_calc = filtered_prices
                    _LOGGER.debug(f"Filtered {len(processed_prices)} prices to {len(filtered_prices)} after excluding {override_mode} periods")

                # For charge mode, only charge windows should be in override period
                elif override_mode == MODE_CHARGE:
                    # Charge windows: only consider prices within override period
                    charge_override_prices = []
                    for price_data in processed_prices:
                        if self._is_in_time_range(price_data["timestamp"], override_start_str, override_end_str):
                            charge_override_prices.append(price_data)
                    prices_for_charge_calc = charge_override_prices
                    # Discharge windows: exclude override period
                    discharge_filtered = []
                    for price_data in processed_prices:
                        if not self._is_in_time_range(price_data["timestamp"], override_start_str, override_end_str):
                            discharge_filtered.append(price_data)
                    prices_for_discharge_calc = discharge_filtered
                    _LOGGER.debug(f"Charge mode: {len(charge_override_prices)} prices for charging, {len(discharge_filtered)} for discharge")

                # For discharge modes, only discharge windows should be in override period
                elif override_mode in [MODE_DISCHARGE, MODE_DISCHARGE_AGGRESSIVE]:
                    # Charge windows: exclude override period
                    charge_filtered = []
                    for price_data in processed_prices:
                        if not self._is_in_time_range(price_data["timestamp"], override_start_str, override_end_str):
                            charge_filtered.append(price_data)
                    prices_for_charge_calc = charge_filtered
                    # Discharge windows: only consider prices within override period
                    discharge_override_prices = []
                    for price_data in processed_prices:
                        if self._is_in_time_range(price_data["timestamp"], override_start_str, override_end_str):
                            discharge_override_prices.append(price_data)
                    prices_for_discharge_calc = discharge_override_prices
                    _LOGGER.debug(f"Discharge mode: {len(charge_filtered)} prices for charging, {len(discharge_override_prices)} for discharge")

        # PV forecast can reduce the number of grid charge windows
        pv_adjustment = self._calculate_pv_adjusted_charge_windows(
            prices_for_charge_calc,
            num_charge_windows,
            config,
            is_tomorrow,
        )
        num_charge_windows = pv_adjustment["pv_adjusted_charge_windows"]

        # Find windows using the pre-filtered prices
        charge_windows = self._find_charge_windows(
            prices_for_charge_calc,  # Use filtered prices
            num_charge_windows,
            cheap_percentile,
            min_spread,
            min_price_diff
        )

        discharge_windows = self._find_discharge_windows(
            prices_for_discharge_calc,  # Use filtered prices
            charge_windows,
            num_discharge_windows,
            expensive_percentile,
            min_spread_discharge,
            min_price_diff
        )

        aggressive_windows = self._find_aggressive_discharge_windows(
            prices_for_discharge_calc,  # Use filtered prices for consistency
            charge_windows,
            discharge_windows,
            num_discharge_windows,
            expensive_percentile,
            aggressive_spread,
            min_price_diff
        )

        # Debug output when calculation window is enabled
        if calc_window_enabled:
            charge_times = [w["timestamp"].strftime("%H:%M") for w in charge_windows]
            discharge_times = [w["timestamp"].strftime("%H:%M") for w in discharge_windows]
            _LOGGER.debug(f"After calculation window filter - Charge windows: {charge_times}, Discharge windows: {discharge_times}")

        # Calculate current state
        current_state = self._determine_current_state(
            processed_prices,
            charge_windows,
            discharge_windows,
            aggressive_windows,
            config
        )

        # Build result
        result = self._build_result(
            processed_prices,
            charge_windows,
            discharge_windows,
            aggressive_windows,
            current_state,
            config,
            is_tomorrow,
            pv_adjustment,
        )

        return result

    def _process_prices(
        self,
        raw_prices: List[Dict[str, Any]],
        pricing_mode: str,
        vat: float,
        tax: float,
        additional_cost: float
    ) -> List[Dict[str, Any]]:
        """Process raw prices with VAT, tax, and additional costs."""
        _LOGGER.debug("="*60)
        _LOGGER.debug("PROCESS PRICES START")
        _LOGGER.debug(f"Raw prices type: {type(raw_prices)}")
        _LOGGER.debug(f"Raw prices length: {len(raw_prices) if hasattr(raw_prices, '__len__') else 'N/A'}")
        _LOGGER.debug(f"Pricing mode: {pricing_mode}")
        _LOGGER.debug(f"VAT: {vat} (type: {type(vat)})")
        _LOGGER.debug(f"Tax: {tax} (type: {type(tax)})")
        _LOGGER.debug(f"Additional cost: {additional_cost} (type: {type(additional_cost)})")

        if raw_prices and len(raw_prices) > 0:
            _LOGGER.debug(f"First item type: {type(raw_prices[0])}")
            _LOGGER.debug(f"First item: {raw_prices[0]}")
            if len(raw_prices) > 1:
                _LOGGER.debug(f"Second item: {raw_prices[1]}")

        processed = []

        if pricing_mode == PRICING_1_HOUR:
            # Group by hour and average
            hourly_prices = {}
            for item in raw_prices:
                try:
                    # Validate item is a dict
                    if not isinstance(item, dict):
                        _LOGGER.error(f"Item is not a dict! Type: {type(item)}, Value: {item}")
                        continue

                    # Parse timestamp - handle both datetime objects and strings
                    start_value = item.get("start")
                    if not start_value:
                        _LOGGER.warning(f"Item has no 'start' key: {item}")
                        continue

                    if isinstance(start_value, datetime):
                        # Already a datetime object (new Nordpool format)
                        timestamp = start_value
                    elif isinstance(start_value, str):
                        # String format (old format)
                        timestamp_str = start_value.replace('"', '')
                        timestamp = datetime.fromisoformat(timestamp_str)
                    else:
                        _LOGGER.error(f"Unexpected start type: {type(start_value)}, Value: {start_value}")
                        continue

                    hour = timestamp.replace(minute=0, second=0, microsecond=0)

                    if hour not in hourly_prices:
                        hourly_prices[hour] = []

                    # Calculate total price
                    base_price = item.get("value", 0)
                    total_price = (base_price * (1 + vat)) + tax + additional_cost
                    hourly_prices[hour].append(total_price)

                except (ValueError, TypeError, AttributeError) as e:
                    _LOGGER.error(f"Failed to process price item: {e}", exc_info=True)
                    _LOGGER.error(f"Problematic item: {item}")
                    continue

            # Average hourly prices
            for hour, prices in hourly_prices.items():
                if prices:
                    processed.append({
                        "timestamp": hour,
                        "price": float(np.mean(prices)),  # Convert numpy.float64 to Python float
                        "duration": 60  # 60 minutes
                    })

        else:  # 15-minute mode
            for item in raw_prices:
                try:
                    # Validate item is a dict
                    if not isinstance(item, dict):
                        _LOGGER.error(f"Item is not a dict! Type: {type(item)}, Value: {item}")
                        continue

                    # Parse timestamp - handle both datetime objects and strings
                    start_value = item.get("start")
                    if not start_value:
                        _LOGGER.warning(f"Item has no 'start' key: {item}")
                        continue

                    if isinstance(start_value, datetime):
                        # Already a datetime object (new Nordpool format)
                        timestamp = start_value
                    elif isinstance(start_value, str):
                        # String format (old format)
                        timestamp_str = start_value.replace('"', '')
                        timestamp = datetime.fromisoformat(timestamp_str)
                    else:
                        _LOGGER.error(f"Unexpected start type: {type(start_value)}, Value: {start_value}")
                        continue

                    base_price = item.get("value", 0)
                    total_price = (base_price * (1 + vat)) + tax + additional_cost

                    processed.append({
                        "timestamp": timestamp,
                        "price": total_price,
                        "duration": 15  # 15 minutes
                    })

                except (ValueError, TypeError, AttributeError) as e:
                    _LOGGER.error(f"Failed to process price item: {e}", exc_info=True)
                    _LOGGER.error(f"Problematic item: {item}")
                    continue

        # Sort by timestamp
        processed.sort(key=lambda x: x["timestamp"])

        _LOGGER.debug(f"Processed {len(processed)} price entries")
        if processed:
            _LOGGER.debug(f"First processed price: {processed[0]}")
            _LOGGER.debug(f"Last processed price: {processed[-1]}")
        _LOGGER.debug("PROCESS PRICES END")
        _LOGGER.debug("="*60)

        return processed

    def _filter_prices_by_calculation_window(
        self,
        prices: List[Dict[str, Any]],
        start_str: str,
        end_str: str
    ) -> List[Dict[str, Any]]:
        """Filter prices to only include those within the calculation window time range.

        This restricts the price analysis to a specific time window each day.
        For example, if you only want to charge/discharge between 06:00-22:00,
        set the calculation window to those times.
        """
        if not prices:
            return prices

        filtered = []

        try:
            # Parse time strings (HH:MM:SS format)
            start_parts = start_str.split(":")
            end_parts = end_str.split(":")

            start_hour = int(start_parts[0])
            start_minute = int(start_parts[1])
            end_hour = int(end_parts[0])
            end_minute = int(end_parts[1])

            for price_data in prices:
                timestamp = price_data["timestamp"]
                price_hour = timestamp.hour
                price_minute = timestamp.minute

                # Convert to minutes since midnight for easier comparison
                price_time = price_hour * 60 + price_minute
                start_time = start_hour * 60 + start_minute
                end_time = end_hour * 60 + end_minute

                # Handle overnight periods
                if end_time < start_time:
                    # Overnight: include if time >= start OR time < end
                    if price_time >= start_time or price_time < end_time:
                        filtered.append(price_data)
                else:
                    # Same day: include if start <= time < end
                    if start_time <= price_time < end_time:
                        filtered.append(price_data)

            _LOGGER.debug(f"Calculation window filter: {len(prices)} -> {len(filtered)} prices (window: {start_str} to {end_str})")

        except (ValueError, IndexError, AttributeError) as e:
            _LOGGER.error(f"Failed to parse calculation window times: {e}")
            return prices  # Return unfiltered on error

        return filtered

    def _calculate_pv_adjusted_charge_windows(
        self,
        prices: List[Dict[str, Any]],
        configured_charge_windows: int,
        config: Dict[str, Any],
        is_tomorrow: bool,
    ) -> Dict[str, Any]:
        """Reduce planned grid charge windows using PV forecast and SoC target."""
        result = {
            "pv_adjustment_active": False,
            "pv_forecast_kwh_used": 0.0,
            "soc_target_sunrise": float(config.get("soc_target_sunrise", 0)),
            "current_soc": config.get("current_soc"),
            "battery_capacity_kwh": config.get("battery_capacity_kwh"),
            "required_charge_kwh": 0.0,
            "pv_offset_kwh": 0.0,
            "net_grid_charge_kwh": 0.0,
            "configured_charge_windows": int(configured_charge_windows),
            "pv_adjusted_charge_windows": int(configured_charge_windows),
            "winter_reserve_active": False,
            "pv_fallback_reason": "",
        }

        if not config.get("pv_forecast_enabled", False):
            result["pv_fallback_reason"] = "disabled"
            return result

        current_soc = config.get("current_soc")
        battery_capacity_kwh = config.get("battery_capacity_kwh")
        if current_soc is None:
            result["pv_fallback_reason"] = "missing_soc"
            return result
        if battery_capacity_kwh is None or battery_capacity_kwh <= 0:
            result["pv_fallback_reason"] = "missing_capacity"
            return result

        pv_forecast_kwh = (
            config.get("pv_forecast_tomorrow_kwh")
            if is_tomorrow
            else config.get("pv_forecast_remaining_today_kwh")
        )
        if pv_forecast_kwh is None or pv_forecast_kwh < 0:
            result["pv_fallback_reason"] = "missing_pv"
            return result

        if not prices:
            result["pv_fallback_reason"] = "missing_prices"
            return result

        charge_power_kw = float(config.get("charge_power", 0)) / 1000
        window_duration_hours = prices[0]["duration"] / 60
        energy_per_window_kwh = charge_power_kw * window_duration_hours
        if energy_per_window_kwh <= 0:
            result["pv_fallback_reason"] = "invalid_charge_power"
            return result

        soc_target = float(config.get("soc_target_sunrise", 0))
        required_charge_kwh = max(0.0, ((soc_target - float(current_soc)) / 100.0) * float(battery_capacity_kwh))
        pv_offset_kwh = min(required_charge_kwh, float(pv_forecast_kwh))
        net_grid_charge_kwh = max(0.0, required_charge_kwh - pv_offset_kwh)

        winter_reserve_active = False
        if config.get("winter_reserve_enabled", False):
            winter_months = config.get("winter_months_list", [11, 12, 1, 2])
            reference_dt = dt_util.now() + timedelta(days=1 if is_tomorrow else 0)
            current_month = reference_dt.month
            if current_month in winter_months:
                winter_reserve_active = True
                winter_min_soc = float(config.get("winter_min_soc", 0))
                winter_floor_kwh = max(
                    0.0,
                    ((winter_min_soc - float(current_soc)) / 100.0) * float(battery_capacity_kwh),
                )
                net_grid_charge_kwh = max(net_grid_charge_kwh, winter_floor_kwh)

        adjusted_windows = int(np.ceil(net_grid_charge_kwh / energy_per_window_kwh)) if net_grid_charge_kwh > 0 else 0
        adjusted_windows = max(0, min(int(configured_charge_windows), adjusted_windows))

        result.update(
            {
                "pv_adjustment_active": adjusted_windows != int(configured_charge_windows),
                "pv_forecast_kwh_used": round(pv_offset_kwh, 3),
                "required_charge_kwh": round(required_charge_kwh, 3),
                "pv_offset_kwh": round(pv_offset_kwh, 3),
                "net_grid_charge_kwh": round(net_grid_charge_kwh, 3),
                "pv_adjusted_charge_windows": adjusted_windows,
                "winter_reserve_active": winter_reserve_active,
                "pv_fallback_reason": "",
            }
        )
        return result

    def _find_charge_windows(
        self,
        prices: List[Dict[str, Any]],
        num_windows: int,
        cheap_percentile: float,
        min_spread: float,
        min_price_diff: float
    ) -> List[Dict[str, Any]]:
        """Find cheapest windows for charging."""
        if not prices or num_windows <= 0:
            return []

        # Convert to numpy array for efficient operations
        price_array = np.array([p["price"] for p in prices])

        # Calculate percentile threshold
        cheap_threshold = np.percentile(price_array, cheap_percentile)

        # Get candidates below threshold
        candidates = []
        for i, price_data in enumerate(prices):
            if price_data["price"] <= cheap_threshold:
                candidates.append({
                    "index": i,
                    "timestamp": price_data["timestamp"],
                    "price": price_data["price"],
                    "duration": price_data["duration"]
                })

        # Sort by price
        candidates.sort(key=lambda x: x["price"])

        # Progressive selection with spread check
        selected = []
        expensive_avg = np.mean(price_array[price_array > np.percentile(price_array, 100 - cheap_percentile)])

        for candidate in candidates:
            if len(selected) >= num_windows:
                break

            # Test spread with this window
            test_prices = [s["price"] for s in selected] + [candidate["price"]]
            cheap_avg = np.mean(test_prices)

            # Calculate spread percentage
            if cheap_avg > 0:
                spread_pct = ((expensive_avg - cheap_avg) / cheap_avg) * 100
                price_diff = expensive_avg - cheap_avg

                if spread_pct >= min_spread and price_diff >= min_price_diff:
                    selected.append(candidate)

        return selected

    def _find_discharge_windows(
        self,
        prices: List[Dict[str, Any]],
        charge_windows: List[Dict[str, Any]],
        num_windows: int,
        expensive_percentile: float,
        min_spread: float,
        min_price_diff: float
    ) -> List[Dict[str, Any]]:
        """Find expensive windows for discharging."""
        if not prices or num_windows <= 0:
            return []

        # Exclude charging times
        charge_indices = {w["index"] for w in charge_windows}

        # Filter out charging windows
        available_prices = []
        for i, price_data in enumerate(prices):
            if i not in charge_indices:
                available_prices.append({
                    "index": i,
                    "timestamp": price_data["timestamp"],
                    "price": price_data["price"],
                    "duration": price_data["duration"]
                })

        if not available_prices:
            return []

        # Convert to numpy array
        price_array = np.array([p["price"] for p in available_prices])

        # Calculate percentile threshold
        expensive_threshold = np.percentile(price_array, 100 - expensive_percentile)

        # Get candidates above threshold
        candidates = []
        for price_data in available_prices:
            if price_data["price"] >= expensive_threshold:
                candidates.append(price_data)

        # Sort by price (descending for discharge)
        candidates.sort(key=lambda x: x["price"], reverse=True)

        # Progressive selection with spread check
        selected = []
        if charge_windows:
            cheap_avg = np.mean([w["price"] for w in charge_windows])
        else:
            cheap_avg = np.mean(price_array[price_array < np.percentile(price_array, expensive_percentile)])

        for candidate in candidates:
            if len(selected) >= num_windows:
                break

            # Test spread with this window
            test_prices = [s["price"] for s in selected] + [candidate["price"]]
            expensive_avg = np.mean(test_prices)

            # Calculate spread percentage
            if cheap_avg > 0:
                spread_pct = ((expensive_avg - cheap_avg) / cheap_avg) * 100
                price_diff = expensive_avg - cheap_avg

                if spread_pct >= min_spread and price_diff >= min_price_diff:
                    selected.append(candidate)

        return selected

    def _find_aggressive_discharge_windows(
        self,
        prices: List[Dict[str, Any]],
        charge_windows: List[Dict[str, Any]],
        discharge_windows: List[Dict[str, Any]],
        num_windows: int,
        expensive_percentile: float,
        aggressive_spread: float,
        min_price_diff: float
    ) -> List[Dict[str, Any]]:
        """Find windows for aggressive discharge (peak prices)."""
        if not prices or num_windows <= 0:
            return []

        # Use discharge windows as base, filter by aggressive spread
        candidates = []

        if charge_windows:
            cheap_avg = np.mean([w["price"] for w in charge_windows])
        else:
            price_array = np.array([p["price"] for p in prices])
            cheap_avg = np.mean(price_array[price_array < np.percentile(price_array, expensive_percentile)])

        for window in discharge_windows:
            if cheap_avg > 0:
                spread_pct = ((window["price"] - cheap_avg) / cheap_avg) * 100
                price_diff = window["price"] - cheap_avg

                if spread_pct >= aggressive_spread and price_diff >= min_price_diff:
                    candidates.append(window)

        return candidates

    def _determine_current_state(
        self,
        prices: List[Dict[str, Any]],
        charge_windows: List[Dict[str, Any]],
        discharge_windows: List[Dict[str, Any]],
        aggressive_windows: List[Dict[str, Any]],
        config: Dict[str, Any]
    ) -> str:
        """Determine current state based on time and configuration."""
        # Check if automation is enabled
        if not config.get("automation_enabled", True):
            return STATE_OFF

        now = dt_util.now()
        current_time = now.replace(second=0, microsecond=0)

        # Check time override
        if config.get("time_override_enabled", False):
            start_str = config.get("time_override_start", "")
            end_str = config.get("time_override_end", "")
            mode = config.get("time_override_mode", MODE_IDLE)

            if self._is_in_time_range(current_time, start_str, end_str):
                return self._mode_to_state(mode)

        # Check price override
        if config.get("price_override_enabled", False):
            threshold = config.get("price_override_threshold", 0.15)
            current_price = self._get_current_price(prices, current_time)
            if current_price and current_price <= threshold:
                return STATE_CHARGE

        # Check scheduled windows
        for window in aggressive_windows:
            if self._is_window_active(window, current_time):
                return STATE_DISCHARGE_AGGRESSIVE

        for window in discharge_windows:
            if self._is_window_active(window, current_time):
                return STATE_DISCHARGE

        for window in charge_windows:
            if self._is_window_active(window, current_time):
                return STATE_CHARGE

        return STATE_IDLE

    def _is_window_active(self, window: Dict[str, Any], current_time: datetime) -> bool:
        """Check if a window is currently active."""
        window_time = window["timestamp"]
        window_duration = window["duration"]

        # Check if current time falls within the window
        window_start = window_time
        window_end = window_time + timedelta(minutes=window_duration)

        return window_start <= current_time < window_end

    def _is_in_time_range(self, current_time: datetime, start_str: str, end_str: str) -> bool:
        """Check if current time is within a time range."""
        try:
            # Parse time strings (HH:MM:SS format)
            start_parts = start_str.split(":")
            end_parts = end_str.split(":")

            start_time = current_time.replace(
                hour=int(start_parts[0]),
                minute=int(start_parts[1]),
                second=0
            )
            end_time = current_time.replace(
                hour=int(end_parts[0]),
                minute=int(end_parts[1]),
                second=0
            )

            # Handle overnight periods
            if end_time < start_time:
                return current_time >= start_time or current_time < end_time
            else:
                return start_time <= current_time < end_time

        except (ValueError, IndexError, AttributeError):
            return False

    def _get_current_price(
        self, prices: List[Dict[str, Any]], current_time: datetime
    ) -> Optional[float]:
        """Get the current price."""
        for price_data in prices:
            if self._is_window_active(price_data, current_time):
                return price_data["price"]
        return None

    def _mode_to_state(self, mode: str) -> str:
        """Convert override mode to state."""
        mode_map = {
            MODE_IDLE: STATE_IDLE,
            MODE_CHARGE: STATE_CHARGE,
            MODE_DISCHARGE: STATE_DISCHARGE,
            MODE_DISCHARGE_AGGRESSIVE: STATE_DISCHARGE_AGGRESSIVE,
            MODE_OFF: STATE_OFF,
        }
        return mode_map.get(mode, STATE_IDLE)

    def _calculate_actual_windows(
        self,
        prices: List[Dict[str, Any]],
        charge_windows: List[Dict[str, Any]],
        discharge_windows: List[Dict[str, Any]],
        aggressive_windows: List[Dict[str, Any]],
        config: Dict[str, Any],
        is_tomorrow: bool = False
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Calculate actual charge/discharge windows considering time and price overrides.

        This shows what the battery will ACTUALLY do when overrides are applied.
        For example:
        - Time override: if 8:00-10:00 is calculated as charge, but 9:00-10:00 has a
          discharge override, the actual charge window will only be 8:00-9:00.
        - Price override: if price drops below threshold, those periods become charge windows
          even if not in calculated windows.

        Args:
            prices: List of processed price data
            charge_windows: Calculated charge windows
            discharge_windows: Calculated discharge windows
            aggressive_windows: Calculated aggressive discharge windows
            config: Configuration dictionary
            is_tomorrow: Whether calculating for tomorrow (affects config key suffix)

        Returns:
            Tuple of (actual_charge_windows, actual_discharge_windows)
        """
        # Use tomorrow's config if applicable
        suffix = "_tomorrow" if is_tomorrow and config.get("tomorrow_settings_enabled", False) else ""

        # Check if any override is enabled
        time_override_enabled = config.get(f"time_override_enabled{suffix}", False)
        price_override_enabled = config.get(f"price_override_enabled{suffix}", False)

        if not time_override_enabled and not price_override_enabled:
            # No overrides, return calculated windows as-is (don't combine normal + aggressive)
            return list(charge_windows), list(discharge_windows)

        # Get override configuration (using suffix for tomorrow settings)
        # Get time values and ensure they're in string format
        override_start = config.get(f"time_override_start{suffix}", "")
        override_end = config.get(f"time_override_end{suffix}", "")
        override_mode = config.get(f"time_override_mode{suffix}", MODE_IDLE)

        # Convert to string format if needed
        if hasattr(override_start, 'strftime'):
            override_start_str = override_start.strftime("%H:%M:%S")
        elif override_start:
            override_start_str = str(override_start)
        else:
            override_start_str = ""

        if hasattr(override_end, 'strftime'):
            override_end_str = override_end.strftime("%H:%M:%S")
        elif override_end:
            override_end_str = str(override_end)
        else:
            override_end_str = ""

        price_override_threshold = config.get(f"price_override_threshold{suffix}", 0.15)

        # Validate time override config if enabled
        if time_override_enabled and (not override_start_str or not override_end_str):
            # Invalid time override config, disable it
            time_override_enabled = False

        # Build a complete timeline of all price windows with their states
        # considering calculated windows, time overrides, and price overrides
        timeline = []

        for price_data in prices:
            timestamp = price_data["timestamp"]
            duration = price_data["duration"]
            price = price_data["price"]

            # Determine state for this time period (priority order: time override > price override > calculated)
            state = STATE_IDLE  # Default

            # Check time override first (highest priority)
            if time_override_enabled and self._is_in_time_range(timestamp, override_start_str, override_end_str):
                state = self._mode_to_state(override_mode)
            # Check price override
            elif price_override_enabled and price <= price_override_threshold:
                state = STATE_CHARGE
            else:
                # Check calculated windows
                for window in aggressive_windows:
                    if self._is_window_active(window, timestamp):
                        state = STATE_DISCHARGE_AGGRESSIVE
                        break

                if state == STATE_IDLE:
                    for window in discharge_windows:
                        if self._is_window_active(window, timestamp):
                            state = STATE_DISCHARGE
                            break

                if state == STATE_IDLE:
                    for window in charge_windows:
                        if self._is_window_active(window, timestamp):
                            state = STATE_CHARGE
                            break

            timeline.append({
                "timestamp": timestamp,
                "price": price_data["price"],
                "duration": duration,
                "state": state
            })

        # Extract actual charge and discharge windows from timeline
        new_actual_charge = [w for w in timeline if w["state"] == STATE_CHARGE]
        new_actual_discharge = [w for w in timeline if w["state"] in [STATE_DISCHARGE, STATE_DISCHARGE_AGGRESSIVE]]

        return new_actual_charge, new_actual_discharge

    def _group_windows(
        self,
        windows: List[Dict[str, Any]],
        reference_price: float,
        mode: str,
    ) -> List[Dict[str, Any]]:
        """Group contiguous windows for dashboard markdown cards."""
        if not windows:
            return []

        sorted_windows = sorted(windows, key=lambda w: w["timestamp"])
        groups: List[List[Dict[str, Any]]] = []
        current_group: List[Dict[str, Any]] = []

        for window in sorted_windows:
            if not current_group:
                current_group = [window]
                continue

            previous = current_group[-1]
            previous_end = previous["timestamp"] + timedelta(minutes=previous["duration"])
            if window["timestamp"] == previous_end:
                current_group.append(window)
            else:
                groups.append(current_group)
                current_group = [window]

        if current_group:
            groups.append(current_group)

        grouped_result = []
        for group in groups:
            first = group[0]
            last = group[-1]
            prices = [float(item["price"]) for item in group]
            avg_price = float(np.mean(prices)) if prices else 0.0
            spread_pct = 0.0
            if reference_price > 0 and avg_price > 0:
                if mode == "charge":
                    spread_pct = ((reference_price - avg_price) / avg_price) * 100
                else:
                    spread_pct = ((avg_price - reference_price) / reference_price) * 100

            group_data = {
                "start_time": first["timestamp"].strftime("%H:%M"),
                "end_time": (last["timestamp"] + timedelta(minutes=last["duration"])).strftime("%H:%M"),
                "num_windows": len(group),
                "avg_price": round(avg_price, 5),
                "spread_pct": round(spread_pct, 1),
                "prices": prices,
            }
            if mode == "discharge":
                group_data["avg_sell_price"] = round(avg_price, 5)
                group_data["sell_prices"] = prices

            grouped_result.append(group_data)

        return grouped_result

    def _build_result(
        self,
        prices: List[Dict[str, Any]],
        charge_windows: List[Dict[str, Any]],
        discharge_windows: List[Dict[str, Any]],
        aggressive_windows: List[Dict[str, Any]],
        current_state: str,
        config: Dict[str, Any],
        is_tomorrow: bool,
        pv_adjustment: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build the result dictionary with all attributes."""
        suffix = "_tomorrow" if is_tomorrow and config.get("tomorrow_settings_enabled", False) else ""
        pv_adjustment = pv_adjustment or {
            "pv_adjustment_active": False,
            "pv_forecast_kwh_used": 0.0,
            "soc_target_sunrise": float(config.get("soc_target_sunrise", 0)),
            "current_soc": config.get("current_soc"),
            "battery_capacity_kwh": config.get("battery_capacity_kwh"),
            "required_charge_kwh": 0.0,
            "pv_offset_kwh": 0.0,
            "net_grid_charge_kwh": 0.0,
            "configured_charge_windows": int(config.get(f"charging_windows{suffix}", 0)),
            "pv_adjusted_charge_windows": int(config.get(f"charging_windows{suffix}", 0)),
            "winter_reserve_active": False,
            "pv_fallback_reason": "disabled",
        }

        now = dt_util.now()
        current_time = now.replace(second=0, microsecond=0)
        current_price = self._get_current_price(prices, current_time)

        # Calculate averages
        cheap_prices = [w["price"] for w in charge_windows]
        expensive_prices = [w["price"] for w in discharge_windows]

        avg_cheap = float(np.mean(cheap_prices)) if cheap_prices else 0.0
        avg_expensive = float(np.mean(expensive_prices)) if expensive_prices else 0.0

        # Calculate spreads
        spread_pct = 0.0
        if avg_cheap > 0 and avg_expensive > 0:
            spread_pct = float(((avg_expensive - avg_cheap) / avg_cheap) * 100)

        # Calculate actual windows considering time and price overrides
        actual_charge, actual_discharge = self._calculate_actual_windows(
            prices,
            charge_windows,
            discharge_windows,
            aggressive_windows,
            config,
            is_tomorrow
        )

        # Count completed windows (use actual windows to include price/time overrides)
        completed_charge = sum(
            1 for w in actual_charge
            if w["timestamp"] + timedelta(minutes=w["duration"]) <= current_time
        )
        completed_discharge = sum(
            1 for w in actual_discharge
            if w["timestamp"] + timedelta(minutes=w["duration"]) <= current_time
        )

        # Calculate costs with base usage strategies
        charge_power = config.get("charge_power", 2400) / 1000  # Convert to kW
        discharge_power = config.get("discharge_power", 2400) / 1000
        base_usage = config.get("base_usage", 0) / 1000

        # Get strategies
        charge_strategy = config.get("base_usage_charge_strategy", "grid_covers_both")
        idle_strategy = config.get("base_usage_idle_strategy", "grid_covers")
        discharge_strategy = config.get("base_usage_discharge_strategy", "subtract_base")
        aggressive_strategy = config.get("base_usage_aggressive_strategy", "same_as_discharge")

        # Initialize tracking variables
        completed_charge_cost = 0
        completed_discharge_revenue = 0
        completed_base_usage_cost = 0  # Grid cost for base usage
        completed_base_usage_battery = 0  # Battery kWh used for base usage

        # CHARGE windows: Apply charge strategy
        for w in actual_charge:
            if w["timestamp"] + timedelta(minutes=w["duration"]) <= current_time:
                duration_hours = w["duration"] / 60
                if charge_strategy == "grid_covers_both":
                    # Grid provides charge power + base usage
                    completed_charge_cost += w["price"] * duration_hours * (charge_power + base_usage)
                else:  # battery_covers_base
                    # Grid provides charge power only, battery covers base
                    completed_charge_cost += w["price"] * duration_hours * charge_power
                    completed_base_usage_battery += duration_hours * base_usage

        # DISCHARGE/AGGRESSIVE windows: Apply discharge/aggressive strategies
        # Separate by state for strategy application
        for w in actual_discharge:
            if w["timestamp"] + timedelta(minutes=w["duration"]) <= current_time:
                duration_hours = w["duration"] / 60

                # Determine which strategy to use based on window state
                if w.get("state") == STATE_DISCHARGE_AGGRESSIVE:
                    # Aggressive discharge window
                    if aggressive_strategy == "same_as_discharge":
                        strategy = discharge_strategy
                    else:
                        strategy = aggressive_strategy
                else:
                    # Regular discharge window
                    strategy = discharge_strategy

                if strategy == "already_included":
                    # Full discharge power generates revenue
                    completed_discharge_revenue += w["price"] * duration_hours * discharge_power
                else:  # subtract_base (NoM)
                    # Battery covers base first, exports the rest
                    net_export = max(0, discharge_power - base_usage)
                    completed_discharge_revenue += w["price"] * duration_hours * net_export
                    completed_base_usage_battery += duration_hours * base_usage

        # IDLE periods: Apply idle strategy
        # Build sets of timestamps for active windows
        charge_timestamps = {w["timestamp"] for w in actual_charge}
        discharge_timestamps = {w["timestamp"] for w in actual_discharge}

        for price_data in prices:
            timestamp = price_data["timestamp"]
            if timestamp + timedelta(minutes=price_data["duration"]) <= current_time:
                # Check if this period is idle (not in any active window)
                is_active = timestamp in charge_timestamps or timestamp in discharge_timestamps

                if not is_active:
                    duration_hours = price_data["duration"] / 60
                    if idle_strategy == "grid_covers":
                        # Grid provides base usage, add to cost
                        completed_base_usage_cost += price_data["price"] * duration_hours * base_usage
                    else:  # battery_covers (NoM)
                        # Battery provides base usage, track battery consumption
                        completed_base_usage_battery += duration_hours * base_usage

        # Calculate planned total cost for ALL windows (for tomorrow's estimate)
        # Unlike total_cost which only counts completed windows, this estimates the full day
        planned_charge_cost = 0
        planned_discharge_revenue = 0
        planned_base_usage_cost = 0

        # All charge windows (not just completed)
        for w in actual_charge:
            duration_hours = w["duration"] / 60
            if charge_strategy == "grid_covers_both":
                planned_charge_cost += w["price"] * duration_hours * (charge_power + base_usage)
            else:  # battery_covers_base
                planned_charge_cost += w["price"] * duration_hours * charge_power

        # All discharge windows (not just completed)
        for w in actual_discharge:
            duration_hours = w["duration"] / 60

            # Determine strategy based on state
            if w.get("state") == STATE_DISCHARGE_AGGRESSIVE:
                strategy = aggressive_strategy if aggressive_strategy != "same_as_discharge" else discharge_strategy
            else:
                strategy = discharge_strategy

            if strategy == "already_included":
                planned_discharge_revenue += w["price"] * duration_hours * discharge_power
            else:  # subtract_base
                net_export = max(0, discharge_power - base_usage)
                planned_discharge_revenue += w["price"] * duration_hours * net_export

        # All idle periods
        for price_data in prices:
            timestamp = price_data["timestamp"]
            is_active = timestamp in charge_timestamps or timestamp in discharge_timestamps

            if not is_active:
                duration_hours = price_data["duration"] / 60
                if idle_strategy == "grid_covers":
                    planned_base_usage_cost += price_data["price"] * duration_hours * base_usage

        planned_total_cost = round(planned_charge_cost + planned_base_usage_cost - planned_discharge_revenue, 3)

        # Dashboard compatibility metrics
        window_duration_hours = (prices[0]["duration"] / 60) if prices else 0.25
        charge_power_kw = config.get("charge_power", 2400) / 1000
        discharge_power_kw = config.get("discharge_power", 2400) / 1000
        base_usage_kw = config.get("base_usage", 0) / 1000
        effective_charge_power_kw = max(
            0.0,
            charge_power_kw - base_usage_kw if charge_strategy == "battery_covers_base" else charge_power_kw,
        )

        net_planned_charge_kwh = sum(
            (w["duration"] / 60) * effective_charge_power_kw for w in actual_charge
        )

        net_planned_discharge_kwh = 0.0
        for w in actual_discharge:
            if w.get("state") == STATE_DISCHARGE_AGGRESSIVE:
                strategy = aggressive_strategy if aggressive_strategy != "same_as_discharge" else discharge_strategy
            else:
                strategy = discharge_strategy
            effective_discharge_power_kw = max(
                0.0,
                discharge_power_kw - base_usage_kw if strategy == "subtract_base" else discharge_power_kw,
            )
            net_planned_discharge_kwh += (w["duration"] / 60) * effective_discharge_power_kw

        # Percentile compatibility averages
        percentile_cheap_avg = 0.0
        percentile_expensive_avg = 0.0
        if prices:
            cheap_percentile = float(config.get(f"cheap_percentile{suffix}", 25))
            expensive_percentile = float(config.get(f"expensive_percentile{suffix}", 25))
            price_array = np.array([p["price"] for p in prices])
            cheap_threshold = np.percentile(price_array, cheap_percentile)
            expensive_threshold = np.percentile(price_array, 100 - expensive_percentile)
            cheap_values = price_array[price_array <= cheap_threshold]
            expensive_values = price_array[price_array >= expensive_threshold]
            percentile_cheap_avg = float(np.mean(cheap_values)) if cheap_values.size > 0 else 0.0
            percentile_expensive_avg = float(np.mean(expensive_values)) if expensive_values.size > 0 else 0.0

        # Group contiguous windows for markdown rendering
        grouped_charge_windows = self._group_windows(
            actual_charge,
            percentile_expensive_avg,
            mode="charge",
        )
        grouped_discharge_windows = self._group_windows(
            actual_discharge,
            percentile_cheap_avg if percentile_cheap_avg > 0 else avg_cheap,
            mode="discharge",
        )

        # Build result
        result = {
            "state": current_state,
            "cheapest_times": [w["timestamp"].isoformat() for w in charge_windows],
            "cheapest_prices": [float(w["price"]) for w in charge_windows],
            "expensive_times": [w["timestamp"].isoformat() for w in discharge_windows],
            "expensive_prices": [float(w["price"]) for w in discharge_windows],
            "expensive_times_aggressive": [w["timestamp"].isoformat() for w in aggressive_windows],
            "expensive_prices_aggressive": [float(w["price"]) for w in aggressive_windows],
            "actual_charge_times": [w["timestamp"].isoformat() for w in actual_charge],
            "actual_charge_prices": [float(w["price"]) for w in actual_charge],
            "actual_discharge_times": [w["timestamp"].isoformat() for w in actual_discharge],
            "actual_discharge_prices": [float(w["price"]) for w in actual_discharge],
            "actual_discharge_sell_prices": [float(w["price"]) for w in actual_discharge],
            "completed_charge_windows": completed_charge,
            "completed_discharge_windows": completed_discharge,
            "completed_charge_cost": round(completed_charge_cost, 3),
            "completed_discharge_revenue": round(completed_discharge_revenue, 3),
            "completed_base_usage_cost": round(completed_base_usage_cost, 3),
            "completed_base_usage_battery": round(completed_base_usage_battery, 3),
            "total_cost": round(completed_charge_cost + completed_base_usage_cost - completed_discharge_revenue, 3),
            "planned_total_cost": planned_total_cost,
            "num_windows": len(charge_windows),
            "min_spread_required": config.get(f"min_spread{suffix}", 10),
            "spread_percentage": round(spread_pct, 1),
            "spread_met": bool(spread_pct >= config.get(f"min_spread{suffix}", 10)),
            "spread_avg": round(spread_pct, 1),
            "arbitrage_avg": round(spread_pct, 1),
            "actual_spread_avg": round(spread_pct, 1),
            "discharge_spread_met": bool(spread_pct >= config.get(f"min_spread_discharge{suffix}", 20)),
            "aggressive_discharge_spread_met": bool(spread_pct >= config.get(f"aggressive_discharge_spread{suffix}", 40)),
            "avg_cheap_price": round(avg_cheap, 5),
            "avg_expensive_price": round(avg_expensive, 5),
            "percentile_cheap_avg": round(percentile_cheap_avg, 5),
            "percentile_expensive_avg": round(percentile_expensive_avg, 5),
            "current_price": round(current_price, 5) if current_price else 0,
            "price_override_active": config.get(f"price_override_enabled{suffix}", False) and
                                    current_price and
                                    current_price <= config.get(f"price_override_threshold{suffix}", 0.15),
            "time_override_active": config.get(f"time_override_enabled{suffix}", False),
            "automation_enabled": config.get("automation_enabled", True),
            "calculation_window_enabled": config.get(f"calculation_window_enabled{suffix}", False),
            "net_planned_charge_kwh": round(net_planned_charge_kwh, 3),
            "net_planned_discharge_kwh": round(net_planned_discharge_kwh, 3),
            "grouped_charge_windows": grouped_charge_windows,
            "grouped_discharge_windows": grouped_discharge_windows,
            "window_duration_hours": round(window_duration_hours, 4),
            "charge_power_kw": round(charge_power_kw, 3),
            "discharge_power_kw": round(discharge_power_kw, 3),
            "base_usage_kw": round(base_usage_kw, 3),
            "pv_adjustment_active": pv_adjustment.get("pv_adjustment_active", False),
            "pv_forecast_kwh_used": pv_adjustment.get("pv_forecast_kwh_used", 0.0),
            "soc_target_sunrise": pv_adjustment.get("soc_target_sunrise", 0.0),
            "current_soc": pv_adjustment.get("current_soc"),
            "battery_capacity_kwh": pv_adjustment.get("battery_capacity_kwh"),
            "required_charge_kwh": pv_adjustment.get("required_charge_kwh", 0.0),
            "pv_offset_kwh": pv_adjustment.get("pv_offset_kwh", 0.0),
            "net_grid_charge_kwh": pv_adjustment.get("net_grid_charge_kwh", 0.0),
            "configured_charge_windows": pv_adjustment.get("configured_charge_windows", 0),
            "pv_adjusted_charge_windows": pv_adjustment.get("pv_adjusted_charge_windows", 0),
            "winter_reserve_active": pv_adjustment.get("winter_reserve_active", False),
            "pv_fallback_reason": pv_adjustment.get("pv_fallback_reason", ""),
        }

        return result

    def _empty_result(self, is_tomorrow: bool) -> Dict[str, Any]:
        """Return an empty result structure."""
        return {
            "state": STATE_OFF,
            "cheapest_times": [],
            "cheapest_prices": [],
            "expensive_times": [],
            "expensive_prices": [],
            "expensive_times_aggressive": [],
            "expensive_prices_aggressive": [],
            "actual_charge_times": [],
            "actual_charge_prices": [],
            "actual_discharge_times": [],
            "actual_discharge_prices": [],
            "actual_discharge_sell_prices": [],
            "completed_charge_windows": 0,
            "completed_discharge_windows": 0,
            "completed_charge_cost": 0,
            "completed_discharge_revenue": 0,
            "completed_base_usage_cost": 0,
            "completed_base_usage_battery": 0,
            "total_cost": 0,
            "planned_total_cost": 0,
            "num_windows": 0,
            "min_spread_required": 0,
            "spread_percentage": 0,
            "spread_met": False,
            "spread_avg": 0,
            "arbitrage_avg": 0,
            "actual_spread_avg": 0,
            "discharge_spread_met": False,
            "aggressive_discharge_spread_met": False,
            "avg_cheap_price": 0,
            "avg_expensive_price": 0,
            "percentile_cheap_avg": 0,
            "percentile_expensive_avg": 0,
            "current_price": 0,
            "price_override_active": False,
            "time_override_active": False,
            "automation_enabled": False,
            "calculation_window_enabled": False,
            "net_planned_charge_kwh": 0,
            "net_planned_discharge_kwh": 0,
            "grouped_charge_windows": [],
            "grouped_discharge_windows": [],
            "window_duration_hours": 0,
            "charge_power_kw": 0,
            "discharge_power_kw": 0,
            "base_usage_kw": 0,
            "pv_adjustment_active": False,
            "pv_forecast_kwh_used": 0,
            "soc_target_sunrise": 0,
            "current_soc": None,
            "battery_capacity_kwh": None,
            "required_charge_kwh": 0,
            "pv_offset_kwh": 0,
            "net_grid_charge_kwh": 0,
            "configured_charge_windows": 0,
            "pv_adjusted_charge_windows": 0,
            "winter_reserve_active": False,
            "pv_fallback_reason": "disabled",
        }
