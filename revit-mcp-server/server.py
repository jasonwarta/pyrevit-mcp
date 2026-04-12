"""Revit MCP Server — bridges Claude Code to pyRevit Routes over HTTP.

This is a standalone CPython 3.10+ process that implements the MCP protocol
over stdio and proxies tool calls to the pyRevit Routes HTTP API running
inside Revit.

Configuration (environment variables):
    REVIT_ROUTES_HOST   — default 127.0.0.1
    REVIT_ROUTES_PORT   — default 48884
    REVIT_REQUEST_TIMEOUT — HTTP timeout in seconds, default 60
    REVIT_CODE_TIMEOUT  — default code execution timeout, default 30
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections import defaultdict
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[revit-mcp] %(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("revit-mcp")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROUTES_HOST = os.environ.get("REVIT_ROUTES_HOST", "127.0.0.1")
ROUTES_PORT = int(os.environ.get("REVIT_ROUTES_PORT", "48884"))
REQUEST_TIMEOUT = int(os.environ.get("REVIT_REQUEST_TIMEOUT", "60"))
CODE_TIMEOUT = int(os.environ.get("REVIT_CODE_TIMEOUT", "30"))

BASE_URL = f"http://{ROUTES_HOST}:{ROUTES_PORT}/camper-mcp"

# ---------------------------------------------------------------------------
# Session stats (Section 8.3)
# ---------------------------------------------------------------------------

_stats: dict[str, dict[str, Any]] = defaultdict(
    lambda: {"calls": 0, "success": 0, "errors": 0, "total_ms": 0.0}
)


def _record_stat(tool_name: str, success: bool, elapsed_ms: float) -> None:
    _stats[tool_name]["calls"] += 1
    _stats[tool_name]["total_ms"] += elapsed_ms
    if success:
        _stats[tool_name]["success"] += 1
    else:
        _stats[tool_name]["errors"] += 1


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

_client = httpx.Client(base_url=BASE_URL, timeout=REQUEST_TIMEOUT)


def _request(
    method: str,
    path: str,
    json_body: dict | None = None,
    tool_name: str = "",
) -> dict:
    """Send an HTTP request to pyRevit Routes and return the parsed JSON.

    Raises RuntimeError on connection or HTTP errors so the MCP tool handler
    can convert them to ``isError`` responses.
    """
    start = time.time()
    try:
        resp = _client.request(method, path, json=json_body)
        elapsed_ms = (time.time() - start) * 1000
        data = resp.json()

        # Application-level error
        if isinstance(data, dict) and data.get("success") is False:
            _record_stat(tool_name, False, elapsed_ms)
            error_msg = data.get("error", "Unknown error")
            tb = data.get("traceback")
            if tb:
                error_msg += "\n" + tb
            raise RuntimeError(error_msg)

        _record_stat(tool_name, True, elapsed_ms)
        return data

    except httpx.ConnectError:
        elapsed_ms = (time.time() - start) * 1000
        _record_stat(tool_name, False, elapsed_ms)
        raise RuntimeError(
            f"Cannot connect to Revit. Is Revit running with pyRevit Routes "
            f"enabled on {ROUTES_HOST}:{ROUTES_PORT}?"
        )
    except httpx.TimeoutException:
        elapsed_ms = (time.time() - start) * 1000
        _record_stat(tool_name, False, elapsed_ms)
        raise RuntimeError(
            f"Revit did not respond within {REQUEST_TIMEOUT}s. "
            f"It may be busy or hung."
        )
    except RuntimeError:
        raise
    except Exception as exc:
        elapsed_ms = (time.time() - start) * 1000
        _record_stat(tool_name, False, elapsed_ms)
        raise RuntimeError(f"Unexpected error communicating with Revit: {exc}")


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "revit",
    instructions="Bridge to Autodesk Revit via pyRevit Routes — create, query, "
    "and modify Revit models in real time.",
)

# ===================================================================
# 7.4.1  revit_health
# ===================================================================


@mcp.tool()
def revit_health() -> str:
    """Check connection to Revit and get session info.

    Returns health status, Revit version, and open document name.
    Call this first to verify the Revit connection is working.
    """
    data = _request("GET", "/health", tool_name="revit_health")
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.2  revit_execute
# ===================================================================


@mcp.tool()
def revit_execute(
    code: str,
    timeout: int = CODE_TIMEOUT,
    transaction_name: str | None = None,
) -> str:
    """Execute arbitrary Python code inside Revit's process.

    The code has access to: uiapp, uidoc, doc, DB (Revit.DB), UI (Revit.UI).
    Use 'return' to send a value back. If transaction_name is provided,
    the code runs inside a named Revit transaction (for model-modifying ops).

    IMPORTANT: This is an escape hatch for operations not covered by the
    dedicated tools. If you find yourself executing similar code more than
    once for the same purpose, you MUST propose adding a new dedicated tool
    for that operation instead of continuing to use revit_execute. Dedicated
    tools are safer, faster, and produce consistent results. Tell the user:
    "I've used revit_execute for [X] multiple times — we should add a
    dedicated tool for this." Then help build it.

    Args:
        code: Python code to execute inside Revit.
        timeout: Max execution time in seconds (default 30).
        transaction_name: If provided, wraps execution in a Revit transaction.
    """
    body: dict[str, Any] = {"code": code, "timeout": timeout}
    if transaction_name is not None:
        body["transaction_name"] = transaction_name
    data = _request("POST", "/execute", json_body=body, tool_name="revit_execute")
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.3  revit_list_elements
# ===================================================================


@mcp.tool()
def revit_list_elements(
    category: str,
    include_parameters: list[str] | None = None,
) -> str:
    """List elements by category (e.g., 'walls', 'doors', 'furniture').

    Args:
        category: Revit category name (case-insensitive).
        include_parameters: Optional list of parameter names to include in results.
    """
    body = {}
    if include_parameters:
        body["include_parameters"] = include_parameters
    data = _request(
        "POST", f"/elements/{category}", json_body=body,
        tool_name="revit_list_elements",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.4  revit_get_element
# ===================================================================


@mcp.tool()
def revit_get_element(element_id: int) -> str:
    """Get detailed info for a single element including all parameters.

    Args:
        element_id: Revit ElementId as integer.
    """
    data = _request(
        "GET", f"/elements/id/{element_id}", tool_name="revit_get_element",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.5  revit_set_parameters
# ===================================================================


@mcp.tool()
def revit_set_parameters(element_id: int, parameters: dict[str, Any]) -> str:
    """Set parameters on an element.

    Args:
        element_id: Target element ID.
        parameters: Map of parameter name to value (e.g. {"Mark": "W-01"}).
    """
    data = _request(
        "PUT", f"/elements/id/{element_id}/parameters",
        json_body={"parameters": parameters},
        tool_name="revit_set_parameters",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.6  revit_list_panels
# ===================================================================


@mcp.tool()
def revit_list_panels() -> str:
    """List all panels (phases) in the project with part counts."""
    data = _request("GET", "/panels", tool_name="revit_list_panels")
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.7  revit_get_panel
# ===================================================================


@mcp.tool()
def revit_get_panel(phase_id: int) -> str:
    """Get all parts on a panel with families, parameters, and positions.

    Args:
        phase_id: Phase ElementId representing the panel.
    """
    data = _request(
        "GET", f"/panels/{phase_id}", tool_name="revit_get_panel",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.8  revit_place_part
# ===================================================================


@mcp.tool()
def revit_place_part(
    phase_id: int,
    family_name: str,
    type_name: str,
    x: float,
    y: float,
    z: float,
    rotation_degrees: float = 0.0,
    parameters: dict[str, Any] | None = None,
) -> str:
    """Place a single part family instance on a panel.

    Args:
        phase_id: Target panel (phase) ID.
        family_name: Part family name (e.g. "Stud_1.5").
        type_name: Type name within the family (e.g. "Standard").
        x: X location in feet.
        y: Y location in feet.
        z: Z location in feet.
        rotation_degrees: Z-axis rotation in degrees (default 0).
        parameters: Optional parameter values to set after placement.
    """
    body: dict[str, Any] = {
        "family_name": family_name,
        "type_name": type_name,
        "location": {"x": x, "y": y, "z": z},
        "rotation_degrees": rotation_degrees,
    }
    if parameters:
        body["parameters"] = parameters
    data = _request(
        "POST", f"/panels/{phase_id}/parts",
        json_body=body, tool_name="revit_place_part",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.9  revit_batch_place_parts
# ===================================================================


@mcp.tool()
def revit_batch_place_parts(
    phase_id: int,
    parts: list[dict[str, Any]],
) -> str:
    """Place multiple parts on a panel in a single atomic transaction.

    Each part dict should have: family_name, type_name, x, y, z,
    optional rotation_degrees, optional parameters.

    Args:
        phase_id: Target panel (phase) ID.
        parts: List of part definitions. Each has family_name, type_name,
               location (x/y/z in feet), optional rotation_degrees,
               optional parameters dict.
    """
    # Normalize part format for the API
    api_parts = []
    for p in parts:
        part_body: dict[str, Any] = {
            "family_name": p["family_name"],
            "type_name": p["type_name"],
            "location": {
                "x": p.get("x", p.get("location", {}).get("x", 0.0)),
                "y": p.get("y", p.get("location", {}).get("y", 0.0)),
                "z": p.get("z", p.get("location", {}).get("z", 0.0)),
            },
            "rotation_degrees": p.get("rotation_degrees", 0.0),
        }
        if "parameters" in p:
            part_body["parameters"] = p["parameters"]
        api_parts.append(part_body)

    data = _request(
        "POST", f"/panels/{phase_id}/parts/batch",
        json_body={"parts": api_parts},
        tool_name="revit_batch_place_parts",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.10  revit_create_panel
# ===================================================================


@mcp.tool()
def revit_create_panel(
    name: str,
    create_reference_plane: bool = True,
    plane_origin_x: float = 0.0,
    plane_origin_y: float = 0.0,
    plane_origin_z: float = 0.0,
    plane_direction_x: float = 0.0,
    plane_direction_y: float = 1.0,
    plane_direction_z: float = 0.0,
    plane_name: str | None = None,
) -> str:
    """Create a new panel (phase + optional reference plane).

    Args:
        name: Panel/phase name.
        create_reference_plane: Whether to create a reference plane (default True).
        plane_origin_x: Plane origin X in feet.
        plane_origin_y: Plane origin Y in feet.
        plane_origin_z: Plane origin Z in feet.
        plane_direction_x: Plane normal X component.
        plane_direction_y: Plane normal Y component.
        plane_direction_z: Plane normal Z component.
        plane_name: Reference plane name (defaults to panel name + " Plane").
    """
    body: dict[str, Any] = {
        "name": name,
        "create_reference_plane": create_reference_plane,
    }
    if create_reference_plane:
        body["plane_origin"] = {
            "x": plane_origin_x,
            "y": plane_origin_y,
            "z": plane_origin_z,
        }
        body["plane_direction"] = {
            "x": plane_direction_x,
            "y": plane_direction_y,
            "z": plane_direction_z,
        }
        if plane_name:
            body["plane_name"] = plane_name
    data = _request(
        "POST", "/panels", json_body=body, tool_name="revit_create_panel",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.11  revit_generate_cutlist
# ===================================================================


@mcp.tool()
def revit_generate_cutlist(
    scope: str = "all",
    phase_id: int | None = None,
    phase_ids: list[int] | None = None,
    group_by: str = "family",
    sort_by: str = "length_desc",
) -> str:
    """Generate a cut list from parts, grouped and sorted.

    Args:
        scope: "all", "panel", or "panels".
        phase_id: Single panel ID (required if scope is "panel").
        phase_ids: List of panel IDs (required if scope is "panels").
        group_by: "family" (default), "thickness", or "family_and_panel".
        sort_by: "length_desc" (default), "length_asc", or "panel".
    """
    body: dict[str, Any] = {
        "scope": scope,
        "group_by": group_by,
        "sort_by": sort_by,
    }
    if scope == "panel" and phase_id is not None:
        body["phase_id"] = phase_id
    if scope == "panels" and phase_ids is not None:
        body["phase_ids"] = phase_ids
    data = _request(
        "POST", "/cutlist", json_body=body, tool_name="revit_generate_cutlist",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.12  revit_list_planes
# ===================================================================


@mcp.tool()
def revit_list_planes() -> str:
    """List all named reference planes with origins and directions."""
    data = _request("GET", "/planes", tool_name="revit_list_planes")
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.13  revit_list_types
# ===================================================================


@mcp.tool()
def revit_list_types(category: str) -> str:
    """List available family types for a category.

    Args:
        category: Category name (e.g. "walls", "doors").
    """
    data = _request(
        "GET", f"/types/{category}", tool_name="revit_list_types",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.14  revit_list_levels
# ===================================================================


@mcp.tool()
def revit_list_levels() -> str:
    """List all levels in the project with elevations."""
    data = _request("GET", "/levels", tool_name="revit_list_levels")
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.15  revit_get_schedules
# ===================================================================


@mcp.tool()
def revit_get_schedules() -> str:
    """List all schedules in the project."""
    data = _request("GET", "/schedules", tool_name="revit_get_schedules")
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.16  revit_export_schedule
# ===================================================================


@mcp.tool()
def revit_export_schedule(schedule_id: int) -> str:
    """Export a schedule as structured data (headers + rows).

    Args:
        schedule_id: Schedule ElementId.
    """
    data = _request(
        "POST", f"/schedules/export/{schedule_id}",
        tool_name="revit_export_schedule",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.17  revit_create_view
# ===================================================================


@mcp.tool()
def revit_create_view(
    view_type: str,
    name: str,
    level: str | None = None,
    scale: int = 48,
    phase_id: int | None = None,
) -> str:
    """Create a new view.

    Args:
        view_type: One of: floor_plan, ceiling_plan, section, elevation, 3d.
        name: View name.
        level: Level name (required for plan views).
        scale: View scale denominator (default 48).
        phase_id: Optional phase ID to filter view to a single panel.
    """
    body: dict[str, Any] = {
        "view_type": view_type,
        "name": name,
        "scale": scale,
    }
    if level:
        body["level"] = level
    if phase_id is not None:
        body["phase_id"] = phase_id
    data = _request(
        "POST", "/create/view", json_body=body, tool_name="revit_create_view",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.18  revit_delete_elements
# ===================================================================


@mcp.tool()
def revit_delete_elements(element_ids: list[int]) -> str:
    """Delete one or more elements from the model.

    Args:
        element_ids: List of element IDs to delete.
    """
    data = _request(
        "POST", "/elements",
        json_body={"element_ids": element_ids, "_method": "DELETE"},
        tool_name="revit_delete_elements",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.19  revit_get_materials
# ===================================================================


@mcp.tool()
def revit_get_materials(element_id: int) -> str:
    """Get material quantities for an element.

    Args:
        element_id: Target element ID.
    """
    data = _request(
        "GET", f"/materials/{element_id}", tool_name="revit_get_materials",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.20  revit_convert_units
# ===================================================================

_UNIT_TO_FEET = {
    "feet": 1.0,
    "inches": 1.0 / 12.0,
    "mm": 1.0 / 304.8,
    "meters": 1.0 / 0.3048,
}


@mcp.tool()
def revit_convert_units(
    value: float,
    from_unit: str,
    to_unit: str,
) -> str:
    """Convert between unit systems (feet, inches, mm, meters).

    No Revit API call needed — pure math.

    Args:
        value: Numeric value to convert.
        from_unit: One of: feet, inches, mm, meters.
        to_unit: One of: feet, inches, mm, meters.
    """
    from_factor = _UNIT_TO_FEET.get(from_unit.lower())
    to_factor = _UNIT_TO_FEET.get(to_unit.lower())
    if from_factor is None:
        return json.dumps({"error": f"Unknown from_unit: {from_unit}"})
    if to_factor is None:
        return json.dumps({"error": f"Unknown to_unit: {to_unit}"})

    feet = value * from_factor
    result = feet / to_factor
    return json.dumps({
        "value": round(result, 6),
        "from": f"{value} {from_unit}",
        "to": f"{round(result, 6)} {to_unit}",
    })


# ===================================================================
# 7.4.21  revit_model_summary
# ===================================================================


@mcp.tool()
def revit_model_summary() -> str:
    """Get a high-level overview of the entire model.

    Returns category breakdown, levels, assemblies, groups, view/sheet/schedule
    counts, and warnings. This is the recommended first tool to call when
    exploring an unfamiliar model.
    """
    data = _request("GET", "/discovery/summary", tool_name="revit_model_summary")
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.22  revit_spatial_query
# ===================================================================


@mcp.tool()
def revit_spatial_query(
    mode: str,
    min_x: float | None = None,
    min_y: float | None = None,
    min_z: float | None = None,
    max_x: float | None = None,
    max_y: float | None = None,
    max_z: float | None = None,
    center_x: float | None = None,
    center_y: float | None = None,
    center_z: float | None = None,
    radius_ft: float | None = None,
    categories: list[str] | None = None,
) -> str:
    """Find elements within a bounding box or radius of a point.

    Args:
        mode: "bounding_box" or "proximity".
        min_x, min_y, min_z: Bounding box minimum corner (feet).
        max_x, max_y, max_z: Bounding box maximum corner (feet).
        center_x, center_y, center_z: Center point for proximity (feet).
        radius_ft: Search radius for proximity mode (feet).
        categories: Optional list of category names to filter.
    """
    body: dict[str, Any] = {"mode": mode}
    if mode == "bounding_box":
        body["min"] = {"x": min_x or 0, "y": min_y or 0, "z": min_z or 0}
        body["max"] = {"x": max_x or 0, "y": max_y or 0, "z": max_z or 0}
    elif mode == "proximity":
        body["center"] = {
            "x": center_x or 0, "y": center_y or 0, "z": center_z or 0,
        }
        body["radius_ft"] = radius_ft or 5.0
    if categories:
        body["categories"] = categories
    data = _request(
        "POST", "/discovery/spatial", json_body=body,
        tool_name="revit_spatial_query",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.23  revit_element_connections
# ===================================================================


@mcp.tool()
def revit_element_connections(element_id: int) -> str:
    """Get all elements connected to, hosted on, hosting, or touching a given element.

    Returns categorized lists: joined_to, hosted_elements, host, touching,
    cut_by, and assembly membership.

    Args:
        element_id: The element to query relationships for.
    """
    data = _request(
        "GET", f"/discovery/connections/{element_id}",
        tool_name="revit_element_connections",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.24  revit_model_bom
# ===================================================================


@mcp.tool()
def revit_model_bom(
    scope: str = "all",
    assembly_id: int | None = None,
    element_ids: list[int] | None = None,
    group_by: str = "category_and_type",
    include_materials: bool = True,
) -> str:
    """Generate a bill of materials for the model, an assembly, or specific elements.

    Args:
        scope: "all", "assembly", or "elements".
        assembly_id: Assembly ID (required if scope is "assembly").
        element_ids: Element IDs (required if scope is "elements").
        group_by: "category_and_type" (default), "material", or "family".
        include_materials: Include material breakdown (default True).
    """
    body: dict[str, Any] = {
        "scope": scope,
        "group_by": group_by,
        "include_materials": include_materials,
    }
    if scope == "assembly" and assembly_id is not None:
        body["assembly_id"] = assembly_id
    if scope == "elements" and element_ids is not None:
        body["element_ids"] = element_ids
    data = _request(
        "POST", "/discovery/bom", json_body=body, tool_name="revit_model_bom",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.25  revit_parameter_schema
# ===================================================================


@mcp.tool()
def revit_parameter_schema() -> str:
    """Get all shared/project parameters and common built-in parameter usage.

    Returns parameter definitions with GUIDs, groups, types, applicable
    categories, and sample values from the model.
    """
    data = _request(
        "GET", "/discovery/parameters", tool_name="revit_parameter_schema",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.26  revit_family_info
# ===================================================================


@mcp.tool()
def revit_family_info(category: str | None = None) -> str:
    """Get detailed info about all loaded families.

    Returns types, parameters, hosting behavior, and placement counts.

    Args:
        category: Optional category filter (e.g. "Walls"). Default: all.
    """
    body = {}
    if category:
        body["category"] = category
    data = _request(
        "POST", "/discovery/families", json_body=body,
        tool_name="revit_family_info",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.27  revit_assembly_detail
# ===================================================================


@mcp.tool()
def revit_assembly_detail(assembly_id: int) -> str:
    """Get the full contents of an assembly — members, views, and sheets.

    Args:
        assembly_id: Assembly ElementId.
    """
    data = _request(
        "GET", f"/discovery/assembly/{assembly_id}",
        tool_name="revit_assembly_detail",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.28  revit_view_contents
# ===================================================================


@mcp.tool()
def revit_view_contents(view_id: int) -> str:
    """Get metadata about a view and the elements visible in it.

    Returns view properties, visible elements by category, annotation counts,
    and sheet placements.

    Args:
        view_id: View ElementId.
    """
    data = _request(
        "GET", f"/discovery/view/{view_id}",
        tool_name="revit_view_contents",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.29  revit_sheet_index
# ===================================================================


@mcp.tool()
def revit_sheet_index() -> str:
    """Get all sheets and the views placed on each."""
    data = _request("GET", "/discovery/sheets", tool_name="revit_sheet_index")
    return json.dumps(data, indent=2)


# ===================================================================
# 7.4.30  revit_warnings
# ===================================================================


@mcp.tool()
def revit_warnings() -> str:
    """Get all active model warnings with descriptions, severities, and element IDs."""
    data = _request(
        "GET", "/discovery/warnings", tool_name="revit_warnings",
    )
    return json.dumps(data, indent=2)


# ===================================================================
# Bonus: revit_session_stats (Section 8.3)
# ===================================================================


@mcp.tool()
def revit_session_stats() -> str:
    """Get session statistics — tool call counts, success/error rates, avg response times."""
    stats_out = {}
    for name, s in _stats.items():
        avg_ms = s["total_ms"] / s["calls"] if s["calls"] > 0 else 0
        stats_out[name] = {
            "calls": s["calls"],
            "success": s["success"],
            "errors": s["errors"],
            "avg_response_ms": round(avg_ms, 1),
        }
    return json.dumps(stats_out, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info(
        "Starting Revit MCP server — target %s:%s", ROUTES_HOST, ROUTES_PORT
    )
    mcp.run(transport="stdio")
