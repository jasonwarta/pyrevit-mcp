"""Core conformance tests for the Revit MCP server (Section 12.1).

These tests validate the MCP server in isolation using a mock HTTP backend.
No Revit or pyRevit installation is required.
"""

import json
import os
import sys

import httpx
import pytest
import respx

# Ensure the server module is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "revit-mcp-server"))

# Set env before importing server
os.environ.setdefault("REVIT_ROUTES_HOST", "127.0.0.1")
os.environ.setdefault("REVIT_ROUTES_PORT", "48884")

import server  # noqa: E402

BASE = server.BASE_URL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse(result: str) -> dict:
    """Parse a JSON string returned by an MCP tool."""
    return json.loads(result)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestRevitHealth:
    @respx.mock
    def test_health_sends_get(self):
        route = respx.get(f"{BASE}/health").mock(
            return_value=httpx.Response(200, json={
                "status": "ok",
                "extension": "camper-mcp",
                "revit_version": "2025",
                "doc_title": "Test.rvt",
                "api_version": "1.0.0",
            })
        )
        result = _parse(server.revit_health())
        assert route.called
        assert result["status"] == "ok"
        assert result["doc_title"] == "Test.rvt"

    @respx.mock
    def test_health_connection_refused(self):
        respx.get(f"{BASE}/health").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(RuntimeError, match="Cannot connect to Revit"):
            server.revit_health()

    @respx.mock
    def test_health_timeout(self):
        respx.get(f"{BASE}/health").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        with pytest.raises(RuntimeError, match="did not respond"):
            server.revit_health()


# ---------------------------------------------------------------------------
# Execute Code
# ---------------------------------------------------------------------------

class TestRevitExecute:
    @respx.mock
    def test_execute_sends_post_with_correct_body(self):
        route = respx.post(f"{BASE}/execute").mock(
            return_value=httpx.Response(200, json={
                "success": True,
                "result": "Project1.rvt",
                "error": None,
                "traceback": None,
                "execution_time_ms": 12,
                "transaction_status": "no_transaction",
            })
        )
        result = _parse(server.revit_execute(code="return doc.Title"))
        assert route.called
        req = route.calls[0].request
        body = json.loads(req.content)
        assert body["code"] == "return doc.Title"
        assert body["timeout"] == 30  # default
        assert "transaction_name" not in body
        assert result["success"] is True
        assert result["result"] == "Project1.rvt"

    @respx.mock
    def test_execute_with_transaction(self):
        route = respx.post(f"{BASE}/execute").mock(
            return_value=httpx.Response(200, json={
                "success": True,
                "result": None,
                "error": None,
                "traceback": None,
                "execution_time_ms": 50,
                "transaction_status": "committed",
            })
        )
        result = _parse(server.revit_execute(
            code="doc.Create.NewWall()",
            transaction_name="Create Wall",
        ))
        body = json.loads(route.calls[0].request.content)
        assert body["transaction_name"] == "Create Wall"
        assert result["transaction_status"] == "committed"

    @respx.mock
    def test_execute_application_error(self):
        respx.post(f"{BASE}/execute").mock(
            return_value=httpx.Response(200, json={
                "success": False,
                "result": None,
                "error": "NameError: name 'foo' is not defined",
                "traceback": "  File ...",
                "execution_time_ms": 2,
                "transaction_status": "rolled_back",
            })
        )
        with pytest.raises(RuntimeError, match="NameError"):
            server.revit_execute(code="foo.bar()")


# ---------------------------------------------------------------------------
# List Elements
# ---------------------------------------------------------------------------

class TestRevitListElements:
    @respx.mock
    def test_list_elements_sends_correct_request(self):
        route = respx.post(f"{BASE}/elements/walls").mock(
            return_value=httpx.Response(200, json={
                "count": 1,
                "elements": [{
                    "element_id": 12345,
                    "category": "Walls",
                    "family": None,
                    "type": "Generic - 6\"",
                    "name": "Generic - 6\"",
                    "location": None,
                    "parameters": {},
                }],
            })
        )
        result = _parse(server.revit_list_elements(category="walls"))
        assert route.called
        assert result["count"] == 1

    @respx.mock
    def test_list_elements_with_parameters(self):
        route = respx.post(f"{BASE}/elements/walls").mock(
            return_value=httpx.Response(200, json={"count": 0, "elements": []})
        )
        server.revit_list_elements(
            category="walls", include_parameters=["Mark", "Width"]
        )
        body = json.loads(route.calls[0].request.content)
        assert body["include_parameters"] == ["Mark", "Width"]


# ---------------------------------------------------------------------------
# Get Element
# ---------------------------------------------------------------------------

class TestRevitGetElement:
    @respx.mock
    def test_get_element(self):
        respx.get(f"{BASE}/elements/id/12345").mock(
            return_value=httpx.Response(200, json={
                "element_id": 12345,
                "category": "Walls",
                "family": None,
                "type": "Generic - 6\"",
                "name": "Generic - 6\"",
                "location": None,
                "parameters": {"Mark": "W-01"},
            })
        )
        result = _parse(server.revit_get_element(element_id=12345))
        assert result["element_id"] == 12345
        assert result["parameters"]["Mark"] == "W-01"


# ---------------------------------------------------------------------------
# Set Parameters
# ---------------------------------------------------------------------------

class TestRevitSetParameters:
    @respx.mock
    def test_set_parameters(self):
        route = respx.put(f"{BASE}/elements/id/12345/parameters").mock(
            return_value=httpx.Response(200, json={
                "success": True,
                "element_id": 12345,
                "updated": ["Mark"],
                "failed": [],
                "transaction_status": "committed",
            })
        )
        result = _parse(server.revit_set_parameters(
            element_id=12345, parameters={"Mark": "W-01"}
        ))
        assert result["success"] is True
        body = json.loads(route.calls[0].request.content)
        assert body["parameters"]["Mark"] == "W-01"


# ---------------------------------------------------------------------------
# Place Part
# ---------------------------------------------------------------------------

class TestRevitPlacePart:
    @respx.mock
    def test_place_part_maps_parameters(self):
        route = respx.post(f"{BASE}/panels/2001/parts").mock(
            return_value=httpx.Response(200, json={
                "success": True,
                "element_id": 10015,
                "family": "Stud_1.5",
                "type": "Standard",
                "phase": "Driver Side Front",
                "parameters": {"Width": 1.5, "Length": 22.5},
                "transaction_status": "committed",
            })
        )
        result = _parse(server.revit_place_part(
            phase_id=2001,
            family_name="Stud_1.5",
            type_name="Standard",
            x=2.0, y=0.0, z=0.75,
            rotation_degrees=90.0,
            parameters={"Length": 22.5, "Offset": 0.0},
        ))
        body = json.loads(route.calls[0].request.content)
        assert body["family_name"] == "Stud_1.5"
        assert body["location"] == {"x": 2.0, "y": 0.0, "z": 0.75}
        assert body["rotation_degrees"] == 90.0
        assert body["parameters"]["Length"] == 22.5
        assert result["success"] is True


# ---------------------------------------------------------------------------
# Cut List
# ---------------------------------------------------------------------------

class TestRevitGenerateCutlist:
    @respx.mock
    def test_cutlist_maps_scope_and_grouping(self):
        route = respx.post(f"{BASE}/cutlist").mock(
            return_value=httpx.Response(200, json={
                "scope": "panel",
                "total_parts": 14,
                "groups": [],
            })
        )
        result = _parse(server.revit_generate_cutlist(
            scope="panel",
            phase_id=2001,
            group_by="thickness",
            sort_by="length_desc",
        ))
        body = json.loads(route.calls[0].request.content)
        assert body["scope"] == "panel"
        assert body["phase_id"] == 2001
        assert body["group_by"] == "thickness"
        assert body["sort_by"] == "length_desc"
        assert result["scope"] == "panel"


# ---------------------------------------------------------------------------
# Convert Units (pure math, no HTTP)
# ---------------------------------------------------------------------------

class TestRevitConvertUnits:
    def test_feet_to_inches(self):
        result = _parse(server.revit_convert_units(1.0, "feet", "inches"))
        assert result["value"] == 12.0

    def test_inches_to_feet(self):
        result = _parse(server.revit_convert_units(12.0, "inches", "feet"))
        assert result["value"] == 1.0

    def test_mm_to_feet(self):
        result = _parse(server.revit_convert_units(304.8, "mm", "feet"))
        assert result["value"] == 1.0

    def test_meters_to_feet(self):
        result = _parse(server.revit_convert_units(0.3048, "meters", "feet"))
        assert result["value"] == 1.0

    def test_inches_to_mm(self):
        result = _parse(server.revit_convert_units(1.0, "inches", "mm"))
        assert result["value"] == 25.4

    def test_unknown_unit(self):
        result = _parse(server.revit_convert_units(1.0, "furlongs", "feet"))
        assert "error" in result


# ---------------------------------------------------------------------------
# Connection errors
# ---------------------------------------------------------------------------

class TestConnectionErrors:
    @respx.mock
    def test_connection_refused_produces_mcp_error(self):
        respx.get(f"{BASE}/panels").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )
        with pytest.raises(RuntimeError, match="Cannot connect to Revit"):
            server.revit_list_panels()

    @respx.mock
    def test_timeout_produces_mcp_error(self):
        respx.get(f"{BASE}/panels").mock(
            side_effect=httpx.ReadTimeout("timeout")
        )
        with pytest.raises(RuntimeError, match="did not respond"):
            server.revit_list_panels()

    @respx.mock
    def test_application_error_propagated(self):
        respx.get(f"{BASE}/elements/id/99999").mock(
            return_value=httpx.Response(200, json={
                "success": False,
                "error": "Element 99999 not found",
            })
        )
        with pytest.raises(RuntimeError, match="Element 99999 not found"):
            server.revit_get_element(element_id=99999)


# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------

class TestConfig:
    def test_default_host(self):
        assert server.ROUTES_HOST == "127.0.0.1"

    def test_default_port(self):
        assert server.ROUTES_PORT == 48884

    def test_base_url(self):
        assert server.BASE_URL == "http://127.0.0.1:48884/camper-mcp"


# ---------------------------------------------------------------------------
# Discovery tools
# ---------------------------------------------------------------------------

class TestDiscoveryTools:
    @respx.mock
    def test_model_summary(self):
        respx.get(f"{BASE}/discovery/summary").mock(
            return_value=httpx.Response(200, json={
                "doc_title": "Test.rvt",
                "categories": [],
                "phases_as_panels": [],
                "levels": [],
            })
        )
        result = _parse(server.revit_model_summary())
        assert result["doc_title"] == "Test.rvt"

    @respx.mock
    def test_spatial_query_bounding_box(self):
        route = respx.post(f"{BASE}/discovery/spatial").mock(
            return_value=httpx.Response(200, json={
                "mode": "bounding_box", "count": 0, "elements": [],
            })
        )
        server.revit_spatial_query(
            mode="bounding_box",
            min_x=0, min_y=0, min_z=0,
            max_x=10, max_y=10, max_z=10,
        )
        body = json.loads(route.calls[0].request.content)
        assert body["mode"] == "bounding_box"
        assert body["min"]["x"] == 0
        assert body["max"]["x"] == 10

    @respx.mock
    def test_spatial_query_proximity(self):
        route = respx.post(f"{BASE}/discovery/spatial").mock(
            return_value=httpx.Response(200, json={
                "mode": "proximity", "count": 0, "elements": [],
            })
        )
        server.revit_spatial_query(
            mode="proximity",
            center_x=5, center_y=3, center_z=4,
            radius_ft=3.0,
        )
        body = json.loads(route.calls[0].request.content)
        assert body["mode"] == "proximity"
        assert body["center"]["x"] == 5
        assert body["radius_ft"] == 3.0

    @respx.mock
    def test_element_connections(self):
        respx.get(f"{BASE}/discovery/connections/12345").mock(
            return_value=httpx.Response(200, json={
                "element_id": 12345,
                "connections": {"joined_to": [], "hosted_elements": []},
            })
        )
        result = _parse(server.revit_element_connections(element_id=12345))
        assert result["element_id"] == 12345

    @respx.mock
    def test_model_bom(self):
        route = respx.post(f"{BASE}/discovery/bom").mock(
            return_value=httpx.Response(200, json={
                "scope": "all", "groups": [],
            })
        )
        server.revit_model_bom(scope="all", group_by="material")
        body = json.loads(route.calls[0].request.content)
        assert body["group_by"] == "material"

    @respx.mock
    def test_parameter_schema(self):
        respx.get(f"{BASE}/discovery/parameters").mock(
            return_value=httpx.Response(200, json={
                "shared_parameters": [],
                "project_parameters": [],
                "builtin_parameter_usage": {},
            })
        )
        result = _parse(server.revit_parameter_schema())
        assert "shared_parameters" in result

    @respx.mock
    def test_family_info(self):
        route = respx.post(f"{BASE}/discovery/families").mock(
            return_value=httpx.Response(200, json={"families": []})
        )
        server.revit_family_info(category="Walls")
        body = json.loads(route.calls[0].request.content)
        assert body["category"] == "Walls"

    @respx.mock
    def test_assembly_detail(self):
        respx.get(f"{BASE}/discovery/assembly/50001").mock(
            return_value=httpx.Response(200, json={
                "assembly_id": 50001, "members": [],
            })
        )
        result = _parse(server.revit_assembly_detail(assembly_id=50001))
        assert result["assembly_id"] == 50001

    @respx.mock
    def test_view_contents(self):
        respx.get(f"{BASE}/discovery/view/80001").mock(
            return_value=httpx.Response(200, json={
                "view_id": 80001, "name": "Test View",
            })
        )
        result = _parse(server.revit_view_contents(view_id=80001))
        assert result["view_id"] == 80001

    @respx.mock
    def test_sheet_index(self):
        respx.get(f"{BASE}/discovery/sheets").mock(
            return_value=httpx.Response(200, json={"sheets": []})
        )
        result = _parse(server.revit_sheet_index())
        assert "sheets" in result

    @respx.mock
    def test_warnings(self):
        respx.get(f"{BASE}/discovery/warnings").mock(
            return_value=httpx.Response(200, json={"count": 0, "warnings": []})
        )
        result = _parse(server.revit_warnings())
        assert result["count"] == 0


# ---------------------------------------------------------------------------
# Batch place parts
# ---------------------------------------------------------------------------

class TestBatchPlaceParts:
    @respx.mock
    def test_batch_place_maps_parts(self):
        route = respx.post(f"{BASE}/panels/2001/parts/batch").mock(
            return_value=httpx.Response(200, json={
                "success": True,
                "placed": [
                    {"index": 0, "element_id": 10020, "family": "Rail_1.5"},
                    {"index": 1, "element_id": 10021, "family": "Stud_1.5"},
                ],
                "failed": [],
                "transaction_status": "committed",
            })
        )
        result = _parse(server.revit_batch_place_parts(
            phase_id=2001,
            parts=[
                {
                    "family_name": "Rail_1.5",
                    "type_name": "Standard",
                    "x": 0.0, "y": 0.0, "z": 0.0,
                    "parameters": {"Length": 96.0},
                },
                {
                    "family_name": "Stud_1.5",
                    "type_name": "Standard",
                    "x": 0.0, "y": 0.0, "z": 0.75,
                    "rotation_degrees": 90.0,
                },
            ],
        ))
        body = json.loads(route.calls[0].request.content)
        assert len(body["parts"]) == 2
        assert body["parts"][0]["family_name"] == "Rail_1.5"
        assert body["parts"][0]["location"]["x"] == 0.0
        assert body["parts"][1]["rotation_degrees"] == 90.0
        assert result["success"] is True


# ---------------------------------------------------------------------------
# Session stats
# ---------------------------------------------------------------------------

class TestSessionStats:
    @respx.mock
    def test_stats_accumulate(self):
        # Reset stats
        server._stats.clear()

        respx.get(f"{BASE}/health").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        server.revit_health()
        server.revit_health()

        result = _parse(server.revit_session_stats())
        assert result["revit_health"]["calls"] == 2
        assert result["revit_health"]["success"] == 2
        assert result["revit_health"]["errors"] == 0


# ---------------------------------------------------------------------------
# Other tool requests
# ---------------------------------------------------------------------------

class TestOtherTools:
    @respx.mock
    def test_list_panels(self):
        respx.get(f"{BASE}/panels").mock(
            return_value=httpx.Response(200, json={"phases": []})
        )
        result = _parse(server.revit_list_panels())
        assert "phases" in result

    @respx.mock
    def test_get_panel(self):
        respx.get(f"{BASE}/panels/2001").mock(
            return_value=httpx.Response(200, json={
                "phase_id": 2001, "parts": [],
            })
        )
        result = _parse(server.revit_get_panel(phase_id=2001))
        assert result["phase_id"] == 2001

    @respx.mock
    def test_create_panel(self):
        route = respx.post(f"{BASE}/panels").mock(
            return_value=httpx.Response(200, json={
                "success": True, "phase_id": 2014,
            })
        )
        result = _parse(server.revit_create_panel(name="Test Panel"))
        body = json.loads(route.calls[0].request.content)
        assert body["name"] == "Test Panel"
        assert result["success"] is True

    @respx.mock
    def test_list_planes(self):
        respx.get(f"{BASE}/planes").mock(
            return_value=httpx.Response(200, json={"planes": []})
        )
        result = _parse(server.revit_list_planes())
        assert "planes" in result

    @respx.mock
    def test_list_types(self):
        respx.get(f"{BASE}/types/walls").mock(
            return_value=httpx.Response(200, json={
                "category": "walls", "types": [],
            })
        )
        result = _parse(server.revit_list_types(category="walls"))
        assert result["category"] == "walls"

    @respx.mock
    def test_list_levels(self):
        respx.get(f"{BASE}/levels").mock(
            return_value=httpx.Response(200, json={"levels": []})
        )
        result = _parse(server.revit_list_levels())
        assert "levels" in result

    @respx.mock
    def test_get_schedules(self):
        respx.get(f"{BASE}/schedules").mock(
            return_value=httpx.Response(200, json={"schedules": []})
        )
        result = _parse(server.revit_get_schedules())
        assert "schedules" in result

    @respx.mock
    def test_export_schedule(self):
        respx.post(f"{BASE}/schedules/export/100").mock(
            return_value=httpx.Response(200, json={
                "schedule_name": "Test", "headers": [], "rows": [],
            })
        )
        result = _parse(server.revit_export_schedule(schedule_id=100))
        assert result["schedule_name"] == "Test"

    @respx.mock
    def test_create_view(self):
        route = respx.post(f"{BASE}/create/view").mock(
            return_value=httpx.Response(200, json={
                "success": True, "view_id": 80001,
            })
        )
        server.revit_create_view(
            view_type="floor_plan", name="Test View", level="Level 1"
        )
        body = json.loads(route.calls[0].request.content)
        assert body["view_type"] == "floor_plan"
        assert body["level"] == "Level 1"

    @respx.mock
    def test_delete_elements(self):
        route = respx.post(f"{BASE}/elements").mock(
            return_value=httpx.Response(200, json={
                "success": True, "deleted": [123], "failed": [],
            })
        )
        result = _parse(server.revit_delete_elements(element_ids=[123]))
        assert result["deleted"] == [123]

    @respx.mock
    def test_get_materials(self):
        respx.get(f"{BASE}/materials/12345").mock(
            return_value=httpx.Response(200, json={
                "element_id": 12345, "materials": [],
            })
        )
        result = _parse(server.revit_get_materials(element_id=12345))
        assert result["element_id"] == 12345
