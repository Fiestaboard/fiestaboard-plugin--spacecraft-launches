"""Unit tests for Spacecraft Launches plugin."""

import json
import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone, timedelta

from src.devices import BoardContext

from plugins.spacecraft_launches import SpacecraftLaunchesPlugin


@pytest.fixture
def plugin(sample_manifest):
    """Create plugin instance for testing."""
    return SpacecraftLaunchesPlugin(sample_manifest)


class TestPluginInitialization:
    """Test plugin initialization."""

    def test_plugin_id(self, plugin):
        """Test plugin ID."""
        assert plugin.plugin_id == "spacecraft_launches"

    def test_plugin_initialization(self, plugin):
        """Test plugin initializes correctly."""
        assert plugin._cache == {}


class TestConfigurationValidation:
    """Test configuration validation."""

    def test_validate_config_valid(self, plugin, sample_config):
        """Test validation with valid configuration."""
        errors = plugin.validate_config(sample_config)
        assert errors == []

    def test_validate_config_defaults(self, plugin):
        """Test validation with empty config uses defaults."""
        errors = plugin.validate_config({})
        assert errors == []

    def test_validate_config_invalid_max_launches_too_high(self, plugin):
        """Test validation fails with max_launches > 24."""
        config = {"max_launches": 25}
        errors = plugin.validate_config(config)
        assert any("Max launches" in e for e in errors)

    def test_validate_config_max_launches_raised_ceiling(self, plugin):
        """24 is now valid: the ceiling was raised so a Note Array panel can
        be configured to show as many launches as it has rows for."""
        config = {"max_launches": 24}
        errors = plugin.validate_config(config)
        assert errors == []

    def test_validate_config_invalid_max_launches_too_low(self, plugin):
        """Test validation fails with max_launches < 1."""
        config = {"max_launches": 0}
        errors = plugin.validate_config(config)
        assert any("Max launches" in e for e in errors)

    def test_validate_refresh_too_low(self, plugin):
        """Test base validation fails with refresh < 240 (manifest minimum)."""
        config = {"refresh_seconds": 100}
        errors = plugin._validate_refresh_seconds(config)
        assert any("at least 240 seconds" in e for e in errors)

    def test_validate_config_invalid_max_launches_type(self, plugin):
        """Test validation fails with non-integer max_launches."""
        config = {"max_launches": "four"}
        errors = plugin.validate_config(config)
        assert any("Max launches" in e for e in errors)

    def test_validate_refresh_non_numeric(self, plugin):
        """Test base validation fails with non-numeric refresh_seconds."""
        config = {"refresh_seconds": "fast"}
        errors = plugin._validate_refresh_seconds(config)
        assert any("must be a number" in e for e in errors)


class TestCountdown:
    """Test countdown computation."""

    def test_countdown_future_launch(self, plugin):
        """Test countdown for a future launch."""
        future = datetime.now(timezone.utc) + timedelta(days=2, hours=5, minutes=30, seconds=15)
        net_str = future.isoformat()
        countdown = plugin._compute_countdown(net_str)
        assert countdown.startswith("2d")

    def test_countdown_today_launch(self, plugin):
        """Test countdown for a launch today (no days)."""
        future = datetime.now(timezone.utc) + timedelta(hours=3, minutes=15, seconds=45)
        net_str = future.isoformat()
        countdown = plugin._compute_countdown(net_str)
        assert "d" not in countdown
        assert countdown.startswith("03:")

    def test_countdown_past_launch(self, plugin):
        """Test countdown for a past launch."""
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        net_str = past.isoformat()
        countdown = plugin._compute_countdown(net_str)
        assert countdown == "LAUNCHED"

    def test_countdown_invalid_date(self, plugin):
        """Test countdown with invalid date string."""
        countdown = plugin._compute_countdown("not-a-date")
        assert countdown == "TBD"

    def test_countdown_empty_string(self, plugin):
        """Test countdown with empty string."""
        countdown = plugin._compute_countdown("")
        assert countdown == "TBD"

    def test_countdown_z_suffix(self, plugin):
        """Test countdown handles Z-terminated dates."""
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        net_str = future.strftime("%Y-%m-%dT%H:%M:%SZ")
        countdown = plugin._compute_countdown(net_str)
        assert countdown != "TBD"


class TestParseLaunch:
    """Test launch parsing."""

    def test_parse_launch_valid(self, plugin, mock_launches_response):
        """Test parsing a valid launch."""
        launch = mock_launches_response["results"][0]
        parsed = plugin._parse_launch(launch)

        assert parsed is not None
        assert "Crew-12" in parsed["mission"]
        assert parsed["status"] == "Go for Launch"
        assert parsed["status_abbrev"] == "Go"
        assert parsed["provider"] == "SpaceX"
        assert parsed["rocket"] == "Falcon 9"
        assert parsed["net_date"] == "03/15"
        assert parsed["net_time"] == "14:30"
        assert parsed["pad"] == "Space Launch Complex 40"

    def test_parse_launch_no_mission(self, plugin, mock_launch_no_mission):
        """Test parsing a launch with no mission data."""
        launch = mock_launch_no_mission["results"][0]
        parsed = plugin._parse_launch(launch)

        assert parsed is not None
        assert parsed["mission"] == "Mystery Payload"
        assert parsed["rocket"] == "Unknown Vehicle"

    def test_parse_launch_null_fields(self, plugin):
        """Test parsing a launch with null optional fields."""
        launch = {
            "name": "Test Launch",
            "status": None,
            "net": "",
            "pad": None,
            "launch_service_provider": None,
            "rocket": None,
            "mission": None,
        }
        parsed = plugin._parse_launch(launch)
        assert parsed is not None
        assert parsed["status"] == "Unknown"
        assert parsed["provider"] == "Unknown"

    def test_parse_launch_all_fields_present(self, plugin, mock_launches_response):
        """Test that all expected fields are present in parsed data."""
        launch = mock_launches_response["results"][0]
        parsed = plugin._parse_launch(launch)

        expected_fields = [
            "name", "status", "status_abbrev", "net", "net_date",
            "net_time", "countdown", "pad", "pad_location", "provider",
            "rocket", "mission", "formatted"
        ]
        for field in expected_fields:
            assert field in parsed, f"Missing field: {field}"


class TestFormatting:
    """Test display formatting.

    `_format_launch_line` is raw per-item template data (`launches.*.formatted`),
    not a rendered board row -- it must NOT assume any particular target board
    width. Board-shaped output that actually fits a board is built separately
    by `_render_lines` from the board being rendered.
    """

    def test_format_launch_line(self, plugin):
        """Test launch line formatting."""
        formatted = plugin._format_launch_line("03/15", "14:30", "CREW-12", "SLC-40")
        assert formatted == "03/15 14:30 CREW-12"

    def test_format_launch_line_long_mission_is_not_truncated(self, plugin):
        """The raw field is unbounded -- it must not bake in a board width.

        (`_parse_launch` separately caps the value it stores to the
        manifest's own declared bound; that is covered by TestManifestHonesty.)
        """
        long_mission = "VERY LONG MISSION NAME HERE THAT EXCEEDS TWENTY TWO CHARACTERS"
        formatted = plugin._format_launch_line("03/15", "14:30", long_mission, "SLC-40")
        assert formatted == f"03/15 14:30 {long_mission}"

    def test_format_launch_line_no_date(self, plugin):
        """Test formatting with no date/time."""
        formatted = plugin._format_launch_line("", "", "CREW-12", "SLC-40")
        assert formatted == "CREW-12"


class TestFetchData:
    """Test data fetching."""

    @patch("plugins.spacecraft_launches.requests.get")
    def test_fetch_data_success(self, mock_get, plugin, sample_config, mock_launches_response):
        """Test successful data fetch."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_launches_response
        mock_get.return_value = mock_response

        plugin.config = sample_config
        result = plugin.fetch_data()

        assert result.available is True
        assert result.error is None
        assert result.data is not None
        assert result.data["launch_count"] == 3
        assert len(result.data["launches"]) == 3
        assert result.data["name"] is not None

    @patch("plugins.spacecraft_launches.requests.get")
    def test_fetch_data_empty_results(self, mock_get, plugin, sample_config, mock_empty_response):
        """Test fetch with empty results."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_empty_response
        mock_get.return_value = mock_response

        plugin.config = sample_config
        result = plugin.fetch_data()

        assert result.available is True
        assert result.data["launch_count"] == 0
        assert result.data["launches"] == []

    @patch("plugins.spacecraft_launches.requests.get")
    def test_fetch_data_rate_limit(self, mock_get, plugin, sample_config):
        """Test rate limit handling without cache."""
        mock_response = Mock()
        mock_response.status_code = 429
        mock_get.return_value = mock_response

        plugin.config = sample_config
        result = plugin.fetch_data()

        assert result.available is False
        assert "rate limit" in result.error.lower()

    @patch("plugins.spacecraft_launches.requests.get")
    def test_fetch_data_rate_limit_with_cache(self, mock_get, plugin, sample_config):
        """Test rate limit returns cached data."""
        mock_response = Mock()
        mock_response.status_code = 429
        mock_get.return_value = mock_response

        plugin.config = sample_config
        plugin._cache = {
            "flagship": {
                "launches": [{"name": "Cached Launch"}],
                "launch_count": 1,
            }
        }

        result = plugin.fetch_data()
        assert result.available is True

    @patch("plugins.spacecraft_launches.requests.get")
    def test_fetch_data_api_error(self, mock_get, plugin, sample_config):
        """Test API error handling."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_get.return_value = mock_response

        plugin.config = sample_config
        result = plugin.fetch_data()

        assert result.available is False
        assert "500" in result.error

    @patch("plugins.spacecraft_launches.requests.get")
    def test_fetch_data_api_error_with_cache(self, mock_get, plugin, sample_config):
        """Test API error returns cached data."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_get.return_value = mock_response

        plugin.config = sample_config
        plugin._cache = {
            "flagship": {
                "launches": [{"name": "Cached Launch"}],
                "launch_count": 1,
            }
        }

        result = plugin.fetch_data()
        assert result.available is True

    @patch("plugins.spacecraft_launches.requests.get")
    def test_fetch_data_network_error(self, mock_get, plugin, sample_config):
        """Test network error handling."""
        mock_get.side_effect = Exception("Connection refused")

        plugin.config = sample_config
        result = plugin.fetch_data()

        assert result.available is False
        assert result.error is not None

    @patch("plugins.spacecraft_launches.requests.get")
    def test_fetch_data_network_error_with_cache(self, mock_get, plugin, sample_config):
        """Test network error returns cached data."""
        mock_get.side_effect = Exception("Connection refused")

        plugin.config = sample_config
        plugin._cache = {
            "flagship": {
                "launches": [{"name": "Cached Launch"}],
                "launch_count": 1,
            }
        }

        result = plugin.fetch_data()
        assert result.available is True

    @patch("plugins.spacecraft_launches.requests.get")
    def test_fetch_data_uses_cache(self, mock_get, plugin, sample_config):
        """Test that fresh cache is used instead of API call."""
        plugin.config = sample_config
        plugin._cache = {
            "flagship": {
                "launches": [{"name": "Cached Launch"}],
                "launch_count": 1,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
        }

        result = plugin.fetch_data()
        assert result.available is True
        mock_get.assert_not_called()

    @patch("plugins.spacecraft_launches.requests.get")
    def test_config_change_invalidates_cache(self, mock_get, plugin, sample_config, mock_launches_response):
        """Test a config change drops the cache instead of serving the old list."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_launches_response
        mock_get.return_value = mock_response

        plugin.config = sample_config
        plugin._cache = {
            "flagship": {
                "launches": [{"name": "Cached Launch"}],
                "launch_count": 1,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
        }

        # Same cache window, fewer launches requested: the cached list is now wrong
        plugin.config = {**sample_config, "max_launches": 2}
        assert plugin._cache == {}

        result = plugin.fetch_data()
        assert result.available is True
        mock_get.assert_called_once()

    @patch("plugins.spacecraft_launches.requests.get")
    def test_fetch_data_respects_max_launches(self, mock_get, plugin, mock_launches_response):
        """Test max_launches limits results."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_launches_response
        mock_get.return_value = mock_response

        plugin.config = {"max_launches": 2, "refresh_seconds": 300}
        result = plugin.fetch_data()

        assert result.available is True
        assert len(result.data["launches"]) == 2

    @patch("plugins.spacecraft_launches.requests.get")
    def test_fetch_data_primary_launch_fields(self, mock_get, plugin, sample_config, mock_launches_response):
        """Test primary launch fields are set from first launch."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_launches_response
        mock_get.return_value = mock_response

        plugin.config = sample_config
        result = plugin.fetch_data()

        assert result.data["provider"] == "SpaceX"
        assert result.data["rocket"] == "Falcon 9"
        assert result.data["status"] == "Go for Launch"


class TestFormattedDisplay:
    """Test formatted display output."""

    @patch("plugins.spacecraft_launches.requests.get")
    def test_get_formatted_display(self, mock_get, plugin, sample_config, mock_launches_response):
        """Test formatted display output."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_launches_response
        mock_get.return_value = mock_response

        plugin.config = sample_config
        lines = plugin.get_formatted_display()

        assert lines is not None
        assert len(lines) == 6
        assert "EARTH DEPARTURES" in lines[0]

    def test_get_formatted_display_no_data(self, plugin, sample_config):
        """Test formatted display with no data returns None."""
        plugin.config = sample_config
        # Force fetch to fail by not mocking
        with patch("plugins.spacecraft_launches.requests.get") as mock_get:
            mock_get.side_effect = Exception("No connection")
            lines = plugin.get_formatted_display()
            assert lines is None


class TestBoardAdaptivity:
    """Layout must be derived from `self.board`, never a hardcoded 22x6."""

    @patch("plugins.spacecraft_launches.requests.get")
    def test_get_formatted_display_defaults_to_flagship_when_unbound(
        self, mock_get, plugin, sample_config, mock_launches_response
    ):
        """self.board is None outside a board-scoped render; must default to
        a Flagship rather than crash (matches fetch_data's own fallback)."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_launches_response
        mock_get.return_value = mock_response

        plugin.config = sample_config
        lines = plugin.get_formatted_display()  # self.board is unbound here

        flagship = BoardContext.from_device_type("flagship")
        assert lines is not None
        assert len(lines) == flagship.rows
        assert all(len(line) <= flagship.cols for line in lines)

    @patch("plugins.spacecraft_launches.requests.get")
    def test_get_formatted_display_fits_a_note(self, mock_get, plugin, sample_config, mock_launches_response):
        """A Note (15x3) must get exactly 3 rows, none wider than 15 tiles --
        not the Flagship's 6x22 frame."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_launches_response
        mock_get.return_value = mock_response

        plugin.config = sample_config
        note = BoardContext.from_device_type("note")
        with plugin._bound_board(note):
            lines = plugin.get_formatted_display()

        assert lines is not None
        assert len(lines) == note.rows
        assert all(len(line) <= note.cols for line in lines)

    @patch("plugins.spacecraft_launches.requests.get")
    def test_taller_note_array_shows_more_launches_than_a_note(
        self, mock_get, plugin, mock_launches_response
    ):
        """A taller board must reflow to more list items, not stay capped at
        whatever a Note has room for -- the bug this fix removes was a
        hardcoded `launches[:4]` regardless of board size."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_launches_response
        mock_get.return_value = mock_response

        # Plenty of launches available so the taller board genuinely has
        # more to show, not just "ran out of content".
        plugin.config = {"max_launches": 24, "refresh_seconds": 300}

        note = BoardContext.from_device_type("note")
        with plugin._bound_board(note):
            note_lines = plugin.get_formatted_display()

        tall_array = BoardContext(device_type="note_array", rows=24, cols=15)
        with plugin._bound_board(tall_array):
            tall_lines = plugin.get_formatted_display()

        note_content_rows = sum(1 for line in note_lines if line.strip())
        tall_content_rows = sum(1 for line in tall_lines if line.strip())
        assert tall_content_rows > note_content_rows

    @patch("plugins.spacecraft_launches.requests.get")
    def test_own_resilience_cache_is_keyed_by_board_geometry(
        self, mock_get, plugin, sample_config, mock_launches_response
    ):
        """A Flagship's fetch must not populate a Note's cache slot, or one
        board's stale frame ends up served to a differently-shaped board."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_launches_response
        mock_get.return_value = mock_response

        plugin.config = sample_config
        flagship = BoardContext.from_device_type("flagship")
        note = BoardContext.from_device_type("note")

        with plugin._bound_board(flagship):
            plugin.fetch_data()
        with plugin._bound_board(note):
            plugin.fetch_data()

        assert set(plugin._cache.keys()) == {"flagship", "note"}


class TestCleanup:
    """Test plugin cleanup."""

    def test_cleanup(self, plugin):
        """Test cleanup clears cache."""
        plugin._cache = {"flagship": {"some": "data"}}
        plugin.cleanup()
        assert plugin._cache == {}


class TestVariablesMatchManifest:
    """Test that returned data matches manifest variables."""

    @patch("plugins.spacecraft_launches.requests.get")
    def test_simple_variables_present(self, mock_get, plugin, sample_config, sample_manifest, mock_launches_response):
        """Test all simple variables from manifest are in fetch_data result."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_launches_response
        mock_get.return_value = mock_response

        plugin.config = sample_config
        result = plugin.fetch_data()

        assert result.available is True
        declared_vars = sample_manifest["variables"]["simple"]
        for var_name in declared_vars.keys():
            assert var_name in result.data, f"Variable '{var_name}' declared in manifest but not in data"

    @patch("plugins.spacecraft_launches.requests.get")
    def test_array_item_fields_present(self, mock_get, plugin, sample_config, sample_manifest, mock_launches_response):
        """Test all array item fields from manifest are in launch data."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_launches_response
        mock_get.return_value = mock_response

        plugin.config = sample_config
        result = plugin.fetch_data()

        assert result.available is True
        assert len(result.data["launches"]) > 0

        declared_fields = sample_manifest["variables"]["arrays"]["launches"]["item_fields"]
        first_launch = result.data["launches"][0]
        for field in declared_fields:
            assert field in first_launch, f"Array field '{field}' declared in manifest but not in launch data"


class TestManifestMetadata:
    """Test that manifest contains rich variable metadata."""

    @pytest.fixture(autouse=True)
    def load_manifest(self):
        """Load manifest.json for metadata tests."""
        manifest_path = Path(__file__).parent.parent / "manifest.json"
        with open(manifest_path) as f:
            self.manifest = json.load(f)

    def test_simple_variables_are_dict(self):
        """Simple variables should be a dict (rich metadata), not a list."""
        simple = self.manifest["variables"]["simple"]
        assert isinstance(simple, dict), "simple variables must be a dict with metadata, not a list"

    def test_variable_groups_defined(self):
        """Variable groups must be declared."""
        groups = self.manifest["variables"]["groups"]
        assert isinstance(groups, dict)
        assert len(groups) > 0

    def test_each_simple_var_has_required_fields(self):
        """Every simple variable must have description, type, max_length, group, and example."""
        required_keys = {"description", "type", "max_length", "group", "example"}
        simple = self.manifest["variables"]["simple"]
        for var_name, meta in simple.items():
            missing = required_keys - set(meta.keys())
            assert not missing, f"Variable '{var_name}' missing metadata keys: {missing}"

    def test_variable_groups_reference_valid_groups(self):
        """Every simple variable's group must reference a declared group."""
        groups = set(self.manifest["variables"]["groups"].keys())
        simple = self.manifest["variables"]["simple"]
        for var_name, meta in simple.items():
            assert meta["group"] in groups, (
                f"Variable '{var_name}' references undeclared group '{meta['group']}'"
            )

    def test_variable_types_are_valid(self):
        """Variable types must be recognized JSON Schema types."""
        valid_types = {"string", "number", "integer", "boolean", "array", "object"}
        simple = self.manifest["variables"]["simple"]
        for var_name, meta in simple.items():
            assert meta["type"] in valid_types, (
                f"Variable '{var_name}' has invalid type '{meta['type']}'"
            )

    def test_max_length_is_positive_int(self):
        """max_length must be a positive integer."""
        simple = self.manifest["variables"]["simple"]
        for var_name, meta in simple.items():
            ml = meta["max_length"]
            assert isinstance(ml, int) and ml > 0, (
                f"Variable '{var_name}' max_length must be a positive int, got {ml}"
            )

    def test_array_variables_have_item_fields(self):
        """Array variables must declare item_fields."""
        arrays = self.manifest["variables"]["arrays"]
        for arr_name, arr_meta in arrays.items():
            assert "item_fields" in arr_meta, f"Array '{arr_name}' missing item_fields"
            assert len(arr_meta["item_fields"]) > 0

    def test_max_lengths_cover_array_fields(self):
        """Every array item_field should have a corresponding max_lengths entry."""
        arrays = self.manifest["variables"]["arrays"]
        max_lengths = self.manifest.get("max_lengths", {})
        for arr_name, arr_meta in arrays.items():
            for field in arr_meta["item_fields"]:
                key = f"{arr_name}.*.{field}"
                assert key in max_lengths, f"max_lengths missing entry for '{key}'"

    def test_group_labels_are_strings(self):
        """Group labels must be non-empty strings."""
        groups = self.manifest["variables"]["groups"]
        for group_id, group_meta in groups.items():
            assert "label" in group_meta, f"Group '{group_id}' missing 'label'"
            assert isinstance(group_meta["label"], str) and len(group_meta["label"]) > 0


class TestManifestHonesty:
    """`max_lengths` must bound what the code actually emits, not just what
    a short test fixture happens to produce -- the Launch Library API's
    text fields (names, pads, locations) carry no length guarantee."""

    @pytest.fixture(autouse=True)
    def load_manifest(self):
        manifest_path = Path(__file__).parent.parent / "manifest.json"
        with open(manifest_path) as f:
            self.manifest = json.load(f)

    @pytest.mark.parametrize(
        "field",
        ["name", "status", "net", "pad", "pad_location", "provider", "rocket", "mission", "formatted"],
    )
    def test_parsed_field_never_exceeds_its_declared_bound(self, plugin, field):
        """Feed in a launch whose every text field is absurdly long and
        confirm the parsed result is still clipped to the manifest's own
        `launches.*.<field>` bound."""
        very_long = "X" * 200
        launch = {
            "name": f"{very_long} | {very_long}",
            "status": {"name": very_long, "abbrev": very_long},
            "net": "2026-03-15T14:30:00Z",
            "pad": {"name": very_long, "location": {"name": very_long}},
            "launch_service_provider": {"name": very_long},
            "rocket": {"configuration": {"name": very_long}},
            "mission": {"name": very_long},
        }
        parsed = plugin._parse_launch(launch)
        assert parsed is not None

        limit = self.manifest["max_lengths"][f"launches.*.{field}"]
        assert len(parsed[field]) <= limit, (
            f"launches.*.{field} declares max_length {limit} but code emitted "
            f"{len(parsed[field])} for absurdly long input data"
        )
