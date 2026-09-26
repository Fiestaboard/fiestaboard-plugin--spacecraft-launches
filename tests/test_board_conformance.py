"""Board-geometry conformance for the Spacecraft Launches plugin.

Verifies the plugin renders correctly on every board shape FiestaBoard
supports -- Flagship, Note, and every Note Array size from 15x3 up to
120x24 (also what a FiestaPanel is). See
``src/plugins/geometry_conformance.py`` in FiestaBoard core for what
"correctly" means; this is the same definition core holds its own plugins
to.
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch

from src.plugins.geometry_conformance import assert_board_conformance

from plugins.spacecraft_launches import SpacecraftLaunchesPlugin

MANIFEST_PATH = Path(__file__).parent.parent / "manifest.json"
with open(MANIFEST_PATH) as f:
    MANIFEST = json.load(f)


def _mock_launches_response(count: int) -> dict:
    """Build *count* distinct upcoming launches for network stubbing.

    24 is enough to fill the largest geometry in the growth ladder (a
    120x24 note array minus its 2-row header = 22 launch rows) with real,
    distinct content -- so growth across the ladder reflects the plugin
    actually having more to say, not the mock running out.
    """
    results = []
    for i in range(count):
        month = (i % 12) + 1
        day = (i % 27) + 1
        results.append(
            {
                "id": f"launch-{i}",
                "name": f"Rocket {i} | Mission {i}",
                "status": {"id": 1, "name": "Go for Launch", "abbrev": "Go"},
                "net": f"2026-{month:02d}-{day:02d}T12:00:00Z",
                "pad": {
                    "id": i,
                    "name": f"Pad {i}",
                    "location": {"name": f"Site {i}"},
                },
                "launch_service_provider": {"id": i, "name": "Provider"},
                "rocket": {"configuration": {"name": "Rocket"}},
                "mission": {"name": f"Mission {i}"},
            }
        )
    return {"count": count, "results": results}


def test_renders_on_every_board_shape():
    """The plugin must adapt to every board shape, growing its launch list
    on taller boards rather than staying capped at whatever fits a Note."""
    response = Mock()
    response.status_code = 200
    response.json.return_value = _mock_launches_response(24)

    def make_plugin() -> SpacecraftLaunchesPlugin:
        """A fresh, ready-to-render plugin configured with enough headroom
        (max_launches=24, the manifest's raised ceiling) that a 24-row panel
        genuinely has more to show than a Note does."""
        plugin = SpacecraftLaunchesPlugin(MANIFEST)
        plugin.config = {
            "enabled": True,
            "max_launches": 24,
            "refresh_seconds": 300,
        }
        return plugin

    # Patched once around the whole suite run rather than per-instance: the
    # suite calls the factory several times (once per check), and every
    # instance it produces must see the network stubbed the same way.
    with patch("plugins.spacecraft_launches.requests.get", return_value=response):
        assert_board_conformance(
            make_plugin,
            manifest=MANIFEST,
            strict_growth=True,
            require_note_array_preview=True,
        )
