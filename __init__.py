"""Spacecraft Launches plugin for FiestaBoard.

Displays upcoming spacecraft launch countdowns and statuses using
the Launch Library 2 API from The Space Devs.
"""

from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
import logging
import requests

from src.devices import BoardContext
from src.plugins.base import PluginBase, PluginResult
from src.text_to_board import count_tiles

logger = logging.getLogger(__name__)

# Launch Library 2 API
LL2_BASE_URL = "https://ll.thespacedevs.com/2.3.0"
LL2_LAUNCHES_URL = f"{LL2_BASE_URL}/launches/upcoming/"

# self.board is None outside a board-scoped render (legacy callers, unit
# tests). Treat that as "assume a Flagship" rather than crashing -- never a
# bare 22/6 literal on the rendering path itself, just this one fallback.
DEFAULT_BOARD = BoardContext.from_device_type("flagship")


def _clip_to_width(text: str, width: int) -> str:
    """Clip *text* to at most *width* tiles (not characters).

    This plugin's board content never contains colour markers, so tile
    count equals character count today -- but measuring with
    ``count_tiles`` keeps the guarantee correct if that ever changes,
    rather than silently overstating width via ``len()``.
    """
    if count_tiles(text) <= width:
        return text
    return text[:width]


def _header_row_count(board: BoardContext) -> int:
    """How many rows the title/column-header block takes on *board*.

    A 3-row board (a Note, or a 1x1 note array) can only spare one row for
    a header before there is nothing left to show; anything taller can
    afford both a title row and a column-header row, matching the
    Flagship's historical two-line header.
    """
    return 1 if board.rows <= 3 else 2


def _launch_capacity(board: BoardContext) -> int:
    """How many launch rows fit on *board* once the header is accounted for."""
    return max(0, board.rows - _header_row_count(board))


def _title_for_width(cols: int) -> str:
    """Board title, abbreviated to fit narrower boards instead of clipping mid-word."""
    if cols >= 16:
        text = "EARTH DEPARTURES"
    elif cols >= 10:
        text = "DEPARTURES"
    else:
        text = "LAUNCHES"
    return _clip_to_width(text, cols).center(cols)


def _column_header_for_width(cols: int) -> str:
    """Column header, abbreviated to fit narrower boards."""
    if cols >= 18:
        text = "DATE TIME MISSION"
    elif cols >= 12:
        text = "DATE MISSION"
    else:
        text = "MISSION"
    return _clip_to_width(text, cols)


class SpacecraftLaunchesPlugin(PluginBase):
    """Spacecraft launches tracker plugin.

    Fetches upcoming launch data from the Launch Library 2 API and displays
    launch name, status, countdown, pad, and provider information.
    """

    def __init__(self, manifest: Dict[str, Any]):
        """Initialize the spacecraft launches plugin."""
        super().__init__(manifest)
        # Resilience cache: last-known-good fetch, served back on a rate
        # limit or outage so a blip doesn't blank the board. Keyed by board
        # geometry (mirrors PluginBase._cache_key) -- a single unkeyed cache
        # would serve one board's frame to a different-shaped board.
        self._cache: Dict[str, Dict[str, Any]] = {}

    @property
    def plugin_id(self) -> str:
        return "spacecraft_launches"

    @staticmethod
    def _geometry_key(board: BoardContext) -> str:
        """Cache key for *board*'s shape and display.

        Mirrors ``PluginBase._cache_key``. Flagship and Note have fixed sizes,
        so their device_type is a sufficient key. Every other family varies in
        size under one device_type -- note arrays, and LED/TV boards, which
        are all "panel" -- so the dimensions are folded in: otherwise a 16x10
        Pixoo and a 22x9 TV panel share one entry, and a board whose grid
        changes at runtime (a larger text size) is served output laid out for
        its old size. Two boards of one size can still draw differently
        (split-flap vs LED), so the display's key is appended when core
        provides one; ``getattr`` keeps this working on cores whose
        BoardContext has no ``display``.
        """
        if board.device_type in ("flagship", "note"):
            key = board.device_type
        else:
            key = f"{board.device_type}:{board.cols}x{board.rows}"
        display = getattr(board, "display", None)
        display_key = getattr(display, "key", None)
        return f"{key}|{display_key}" if display_key else key

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        """Validate spacecraft launches configuration."""
        errors = []

        max_launches = config.get("max_launches", 4)
        if not isinstance(max_launches, int) or not (1 <= max_launches <= 24):
            errors.append("Max launches must be between 1 and 24")

        return errors

    def on_config_change(self, old_config: Dict[str, Any], new_config: Dict[str, Any]) -> None:
        """Drop the cached launches so a config change takes effect immediately.

        The cache is keyed only on age (per geometry), so without this a
        change to `max_launches` would keep serving the old-sized list for
        up to refresh_seconds on every board.
        """
        self._cache = {}
        logger.debug("Cleared cached launches after config change")

    @staticmethod
    def _compute_countdown(net_str: str) -> str:
        """Compute countdown string from a NET datetime string.

        Args:
            net_str: ISO 8601 datetime string for the launch NET.

        Returns:
            Human-readable countdown string (e.g., "2d 05:30:00").
        """
        try:
            net_dt = datetime.fromisoformat(net_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = net_dt - now

            if delta.total_seconds() <= 0:
                return "LAUNCHED"

            total_seconds = int(delta.total_seconds())
            days = total_seconds // 86400
            hours = (total_seconds % 86400) // 3600
            minutes = (total_seconds % 3600) // 60
            seconds = total_seconds % 60

            if days > 0:
                return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
            else:
                return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        except (ValueError, TypeError):
            return "TBD"

    def _cap(self, field: str, value: str) -> str:
        """Clip *value* to the manifest's declared ``launches.*.<field>`` bound.

        The Launch Library API returns free-text (names, pad and location
        strings) with no length guarantee of its own. The page editor sizes
        templates from ``max_lengths``, so a value that runs longer than its
        declared bound makes the editor's fit warnings wrong -- this is what
        keeps that declaration honest regardless of what the API hands back.
        """
        limit = (self._manifest.get("max_lengths") or {}).get(f"launches.*.{field}")
        if isinstance(limit, int) and limit > 0 and count_tiles(value) > limit:
            return value[:limit]
        return value

    def _parse_launch(self, launch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse a single launch object from the API response.

        Args:
            launch: Launch dictionary from the API.

        Returns:
            Parsed launch data dictionary, or None if invalid.
        """
        try:
            name = launch.get("name", "Unknown")

            # Status
            status_obj = launch.get("status") or {}
            status_name = status_obj.get("name", "Unknown")
            status_abbrev = status_obj.get("abbrev", "UNK")

            # NET (No Earlier Than) datetime
            net_str = launch.get("net", "")
            net_date = ""
            net_time = ""
            if net_str:
                try:
                    net_dt = datetime.fromisoformat(net_str.replace("Z", "+00:00"))
                    net_date = net_dt.strftime("%m/%d")
                    net_time = net_dt.strftime("%H:%M")
                except (ValueError, TypeError):
                    pass

            # Countdown
            countdown = self._compute_countdown(net_str) if net_str else "TBD"

            # Pad
            pad_obj = launch.get("pad") or {}
            pad_name = pad_obj.get("name", "")
            pad_location_obj = pad_obj.get("location") or {}
            pad_location = pad_location_obj.get("name", "")

            # Provider
            provider_obj = launch.get("launch_service_provider") or {}
            provider = provider_obj.get("name", "Unknown")

            # Rocket
            rocket_obj = launch.get("rocket") or {}
            rocket_config = rocket_obj.get("configuration") or {}
            rocket = rocket_config.get("name", "")
            if not rocket:
                # Fallback: extract rocket name from launch name (before " | ")
                if " | " in name:
                    rocket = name.split(" | ")[0].strip()

            # Mission
            mission_obj = launch.get("mission") or {}
            mission = mission_obj.get("name", "")
            if not mission:
                # Fallback: extract mission from launch name (after " | ")
                if " | " in name:
                    mission = name.split(" | ", 1)[1].strip()
            mission = mission or name

            # Raw display line: intentionally NOT sized to any board here --
            # this is per-item template data, not a rendered board row, so it
            # must not bake in an assumed target width. Board-shaped output
            # is built separately in `_render_lines`, from the actual board.
            formatted = self._format_launch_line(net_date, net_time, mission, pad_name)

            return {
                "name": self._cap("name", name),
                "status": self._cap("status", status_name),
                "status_abbrev": self._cap("status_abbrev", status_abbrev),
                "net": self._cap("net", net_str if net_str else ""),
                "net_date": net_date,
                "net_time": net_time,
                "countdown": self._cap("countdown", countdown),
                "pad": self._cap("pad", pad_name),
                "pad_location": self._cap("pad_location", pad_location),
                "provider": self._cap("provider", provider),
                "rocket": self._cap("rocket", rocket),
                "mission": self._cap("mission", mission),
                "formatted": self._cap("formatted", formatted),
            }

        except (ValueError, TypeError, KeyError) as e:
            logger.debug(f"Error parsing launch: {e}")
            return None

    @staticmethod
    def _format_launch_line(date: str, time: str, mission: str, pad: str) -> str:
        """Format launch data for display line.

        Format: MM/DD HH:MM MISSION
        Example: 03/15 14:30 CREW-12

        Deliberately unbounded: this is raw per-item data exposed as a
        template variable (``launches.*.formatted``), not a board row, so it
        must not assume any particular target board width. Callers that
        place this on a specific board size it themselves; `_parse_launch`
        separately caps it to the manifest's own declared bound.

        Args:
            date: Date string (MM/DD).
            time: Time string (HH:MM).
            mission: Mission name.
            pad: Pad name (unused; kept for call-site compatibility).

        Returns:
            Formatted string, e.g. "03/15 14:30 CREW-12".
        """
        prefix = f"{date} {time} " if date and time else ""
        return f"{prefix}{mission}"

    def _launch_line(self, launch: Dict[str, Any], cols: int) -> str:
        """Render one launch as a single board row exactly *cols* wide.

        Reflows rather than truncates blindly: on a wide board the mission
        gets more room; on a narrow one the date/time prefix is abbreviated
        away first so the mission itself still gets some space.
        """
        date = launch.get("net_date", "")
        time = launch.get("net_time", "")
        mission = launch.get("mission") or launch.get("name") or ""

        if cols >= 20 and date and time:
            prefix = f"{date} {time} "
        elif cols >= 12 and date:
            prefix = f"{date} "
        else:
            prefix = ""

        available = max(0, cols - len(prefix))
        line = f"{prefix}{mission[:available]}" if available > 0 else ""
        return _clip_to_width(line, cols)

    def _render_lines(self, launches: List[Dict[str, Any]], board: BoardContext) -> List[str]:
        """Render *launches* to fit *board* exactly.

        Every dimension comes from *board* -- never a literal 22 or 6. Never
        more than ``board.rows`` rows, never a row wider than ``board.cols``.
        A taller board gets more launch rows (reflow), not a blank panel.
        """
        rows, cols = board.rows, board.cols
        if rows <= 0 or cols <= 0:
            return []

        header_count = _header_row_count(board)
        capacity = _launch_capacity(board)

        lines: List[str] = [_title_for_width(cols)]
        if header_count == 2:
            lines.append(_column_header_for_width(cols))

        if not launches:
            lines.append(_clip_to_width("NO UPCOMING LAUNCHES", cols))
        else:
            for launch in launches[:capacity]:
                lines.append(self._launch_line(launch, cols))

        while len(lines) < rows:
            lines.append("")
        return lines[:rows]

    def _empty_result_data(self) -> Dict[str, Any]:
        """Template-variable payload for "no upcoming launches"."""
        return {
            "launch_count": 0,
            "launches": [],
            "name": "",
            "status": "No launches",
            "net": "",
            "countdown": "",
            "pad": "",
            "provider": "",
            "rocket": "",
            "mission": "",
            "formatted": "NO UPCOMING LAUNCHES",
            "headers": "DATE TIME MISSION",
            "last_updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def fetch_data(self) -> PluginResult:
        """Fetch upcoming spacecraft launch data from Launch Library 2 API."""
        # self.board is set by the render pipeline around this call; None
        # outside a board-scoped render (unit tests, legacy callers).
        board = self.board or DEFAULT_BOARD
        geo_key = self._geometry_key(board)

        max_launches = self.config.get("max_launches", 4)
        refresh_seconds = self.config.get("refresh_seconds", 300)

        cached = self._cache.get(geo_key)

        # Check cache first
        if cached and cached.get("launches"):
            last_updated = cached.get("last_updated", "")
            if last_updated:
                try:
                    cache_time = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
                    age_seconds = (datetime.now(timezone.utc) - cache_time).total_seconds()
                    if age_seconds < refresh_seconds:
                        logger.debug(f"Using cached data for {geo_key} (age: {age_seconds:.0f}s < {refresh_seconds}s)")
                        return PluginResult(
                            available=True,
                            data=cached,
                            formatted_lines=self._render_lines(cached.get("launches", []), board),
                        )
                except Exception:
                    pass

        try:
            params = {
                "limit": max_launches,
                "mode": "detailed",
            }

            response = requests.get(LL2_LAUNCHES_URL, params=params, timeout=15)

            # Handle rate limiting
            if response.status_code == 429:
                logger.warning("Launch Library 2 API rate limit exceeded, using cached data if available")
                if cached and cached.get("launches"):
                    return PluginResult(
                        available=True,
                        data=cached,
                        formatted_lines=self._render_lines(cached.get("launches", []), board),
                    )
                return PluginResult(
                    available=False,
                    error="API rate limit exceeded (15 req/hr). Please wait."
                )

            if response.status_code != 200:
                logger.error(f"Launch Library 2 API error: {response.status_code}")
                if cached and cached.get("launches"):
                    return PluginResult(
                        available=True,
                        data=cached,
                        formatted_lines=self._render_lines(cached.get("launches", []), board),
                    )
                return PluginResult(
                    available=False,
                    error=f"API error: {response.status_code}"
                )

            data = response.json()
            results = data.get("results", [])

            if not results:
                return PluginResult(
                    available=True,
                    data=self._empty_result_data(),
                    formatted_lines=self._render_lines([], board),
                )

            # Parse launches
            launches = []
            for launch_data in results:
                parsed = self._parse_launch(launch_data)
                if parsed:
                    launches.append(parsed)

            launches = launches[:max_launches]

            if not launches:
                return PluginResult(
                    available=True,
                    data=self._empty_result_data(),
                    formatted_lines=self._render_lines([], board),
                )

            # Primary launch (next upcoming)
            primary = launches[0]

            result_data = {
                # Primary launch fields
                "name": primary["name"],
                "status": primary["status"],
                "net": primary["net"],
                "countdown": primary["countdown"],
                "pad": primary["pad"],
                "provider": primary["provider"],
                "rocket": primary["rocket"],
                "mission": primary["mission"],
                "formatted": primary["formatted"],
                # Headers
                "headers": "DATE TIME MISSION",
                # Aggregate
                "launch_count": len(launches),
                "last_updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                # Array of all launches
                "launches": launches,
            }

            self._cache[geo_key] = result_data
            return PluginResult(
                available=True,
                data=result_data,
                formatted_lines=self._render_lines(launches, board),
            )

        except requests.exceptions.RequestException as e:
            logger.exception("Error fetching launch data")
            if cached and cached.get("launches"):
                return PluginResult(
                    available=True,
                    data=cached,
                    formatted_lines=self._render_lines(cached.get("launches", []), board),
                )
            return PluginResult(available=False, error=f"Network error: {str(e)}")
        except Exception as e:
            logger.exception("Unexpected error fetching launch data")
            if cached and cached.get("launches"):
                return PluginResult(
                    available=True,
                    data=cached,
                    formatted_lines=self._render_lines(cached.get("launches", []), board),
                )
            return PluginResult(available=False, error=str(e))

    def get_formatted_display(self) -> Optional[List[str]]:
        """Return a formatted launch display sized to `self.board`.

        `self.board` is bound by the conformance suite (and, were core ever
        to call this hook, by the render pipeline) for the duration of the
        call; it defaults to a Flagship when unbound, matching `fetch_data`.
        """
        board = self.board or DEFAULT_BOARD
        result = self.fetch_data()
        if not result.available or not result.data:
            return None

        launches = result.data.get("launches", [])
        return self._render_lines(launches, board)

    def cleanup(self) -> None:
        """Cleanup when plugin is disabled."""
        self._cache = {}


# Export the plugin class
Plugin = SpacecraftLaunchesPlugin
