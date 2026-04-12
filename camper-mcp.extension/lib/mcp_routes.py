# -*- coding: utf-8 -*-
"""Route handler functions for the camper-mcp pyRevit extension.

Each public function in this module is registered as a route handler by
``startup.py``.  Handlers that need Revit API context declare ``uiapp``
in their signature so pyRevit Routes marshals them to the main thread
via ``ExternalEvent``.

All routes are prefixed with ``/camper-mcp/`` (the API name).
"""

import json
import math
import time
import traceback

from System import Int64

from pyrevit.api import DB, UI
from pyrevit import routes, HOST_APP
from pyrevit.coreutils.logger import get_logger

from serializers import (
    eid_int,
    serialize,
    serialize_xyz,
    serialize_element_id,
    serialize_element_summary,
    serialize_element_summary_full,
    serialize_part_instance,
    serialize_location,
    serialize_bounding_box,
    get_parameter_value,
    get_parameters_dict,
)
from transaction_utils import run_in_transaction, run_read_only, TransactionResult

logger = get_logger(__name__)

API_VERSION = "1.0.0"
API_NAME = "camper-mcp"

api = routes.API(API_NAME)


# ===================================================================
# Helpers
# ===================================================================

def _resolve_category(category_name):
    """Map a user-friendly category name to a BuiltInCategory enum value.

    Exact match only (case-insensitive, spaces stripped). No substring
    guessing — returns None if no exact match found.
    """
    lookup = category_name.strip().lower().replace(" ", "")
    for bic in DB.BuiltInCategory.GetValues(DB.BuiltInCategory):
        name = str(bic).replace("OST_", "").lower()
        if name == lookup:
            return bic
    return None


def _find_family_symbol(doc, family_name, type_name):
    """Find a FamilySymbol by family + type name (case-insensitive)."""
    collector = DB.FilteredElementCollector(doc).OfClass(DB.FamilySymbol)
    fn_lower = family_name.strip().lower()
    tn_lower = type_name.strip().lower()
    for fs in collector:
        fam = fs.Family
        if fam is None:
            continue
        if fam.Name.strip().lower() == fn_lower:
            sym_name = DB.Element.Name.__get__(fs).strip().lower()
            if sym_name == tn_lower:
                return fs
    return None


def _list_available_families(doc):
    """Return a sorted list of 'Family : Type' strings for error messages."""
    collector = DB.FilteredElementCollector(doc).OfClass(DB.FamilySymbol)
    names = set()
    for fs in collector:
        fam = fs.Family
        if fam is None:
            continue
        names.add("{} : {}".format(fam.Name, DB.Element.Name.__get__(fs)))
    return sorted(names)


def _get_phase_by_id(doc, phase_id):
    """Get a Phase element by integer id."""
    eid = DB.ElementId(Int64(phase_id))
    elem = doc.GetElement(eid)
    if elem is None or not isinstance(elem, DB.Phase):
        return None
    return elem


def _resolve_phase(doc, phase_id=None, phase_name=None):
    """Resolve a phase by ID or name. Returns (phase, error_string)."""
    if phase_id is not None:
        phase = _get_phase_by_id(doc, phase_id)
        if phase:
            return phase, None
        return None, "Phase with ID {} not found".format(phase_id)
    if phase_name is not None:
        target = phase_name.strip().lower()
        for p in doc.Phases:
            if p.Name.strip().lower() == target:
                return p, None
        available = [p.Name for p in doc.Phases]
        return None, "Phase '{}' not found. Available phases: {}".format(
            phase_name, available
        )
    return None, "Either phase_id or phase_name is required"


def _get_elements_in_phase(doc, phase):
    """Return all family instances that belong to the given phase."""
    collector = (
        DB.FilteredElementCollector(doc)
        .OfClass(DB.FamilyInstance)
        .WhereElementIsNotElementType()
    )
    results = []
    for fi in collector:
        try:
            phase_param = fi.get_Parameter(DB.BuiltInParameter.PHASE_CREATED)
            if phase_param and phase_param.AsElementId() == phase.Id:
                results.append(fi)
        except Exception:
            pass
    return results


def _find_reference_plane_for_phase(doc, phase):
    """Find the reference plane associated with a phase (by naming convention).

    Matching priority:
    1. Exact match (case-insensitive)
    2. Plane name == phase name + " Plane" (case-insensitive)
    3. No match — returns None (no substring guessing)
    """
    phase_name = phase.Name.strip().lower()
    collector = DB.FilteredElementCollector(doc).OfClass(DB.ReferencePlane)
    for rp in collector:
        rp_name = (rp.Name or "").strip().lower()
        if rp_name == phase_name:
            return rp
    # Second pass: check for "phase name + Plane" convention
    expected = phase_name + " plane"
    for rp in DB.FilteredElementCollector(doc).OfClass(DB.ReferencePlane):
        rp_name = (rp.Name or "").strip().lower()
        if rp_name == expected:
            return rp
    return None


def _error_response(message, suggestion=None):
    """Build a standard error dict."""
    resp = {"success": False, "error": message}
    if suggestion:
        resp["suggestion"] = suggestion
    return resp


def _get_doc(uiapp):
    """Get the active document, or return None if no document is open."""
    uidoc = uiapp.ActiveUIDocument
    if uidoc is None:
        return None
    return uidoc.Document


def _require_doc(uiapp):
    """Get the active document, or return an error response dict."""
    doc = _get_doc(uiapp)
    if doc is None:
        return None, _error_response("No active Revit document is open")
    return doc, None


# ===================================================================
# 6.2.1  Health Check
# ===================================================================

@api.route("/health", methods=["GET"])
def health_check(doc):
    """Returns server and document status."""
    doc_title = None
    try:
        if doc is not None:
            doc_title = doc.Title
    except Exception:
        pass

    return {
        "status": "ok",
        "extension": API_NAME,
        "revit_version": str(HOST_APP.version),
        "doc_title": doc_title,
        "api_version": API_VERSION,
    }


# ===================================================================
# 6.2.2  Execute Code (Generic)
# ===================================================================

@api.route("/execute", methods=["POST"])
def execute_code(uiapp, request):
    data = request.data or {}
    code = data.get("code", "")
    timeout = data.get("timeout", 30)
    txn_name = data.get("transaction_name", None)

    doc, err = _require_doc(uiapp)
    if err:
        return err
    uidoc = uiapp.ActiveUIDocument
    start = time.time()

    namespace = {
        "uiapp": uiapp,
        "uidoc": uidoc,
        "doc": doc,
        "DB": DB,
        "UI": UI,
        "__result__": None,
    }

    # Wrap code in a function to support return statements
    # NOTE: textwrap.indent does not exist in IronPython 2.7 (Python 3.3+)
    indented = "\n".join("    " + line for line in code.split("\n"))
    wrapped = "def _execute():\n{}\n__result__ = _execute()".format(indented)

    txn = None
    txn_status = "no_transaction"

    try:
        if txn_name is not None:
            txn = DB.Transaction(doc, str(txn_name))
            txn.Start()
            txn_status = "started"

        exec(wrapped, namespace)

        if txn is not None:
            txn.Commit()
            txn_status = "committed"

        result = serialize(namespace["__result__"])
        elapsed = int((time.time() - start) * 1000)

        return {
            "success": True,
            "result": result,
            "error": None,
            "traceback": None,
            "execution_time_ms": elapsed,
            "transaction_status": txn_status,
        }

    except Exception as exc:
        if txn is not None and txn.HasStarted() and not txn.HasEnded():
            txn.RollBack()
            txn_status = "rolled_back"
        elapsed = int((time.time() - start) * 1000)
        return {
            "success": False,
            "result": None,
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "execution_time_ms": elapsed,
            "transaction_status": txn_status,
        }


# ===================================================================
# 6.2.3  List Elements by Category
# ===================================================================

@api.route("/elements/<category>", methods=["GET", "POST"])
def list_elements_by_category(uiapp, request, category):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    data = request.data or {}
    include_params = data.get("include_parameters", [])

    bic = _resolve_category(category)
    if bic is None:
        return _error_response("Category '{}' not found".format(category))

    collector = (
        DB.FilteredElementCollector(doc)
        .OfCategory(bic)
        .WhereElementIsNotElementType()
    )
    elements = []
    for elem in collector:
        summary = serialize_element_summary(elem, parameter_names=include_params or None)
        if summary:
            if not include_params:
                summary["parameters"] = {}
            elements.append(summary)

    return {"count": len(elements), "elements": elements}


# ===================================================================
# 6.2.4  Get Element by ID
# ===================================================================

@api.route("/elements/id/<int:element_id>", methods=["GET"])
def get_element_by_id(uiapp, request, element_id):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    eid = DB.ElementId(Int64(element_id))
    elem = doc.GetElement(eid)
    if elem is None:
        return _error_response("Element {} not found".format(element_id))
    return serialize_element_summary_full(elem)


# ===================================================================
# 6.2.5  Set Element Parameters
# ===================================================================

@api.route("/elements/id/<int:element_id>/parameters", methods=["PUT", "POST"])
def set_element_parameters(uiapp, request, element_id):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    data = request.data or {}
    params_to_set = data.get("parameters", {})

    eid = DB.ElementId(Int64(element_id))
    elem = doc.GetElement(eid)
    if elem is None:
        return _error_response("Element {} not found".format(element_id))

    updated = []
    failed = []

    txn = DB.Transaction(doc, "MCP Set Parameters")
    txn.Start()
    try:
        for name, value in params_to_set.items():
            param = elem.LookupParameter(name)
            if param is None:
                # Case-insensitive fallback
                for p in elem.Parameters:
                    if p.Definition.Name.strip().lower() == name.strip().lower():
                        param = p
                        break
            if param is None:
                failed.append({"name": name, "error": "Parameter not found"})
                continue
            if param.IsReadOnly:
                failed.append({"name": name, "error": "Parameter is read-only"})
                continue
            try:
                st = param.StorageType
                if st == DB.StorageType.String:
                    param.Set(str(value))
                elif st == DB.StorageType.Integer:
                    param.Set(int(value))
                elif st == DB.StorageType.Double:
                    param.Set(float(value))
                elif st == DB.StorageType.ElementId:
                    param.Set(DB.ElementId(Int64(int(value))))
                updated.append(name)
            except Exception as exc:
                failed.append({"name": name, "error": str(exc)})

        txn.Commit()
        txn_status = "committed"
    except Exception as exc:
        if txn.HasStarted() and not txn.HasEnded():
            txn.RollBack()
        return _error_response(str(exc))

    return {
        "success": len(updated) > 0 or len(failed) == 0,
        "element_id": element_id,
        "updated": updated,
        "failed": failed,
        "transaction_status": txn_status,
    }


# ===================================================================
# 6.2.6  List Phases (Panels)
# ===================================================================

@api.route("/panels", methods=["GET"])
def list_panels(uiapp, request):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    phases = doc.Phases
    result = []
    for phase in phases:
        parts = _get_elements_in_phase(doc, phase)
        result.append({
            "name": phase.Name,
            "element_id": eid_int(phase.Id),
            "part_count": len(parts),
        })
    return {"phases": result}


# ===================================================================
# 6.2.7  Get Panel Detail
# ===================================================================

@api.route("/panels/by-name/<phase_name>", methods=["GET"])
@api.route("/panels/<int:phase_id>", methods=["GET"])
def get_panel_detail(uiapp, request, phase_id=None, phase_name=None):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    phase, err = _resolve_phase(doc, phase_id=phase_id, phase_name=phase_name)
    if err:
        return _error_response(err)

    parts = _get_elements_in_phase(doc, phase)
    ref_plane = _find_reference_plane_for_phase(doc, phase)

    rp_info = None
    if ref_plane is not None:
        rp_info = {
            "name": ref_plane.Name,
            "element_id": eid_int(ref_plane.Id),
        }

    parts_list = [serialize_part_instance(p) for p in parts if p is not None]

    # parts_by_family summary
    by_family = {}
    for p in parts_list:
        if p and p.get("family"):
            by_family[p["family"]] = by_family.get(p["family"], 0) + 1

    return {
        "phase_id": phase_id,
        "phase_name": phase.Name,
        "reference_plane": rp_info,
        "part_count": len(parts_list),
        "parts": parts_list,
        "parts_by_family": by_family,
    }


# ===================================================================
# 6.2.8  Place Part on Panel
# ===================================================================

@api.route("/panels/<int:phase_id>/parts", methods=["POST"])
def place_part(uiapp, request, phase_id=None):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    data = request.data or {}

    phase_name = data.get("phase_name", None)
    phase, err = _resolve_phase(doc, phase_id=phase_id, phase_name=phase_name)
    if err:
        return _error_response(err)

    family_name = data.get("family_name", "")
    type_name = data.get("type_name", "")

    symbol = _find_family_symbol(doc, family_name, type_name)
    if symbol is None:
        available = _list_available_families(doc)
        return _error_response(
            "Family '{}' type '{}' not found. Available: {}".format(
                family_name, type_name, available[:20]
            )
        )

    loc = data.get("location", {})
    x = float(loc.get("x", 0.0))
    y = float(loc.get("y", 0.0))
    z = float(loc.get("z", 0.0))
    rotation = float(data.get("rotation_degrees", 0.0))
    params_to_set = data.get("parameters", {})

    txn = DB.Transaction(doc, "MCP Place Part")
    txn.Start()
    try:
        if not symbol.IsActive:
            symbol.Activate()
            doc.Regenerate()

        point = DB.XYZ(x, y, z)
        instance = doc.Create.NewFamilyInstance(
            point, symbol, DB.Structure.StructuralType.NonStructural
        )

        # Assign to phase
        # NOTE: PHASE_CREATED may be read-only depending on Revit version
        # and element category. If Set() fails or parameter is read-only,
        # we report it in the response so the caller knows.
        phase_warning = None
        phase_param = instance.get_Parameter(DB.BuiltInParameter.PHASE_CREATED)
        if phase_param is None:
            phase_warning = "Element has no PHASE_CREATED parameter"
        elif phase_param.IsReadOnly:
            phase_warning = "PHASE_CREATED is read-only on this element"
        else:
            try:
                phase_param.Set(phase.Id)
                # Verify it stuck
                if phase_param.AsElementId() != phase.Id:
                    phase_warning = "PHASE_CREATED was set but did not take effect"
            except Exception as pe:
                phase_warning = "Failed to set PHASE_CREATED: {}".format(pe)

        # Apply rotation
        if rotation != 0.0:
            axis = DB.Line.CreateBound(point, point + DB.XYZ.BasisZ)
            DB.ElementTransformUtils.RotateElement(
                doc, instance.Id, axis, math.radians(rotation)
            )

        # Set parameters
        for name, value in params_to_set.items():
            param = instance.LookupParameter(name)
            if param and not param.IsReadOnly:
                st = param.StorageType
                if st == DB.StorageType.String:
                    param.Set(str(value))
                elif st == DB.StorageType.Integer:
                    param.Set(int(value))
                elif st == DB.StorageType.Double:
                    param.Set(float(value))

        txn.Commit()

        # Read back final params
        final_params = get_parameters_dict(instance)

        resp = {
            "success": True,
            "element_id": eid_int(instance.Id),
            "family": family_name,
            "type": type_name,
            "phase": phase.Name,
            "parameters": final_params,
            "transaction_status": "committed",
        }
        if phase_warning:
            resp["phase_warning"] = phase_warning
        return resp

    except Exception as exc:
        if txn.HasStarted() and not txn.HasEnded():
            txn.RollBack()
        return _error_response(str(exc))


# ===================================================================
# 6.2.9  Batch Place Parts
# ===================================================================

@api.route("/panels/<int:phase_id>/parts/batch", methods=["POST"])
def batch_place_parts(uiapp, request, phase_id=None):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    data = request.data or {}

    phase_name = data.get("phase_name", None)
    phase, err = _resolve_phase(doc, phase_id=phase_id, phase_name=phase_name)
    if err:
        return _error_response(err)

    parts_data = data.get("parts", [])
    if not parts_data:
        return _error_response("No parts provided")

    txn = DB.Transaction(doc, "MCP Batch Place Parts")
    txn.Start()
    placed = []
    failed = []
    try:
        for idx, part in enumerate(parts_data):
            try:
                family_name = part.get("family_name", "")
                type_name = part.get("type_name", "")
                symbol = _find_family_symbol(doc, family_name, type_name)
                if symbol is None:
                    raise ValueError("Family '{}' type '{}' not found".format(
                        family_name, type_name))

                if not symbol.IsActive:
                    symbol.Activate()
                    doc.Regenerate()

                loc = part.get("location", {})
                x = float(loc.get("x", 0.0))
                y = float(loc.get("y", 0.0))
                z = float(loc.get("z", 0.0))
                point = DB.XYZ(x, y, z)

                instance = doc.Create.NewFamilyInstance(
                    point, symbol, DB.Structure.StructuralType.NonStructural
                )

                # Assign phase (may be read-only — see place_part)
                phase_param = instance.get_Parameter(DB.BuiltInParameter.PHASE_CREATED)
                if phase_param and not phase_param.IsReadOnly:
                    try:
                        phase_param.Set(phase.Id)
                    except Exception:
                        pass  # Phase assignment failure is non-fatal in batch

                # Rotation
                rotation = float(part.get("rotation_degrees", 0.0))
                if rotation != 0.0:
                    axis = DB.Line.CreateBound(point, point + DB.XYZ.BasisZ)
                    DB.ElementTransformUtils.RotateElement(
                        doc, instance.Id, axis, math.radians(rotation)
                    )

                # Parameters
                for name, value in part.get("parameters", {}).items():
                    param = instance.LookupParameter(name)
                    if param and not param.IsReadOnly:
                        st = param.StorageType
                        if st == DB.StorageType.String:
                            param.Set(str(value))
                        elif st == DB.StorageType.Integer:
                            param.Set(int(value))
                        elif st == DB.StorageType.Double:
                            param.Set(float(value))

                placed.append({
                    "index": idx,
                    "element_id": eid_int(instance.Id),
                    "family": family_name,
                })

            except Exception as exc:
                failed.append({"index": idx, "error": str(exc)})
                # Atomic: if any fails, roll back all
                raise

        txn.Commit()
        return {
            "success": True,
            "placed": placed,
            "failed": [],
            "transaction_status": "committed",
        }

    except Exception as exc:
        if txn.HasStarted() and not txn.HasEnded():
            txn.RollBack()
        if failed:
            return {
                "success": False,
                "placed": [],
                "failed": failed,
                "transaction_status": "rolled_back",
            }
        return _error_response(str(exc))


# ===================================================================
# 6.2.10  Create Panel (Phase + Reference Plane)
# ===================================================================

@api.route("/panels", methods=["POST"])
def create_panel(uiapp, request):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    data = request.data or {}

    name = data.get("name", "New Panel")
    create_ref_plane = data.get("create_reference_plane", True)

    # Find existing phase by name, or use last phase
    phase = None
    for p in doc.Phases:
        if p.Name.strip().lower() == name.strip().lower():
            phase = p
            break

    phase_created = False
    phase_id = None
    phase_name = name

    # Phase creation is not available via the Revit API (Revit 2024+).
    # If no matching phase exists, report it and still create the ref plane.
    if phase is not None:
        phase_id = eid_int(phase.Id)
        phase_name = phase.Name
    else:
        # Use the last existing phase as fallback
        phases_list = list(doc.Phases)
        if phases_list:
            phase = phases_list[-1]
            phase_id = eid_int(phase.Id)
            phase_name = phase.Name

    txn = DB.Transaction(doc, "MCP Create Panel")
    txn.Start()
    try:
        ref_plane_id = None
        ref_plane_name = None

        if create_ref_plane:
            origin = data.get("plane_origin", {})
            direction = data.get("plane_direction", {})
            plane_name = data.get("plane_name", name + " Plane")

            ox = float(origin.get("x", 0.0))
            oy = float(origin.get("y", 0.0))
            oz = float(origin.get("z", 0.0))
            dx = float(direction.get("x", 0.0))
            dy = float(direction.get("y", 1.0))
            dz = float(direction.get("z", 0.0))

            bubble_end = DB.XYZ(ox - 10, oy, oz)
            free_end = DB.XYZ(ox + 10, oy, oz)
            cut_vec = DB.XYZ(dx, dy, dz)

            ref_plane = doc.Create.NewReferencePlane(
                bubble_end, free_end, cut_vec, doc.ActiveView
            )
            ref_plane.Name = plane_name
            ref_plane_id = eid_int(ref_plane.Id)
            ref_plane_name = plane_name

        txn.Commit()

        result = {
            "success": True,
            "phase_id": phase_id,
            "phase_name": phase_name,
            "reference_plane_id": ref_plane_id,
            "reference_plane_name": ref_plane_name,
            "transaction_status": "committed",
        }
        if phase is not None and phase.Name.strip().lower() != name.strip().lower():
            result["warning"] = (
                "Phase '{}' does not exist and cannot be created via API. "
                "Using existing phase '{}' instead. "
                "Create the phase manually in Revit (Manage > Phases) if needed."
            ).format(name, phase.Name)
        return result

    except Exception as exc:
        if txn.HasStarted() and not txn.HasEnded():
            txn.RollBack()
        return _error_response(str(exc))


# ===================================================================
# 6.2.11  Generate Cut List
# ===================================================================

@api.route("/cutlist", methods=["POST"])
def generate_cutlist(uiapp, request):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    data = request.data or {}

    scope = data.get("scope", "all")
    group_by = data.get("group_by", "family")
    sort_by = data.get("sort_by", "length_desc")

    # Gather parts based on scope
    parts = []
    phase_map = {}  # element_id -> phase_name

    if scope == "panel":
        phase_id = data.get("phase_id")
        phase = _get_phase_by_id(doc, phase_id)
        if phase is None:
            return _error_response("Phase {} not found".format(phase_id))
        elements = _get_elements_in_phase(doc, phase)
        for e in elements:
            parts.append(e)
            phase_map[eid_int(e.Id)] = phase.Name

    elif scope == "panels":
        phase_ids = data.get("phase_ids", [])
        for pid in phase_ids:
            phase = _get_phase_by_id(doc, pid)
            if phase:
                for e in _get_elements_in_phase(doc, phase):
                    parts.append(e)
                    phase_map[eid_int(e.Id)] = phase.Name

    else:  # "all"
        for phase in doc.Phases:
            for e in _get_elements_in_phase(doc, phase):
                parts.append(e)
                phase_map[eid_int(e.Id)] = phase.Name

    # Build part data
    part_records = []
    for p in parts:
        elem_type = doc.GetElement(p.GetTypeId())
        family_name = ""
        if elem_type and hasattr(elem_type, "Family") and elem_type.Family:
            family_name = elem_type.Family.Name
        elif elem_type and hasattr(elem_type, "FamilyName"):
            family_name = elem_type.FamilyName

        length_param = p.LookupParameter("Length")
        length = get_parameter_value(length_param) if length_param else 0.0
        if length is None:
            length = 0.0

        width_param = p.LookupParameter("Width")
        width = get_parameter_value(width_param) if width_param else 0.0
        if width is None:
            width = 0.0

        part_records.append({
            "element_id": eid_int(p.Id),
            "family": family_name,
            "length": length,
            "width": width,
            "panel": phase_map.get(eid_int(p.Id), "Unknown"),
        })

    # Group
    groups = {}
    if group_by == "family":
        for r in part_records:
            key = r["family"]
            groups.setdefault(key, []).append(r)
    elif group_by == "thickness":
        for r in part_records:
            key = '{}\"'.format(r["width"])
            groups.setdefault(key, []).append(r)
    elif group_by == "family_and_panel":
        for r in part_records:
            key = "{} | {}".format(r["family"], r["panel"])
            groups.setdefault(key, []).append(r)

    # Build response
    response_groups = []
    for key, recs in sorted(groups.items()):
        # Aggregate by length
        length_agg = {}
        for r in recs:
            l = r["length"]
            if l not in length_agg:
                length_agg[l] = {"quantity": 0, "panels": set(), "element_ids": []}
            length_agg[l]["quantity"] += 1
            length_agg[l]["panels"].add(r["panel"])
            length_agg[l]["element_ids"].append(r["element_id"])

        # Sort
        if sort_by == "length_desc":
            sorted_lengths = sorted(length_agg.items(), key=lambda x: x[0], reverse=True)
        elif sort_by == "length_asc":
            sorted_lengths = sorted(length_agg.items(), key=lambda x: x[0])
        else:  # "panel"
            sorted_lengths = sorted(length_agg.items(), key=lambda x: x[0], reverse=True)

        parts_list = []
        for length_val, agg in sorted_lengths:
            entry = {
                "length": length_val,
                "quantity": agg["quantity"],
                "panels": sorted(agg["panels"]),
                "element_ids": agg["element_ids"],
            }
            if group_by != "family_and_panel":
                entry["family"] = recs[0]["family"]
            parts_list.append(entry)

        group_entry = {
            "group_key": key,
            "part_count": len(recs),
            "parts": parts_list,
        }
        if group_by == "thickness":
            group_entry["group_label"] = "{} inch parts".format(
                key.replace('"', "")
            )
        response_groups.append(group_entry)

    resp = {
        "scope": scope,
        "total_parts": len(part_records),
        "groups": response_groups,
    }
    if scope == "panel":
        phase = _get_phase_by_id(doc, data.get("phase_id"))
        if phase:
            resp["phase_id"] = eid_int(phase.Id)
            resp["phase_name"] = phase.Name
    return resp


# ===================================================================
# 6.2.12  List Reference Planes
# ===================================================================

@api.route("/planes", methods=["GET"])
def list_reference_planes(uiapp, request):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    collector = DB.FilteredElementCollector(doc).OfClass(DB.ReferencePlane)
    planes = []
    for rp in collector:
        if not rp.Name:
            continue
        origin = rp.GetPlane().Origin if rp.GetPlane() else None
        normal = rp.GetPlane().Normal if rp.GetPlane() else None
        planes.append({
            "element_id": eid_int(rp.Id),
            "name": rp.Name,
            "origin": serialize_xyz(origin),
            "direction": serialize_xyz(normal),
        })
    return {"planes": planes}


# ===================================================================
# 6.2.13  List Available Types
# ===================================================================

@api.route("/types/<category>", methods=["GET"])
def list_types(uiapp, request, category):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    bic = _resolve_category(category)
    if bic is None:
        return _error_response("Category '{}' not found".format(category))

    collector = (
        DB.FilteredElementCollector(doc)
        .OfCategory(bic)
        .WhereElementIsElementType()
    )
    types = []
    for et in collector:
        family_name = None
        if hasattr(et, "Family") and et.Family:
            family_name = et.Family.Name
        elif hasattr(et, "FamilyName"):
            family_name = et.FamilyName
        type_name = DB.Element.Name.__get__(et)
        types.append({
            "family": family_name,
            "type": type_name,
            "type_id": eid_int(et.Id),
        })
    return {"category": category, "types": types}


# ===================================================================
# 6.2.14  List Levels
# ===================================================================

@api.route("/levels", methods=["GET"])
def list_levels(uiapp, request):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    collector = DB.FilteredElementCollector(doc).OfClass(DB.Level)
    levels = []
    for lev in collector:
        levels.append({
            "name": lev.Name,
            "elevation_ft": round(lev.Elevation, 6),
            "element_id": eid_int(lev.Id),
        })
    levels.sort(key=lambda l: l["elevation_ft"])
    return {"levels": levels}


# ===================================================================
# 6.2.15  Get/Export Schedules
# ===================================================================

@api.route("/schedules", methods=["GET"])
def list_schedules(uiapp, request):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    collector = (
        DB.FilteredElementCollector(doc)
        .OfClass(DB.ViewSchedule)
        .WhereElementIsNotElementType()
    )
    schedules = []
    for vs in collector:
        if vs.IsTitleblockRevisionSchedule:
            continue
        schedules.append({
            "schedule_id": eid_int(vs.Id),
            "name": vs.Name,
        })
    return {"schedules": schedules}


@api.route("/schedules/export/<int:schedule_id>", methods=["POST", "GET"])
def export_schedule(uiapp, request, schedule_id):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    eid = DB.ElementId(Int64(schedule_id))
    elem = doc.GetElement(eid)
    if elem is None or not isinstance(elem, DB.ViewSchedule):
        return _error_response("Schedule {} not found".format(schedule_id))

    table = elem.GetTableData()
    section = table.GetSectionData(DB.SectionType.Body)
    header_section = table.GetSectionData(DB.SectionType.Header)

    rows_count = section.NumberOfRows
    cols_count = section.NumberOfColumns

    # Headers from the first body row or header section
    headers = []
    for col in range(cols_count):
        try:
            val = elem.GetCellText(DB.SectionType.Header, 0, col)
        except Exception:
            val = "Column {}".format(col)
        headers.append(val)

    rows = []
    for row in range(rows_count):
        row_data = []
        for col in range(cols_count):
            try:
                val = elem.GetCellText(DB.SectionType.Body, row, col)
            except Exception:
                val = ""
            row_data.append(val)
        rows.append(row_data)

    return {
        "schedule_name": elem.Name,
        "headers": headers,
        "rows": rows,
    }


# ===================================================================
# 6.2.16  Create View
# ===================================================================

@api.route("/create/view", methods=["POST"])
def create_view(uiapp, request):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    data = request.data or {}

    view_type = data.get("view_type", "floor_plan")
    level_name = data.get("level", "")
    view_name = data.get("name", "New View")
    scale = data.get("scale", 48)
    phase_id = data.get("phase_id", None)

    txn = DB.Transaction(doc, "MCP Create View")
    txn.Start()
    try:
        # Find the level
        level = None
        if level_name:
            for lev in DB.FilteredElementCollector(doc).OfClass(DB.Level):
                if lev.Name.lower() == level_name.strip().lower():
                    level = lev
                    break

        # Find view family type
        view = None
        if view_type == "floor_plan":
            vft = _get_view_family_type(doc, DB.ViewFamily.FloorPlan)
            if level and vft:
                view = DB.ViewPlan.Create(doc, vft.Id, level.Id)
        elif view_type == "ceiling_plan":
            vft = _get_view_family_type(doc, DB.ViewFamily.CeilingPlan)
            if level and vft:
                view = DB.ViewPlan.Create(doc, vft.Id, level.Id)
        elif view_type == "3d":
            vft = _get_view_family_type(doc, DB.ViewFamily.ThreeDimensional)
            if vft:
                view = DB.View3D.CreateIsometric(doc, vft.Id)
        elif view_type == "section":
            vft = _get_view_family_type(doc, DB.ViewFamily.Section)
            if vft:
                bb = DB.BoundingBoxXYZ()
                bb.Min = DB.XYZ(-10, -10, -10)
                bb.Max = DB.XYZ(10, 10, 10)
                view = DB.ViewSection.CreateSection(doc, vft.Id, bb)
        elif view_type == "elevation":
            vft = _get_view_family_type(doc, DB.ViewFamily.Elevation)
            if vft:
                # Create an elevation marker and get the view
                marker = DB.ElevationMarker.CreateElevationMarker(
                    doc, vft.Id, DB.XYZ.Zero, scale
                )
                view = marker.CreateElevation(doc, doc.ActiveView.Id, 0)

        if view is None:
            txn.RollBack()
            return _error_response(
                "Could not create view of type '{}'".format(view_type)
            )

        view.Name = view_name
        view.Scale = scale

        # Set phase filter if requested
        if phase_id is not None:
            phase_param = view.get_Parameter(DB.BuiltInParameter.VIEW_PHASE)
            if phase_param and not phase_param.IsReadOnly:
                phase_param.Set(DB.ElementId(Int64(phase_id)))

        txn.Commit()

        return {
            "success": True,
            "view_id": eid_int(view.Id),
            "name": view.Name,
            "transaction_status": "committed",
        }

    except Exception as exc:
        if txn.HasStarted() and not txn.HasEnded():
            txn.RollBack()
        return _error_response(str(exc))


def _get_view_family_type(doc, view_family):
    """Find a ViewFamilyType for the given ViewFamily enum."""
    collector = DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType)
    for vft in collector:
        if vft.ViewFamily == view_family:
            return vft
    return None


# ===================================================================
# 6.2.17  Delete Elements
# ===================================================================

@api.route("/elements", methods=["DELETE", "POST"])
def delete_elements(uiapp, request):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    data = request.data or {}
    element_ids = data.get("element_ids", [])

    if not element_ids:
        return _error_response("No element_ids provided")

    txn = DB.Transaction(doc, "MCP Delete Elements")
    txn.Start()
    deleted = []
    failed = []
    try:
        for eid_val in element_ids:
            eid = DB.ElementId(Int64(eid_val))
            elem = doc.GetElement(eid)
            if elem is None:
                failed.append({"element_id": eid_val, "error": "Not found"})
                continue
            try:
                doc.Delete(eid)
                deleted.append(eid_val)
            except Exception as exc:
                failed.append({"element_id": eid_val, "error": str(exc)})

        txn.Commit()
        return {
            "success": True,
            "deleted": deleted,
            "failed": failed,
            "transaction_status": "committed",
        }
    except Exception as exc:
        if txn.HasStarted() and not txn.HasEnded():
            txn.RollBack()
        return _error_response(str(exc))


# ===================================================================
# 6.2.18  Get Material Quantities
# ===================================================================

@api.route("/materials/<int:element_id>", methods=["GET"])
def get_material_quantities(uiapp, request, element_id):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    eid = DB.ElementId(Int64(element_id))
    elem = doc.GetElement(eid)
    if elem is None:
        return _error_response("Element {} not found".format(element_id))

    materials = []
    mat_ids = elem.GetMaterialIds(False)
    for mid in mat_ids:
        mat = doc.GetElement(mid)
        if mat is None:
            continue
        area = elem.GetMaterialArea(mid, False)
        volume = elem.GetMaterialVolume(mid)
        materials.append({
            "name": mat.Name,
            "area_sqft": round(area, 2),
            "volume_cuft": round(volume, 2),
        })

    return {
        "element_id": element_id,
        "materials": materials,
    }


# ===================================================================
# 6.3.1  Model Summary (Discovery)
# ===================================================================

@api.route("/discovery/summary", methods=["GET"])
def discovery_summary(uiapp, request):
    doc, err = _require_doc(uiapp)
    if err:
        return err

    # Categories with counts — single pass
    cat_counts = {}
    for elem in DB.FilteredElementCollector(doc).WhereElementIsNotElementType():
        cat = elem.Category
        if cat and cat.Name:
            cat_counts[cat.Name] = cat_counts.get(cat.Name, 0) + 1
    categories = [
        {"name": n, "count": c}
        for n, c in sorted(cat_counts.items())
    ]

    # Phases as panels
    phases = []
    for phase in doc.Phases:
        parts = _get_elements_in_phase(doc, phase)
        phases.append({
            "name": phase.Name,
            "element_id": eid_int(phase.Id),
            "part_count": len(parts),
        })

    # Levels
    levels = []
    for lev in DB.FilteredElementCollector(doc).OfClass(DB.Level):
        levels.append({
            "name": lev.Name,
            "elevation_ft": round(lev.Elevation, 6),
        })

    # Assemblies
    assemblies = []
    for asm in DB.FilteredElementCollector(doc).OfCategory(
        DB.BuiltInCategory.OST_Assemblies
    ).WhereElementIsNotElementType():
        assemblies.append({
            "name": asm.Name if hasattr(asm, "Name") else str(eid_int(asm.Id)),
            "element_id": eid_int(asm.Id),
        })

    # Groups
    groups = []
    for grp in DB.FilteredElementCollector(doc).OfClass(DB.Group):
        groups.append({
            "name": grp.Name,
            "element_id": eid_int(grp.Id),
        })

    # Counts
    views_count = DB.FilteredElementCollector(doc).OfClass(
        DB.View
    ).WhereElementIsNotElementType().GetElementCount()
    sheets_count = DB.FilteredElementCollector(doc).OfClass(
        DB.ViewSheet
    ).GetElementCount()
    schedules_count = DB.FilteredElementCollector(doc).OfClass(
        DB.ViewSchedule
    ).GetElementCount()

    # Warnings
    warnings = doc.GetWarnings() if hasattr(doc, "GetWarnings") else []
    warnings_count = len(list(warnings))

    return {
        "doc_title": doc.Title,
        "file_path": doc.PathName,
        "categories": categories,
        "phases_as_panels": phases,
        "levels": levels,
        "assemblies": assemblies,
        "groups": groups,
        "views_count": views_count,
        "sheets_count": sheets_count,
        "schedules_count": schedules_count,
        "warnings_count": warnings_count,
    }


# ===================================================================
# 6.3.2  Spatial Query (Discovery)
# ===================================================================

@api.route("/discovery/spatial", methods=["POST"])
def discovery_spatial(uiapp, request):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    data = request.data or {}

    mode = data.get("mode", "bounding_box")
    filter_cats = data.get("categories", None)

    elements = []

    if mode == "bounding_box":
        min_pt = data.get("min", {})
        max_pt = data.get("max", {})
        outline = DB.Outline(
            DB.XYZ(float(min_pt.get("x", 0)), float(min_pt.get("y", 0)), float(min_pt.get("z", 0))),
            DB.XYZ(float(max_pt.get("x", 0)), float(max_pt.get("y", 0)), float(max_pt.get("z", 0))),
        )
        bb_filter = DB.BoundingBoxIntersectsFilter(outline)
        collector = (
            DB.FilteredElementCollector(doc)
            .WhereElementIsNotElementType()
            .WherePasses(bb_filter)
        )
        for elem in collector:
            if filter_cats:
                if not elem.Category or elem.Category.Name.lower() not in [
                    c.lower() for c in filter_cats
                ]:
                    continue
            summary = serialize_element_summary(elem)
            if summary:
                bb = elem.get_BoundingBox(None)
                summary["bounding_box"] = serialize_bounding_box(bb)
                elements.append(summary)

    elif mode == "proximity":
        center = data.get("center", {})
        cx = float(center.get("x", 0))
        cy = float(center.get("y", 0))
        cz = float(center.get("z", 0))
        radius = float(data.get("radius_ft", 5.0))
        center_pt = DB.XYZ(cx, cy, cz)

        # Use a bounding box to pre-filter, then distance check
        outline = DB.Outline(
            DB.XYZ(cx - radius, cy - radius, cz - radius),
            DB.XYZ(cx + radius, cy + radius, cz + radius),
        )
        bb_filter = DB.BoundingBoxIntersectsFilter(outline)
        collector = (
            DB.FilteredElementCollector(doc)
            .WhereElementIsNotElementType()
            .WherePasses(bb_filter)
        )
        for elem in collector:
            if filter_cats:
                if not elem.Category or elem.Category.Name.lower() not in [
                    c.lower() for c in filter_cats
                ]:
                    continue
            loc = elem.Location
            if loc is None:
                continue
            pt = None
            if isinstance(loc, DB.LocationPoint):
                pt = loc.Point
            elif isinstance(loc, DB.LocationCurve):
                pt = loc.Curve.Evaluate(0.5, True)
            if pt and pt.DistanceTo(center_pt) <= radius:
                summary = serialize_element_summary(elem)
                if summary:
                    bb = elem.get_BoundingBox(None)
                    summary["bounding_box"] = serialize_bounding_box(bb)
                    elements.append(summary)

    return {
        "mode": mode,
        "count": len(elements),
        "elements": elements,
    }


# ===================================================================
# 6.3.3  Element Connections (Discovery)
# ===================================================================

@api.route("/discovery/connections/<int:element_id>", methods=["GET"])
def discovery_connections(uiapp, request, element_id):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    eid = DB.ElementId(Int64(element_id))
    elem = doc.GetElement(eid)
    if elem is None:
        return _error_response("Element {} not found".format(element_id))

    connections = {
        "joined_to": [],
        "hosted_elements": [],
        "host": None,
        "touching": [],
        "cut_by": [],
        "assembly": None,
    }

    # Joined elements (for walls)
    if isinstance(elem, DB.Wall):
        try:
            joined_ids = DB.JoinGeometryUtils.GetJoinedElements(doc, elem)
            for jid in joined_ids:
                je = doc.GetElement(jid)
                if je:
                    connections["joined_to"].append({
                        "element_id": eid_int(jid),
                        "category": je.Category.Name if je.Category else None,
                        "type": DB.Element.Name.__get__(
                            doc.GetElement(je.GetTypeId())
                        ) if je.GetTypeId() != DB.ElementId.InvalidElementId else None,
                        "join_type": "butt",
                    })
        except Exception:
            pass

    # Hosted elements
    try:
        dep_ids = elem.GetDependentElements(None)
        for did in dep_ids:
            de = doc.GetElement(did)
            if de and hasattr(de, "Host") and de.Host and de.Host.Id == elem.Id:
                connections["hosted_elements"].append({
                    "element_id": eid_int(did),
                    "category": de.Category.Name if de.Category else None,
                    "family": _get_family_name(doc, de),
                    "type": _get_type_name(doc, de),
                })
    except Exception:
        pass

    # Host
    if hasattr(elem, "Host") and elem.Host:
        host = elem.Host
        connections["host"] = {
            "element_id": eid_int(host.Id),
            "category": host.Category.Name if host.Category else None,
            "type": _get_type_name(doc, host),
        }

    # Assembly
    if elem.AssemblyInstanceId and elem.AssemblyInstanceId != DB.ElementId.InvalidElementId:
        asm = doc.GetElement(elem.AssemblyInstanceId)
        if asm:
            connections["assembly"] = {
                "name": asm.Name if hasattr(asm, "Name") else str(eid_int(asm.Id)),
                "element_id": eid_int(asm.Id),
            }

    return {
        "element_id": element_id,
        "category": elem.Category.Name if elem.Category else None,
        "connections": connections,
    }


def _get_family_name(doc, elem):
    et = doc.GetElement(elem.GetTypeId())
    if et and hasattr(et, "Family") and et.Family:
        return et.Family.Name
    if et and hasattr(et, "FamilyName"):
        return et.FamilyName
    return None


def _get_type_name(doc, elem):
    et = doc.GetElement(elem.GetTypeId())
    if et:
        return DB.Element.Name.__get__(et)
    return None


# ===================================================================
# 6.3.4  Model BOM (Discovery)
# ===================================================================

@api.route("/discovery/bom", methods=["POST"])
def discovery_bom(uiapp, request):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    data = request.data or {}

    scope = data.get("scope", "all")
    group_by = data.get("group_by", "category_and_type")
    include_materials = data.get("include_materials", True)

    # Gather elements
    elements = []
    if scope == "assembly":
        asm_id = data.get("assembly_id")
        asm = doc.GetElement(DB.ElementId(Int64(asm_id)))
        if asm and hasattr(asm, "GetMemberIds"):
            for mid in asm.GetMemberIds():
                me = doc.GetElement(mid)
                if me:
                    elements.append(me)
    elif scope == "elements":
        for eid_val in data.get("element_ids", []):
            e = doc.GetElement(DB.ElementId(Int64(eid_val)))
            if e:
                elements.append(e)
    else:  # "all"
        collector = (
            DB.FilteredElementCollector(doc)
            .WhereElementIsNotElementType()
        )
        for e in collector:
            if e.Category and e.Category.CategoryType == DB.CategoryType.Model:
                elements.append(e)

    # Build groups
    groups = {}
    all_materials = {}

    for elem in elements:
        cat_name = elem.Category.Name if elem.Category else "Unknown"
        type_name = _get_type_name(doc, elem)
        family_name = _get_family_name(doc, elem) or cat_name

        if group_by == "category_and_type":
            key = (cat_name, type_name or "Unknown")
        elif group_by == "family":
            key = (family_name,)
        else:  # "material" - handled separately
            key = (cat_name, type_name or "Unknown")

        if key not in groups:
            groups[key] = {"elements": [], "count": 0}
        groups[key]["elements"].append(elem)
        groups[key]["count"] += 1

        # Material tracking
        if include_materials:
            try:
                mat_ids = elem.GetMaterialIds(False)
                for mid in mat_ids:
                    mat = doc.GetElement(mid)
                    if mat:
                        mat_name = mat.Name
                        if mat_name not in all_materials:
                            all_materials[mat_name] = {
                                "total_area_sqft": 0.0,
                                "total_volume_cuft": 0.0,
                                "used_in": {},
                            }
                        area = elem.GetMaterialArea(mid, False)
                        volume = elem.GetMaterialVolume(mid)
                        all_materials[mat_name]["total_area_sqft"] += area
                        all_materials[mat_name]["total_volume_cuft"] += volume
                        usage_key = "{} - {}".format(cat_name, type_name)
                        if usage_key not in all_materials[mat_name]["used_in"]:
                            all_materials[mat_name]["used_in"][usage_key] = 0
                        all_materials[mat_name]["used_in"][usage_key] += 1
            except Exception:
                pass

    # Format response
    if group_by == "material":
        materials_list = []
        for mat_name, mat_data in sorted(all_materials.items()):
            used_in = []
            for usage_key, count in mat_data["used_in"].items():
                parts = usage_key.split(" - ", 1)
                used_in.append({
                    "category": parts[0],
                    "type": parts[1] if len(parts) > 1 else None,
                    "count": count,
                })
            materials_list.append({
                "name": mat_name,
                "total_area_sqft": round(mat_data["total_area_sqft"], 2),
                "total_volume_cuft": round(mat_data["total_volume_cuft"], 2),
                "used_in": used_in,
            })
        return {
            "scope": scope,
            "group_by": group_by,
            "materials": materials_list,
        }

    response_groups = []
    for key, grp in sorted(groups.items()):
        entry = {"count": grp["count"]}
        if group_by == "category_and_type":
            entry["category"] = key[0]
            entry["type"] = key[1]
        elif group_by == "family":
            entry["family"] = key[0]

        if include_materials:
            mats = {}
            for elem in grp["elements"]:
                try:
                    for mid in elem.GetMaterialIds(False):
                        mat = doc.GetElement(mid)
                        if mat:
                            if mat.Name not in mats:
                                mats[mat.Name] = {"area": 0.0, "volume": 0.0}
                            mats[mat.Name]["area"] += elem.GetMaterialArea(mid, False)
                            mats[mat.Name]["volume"] += elem.GetMaterialVolume(mid)
                except Exception:
                    pass
            entry["materials"] = [
                {
                    "name": n,
                    "total_area_sqft": round(d["area"], 2),
                    "total_volume_cuft": round(d["volume"], 2),
                }
                for n, d in sorted(mats.items())
            ]

        response_groups.append(entry)

    resp = {
        "scope": scope,
        "group_by": group_by,
        "groups": response_groups,
    }
    if include_materials:
        resp["material_totals"] = [
            {
                "name": n,
                "total_area_sqft": round(d["total_area_sqft"], 2),
                "total_volume_cuft": round(d["total_volume_cuft"], 2),
            }
            for n, d in sorted(all_materials.items())
        ]
    return resp


# ===================================================================
# 6.3.5  Parameter Schema (Discovery)
# ===================================================================

@api.route("/discovery/parameters", methods=["GET"])
def discovery_parameters(uiapp, request):
    doc, err = _require_doc(uiapp)
    if err:
        return err

    shared_params = []
    project_params = []
    builtin_usage = {}

    # Shared parameters from BindingMap
    binding_map = doc.ParameterBindings
    iterator = binding_map.ForwardIterator()
    while iterator.MoveNext():
        defn = iterator.Key
        binding = iterator.Current
        cats = []
        if hasattr(binding, "Categories"):
            for cat in binding.Categories:
                cats.append(cat.Name)

        is_instance = isinstance(binding, DB.InstanceBinding)

        # Sample values
        samples = set()
        if cats:
            try:
                first_cat = binding.Categories.get_Item(0) if binding.Categories.Size > 0 else None
                if first_cat:
                    bic = first_cat.BuiltInCategory if hasattr(first_cat, "BuiltInCategory") else None
                    if bic:
                        for elem in DB.FilteredElementCollector(doc).OfCategory(bic).WhereElementIsNotElementType():
                            param = elem.LookupParameter(defn.Name)
                            if param and param.HasValue:
                                val = get_parameter_value(param)
                                if val is not None:
                                    samples.add(str(val))
                            if len(samples) >= 10:
                                break
            except Exception:
                pass

        entry = {
            "name": defn.Name,
            "group": str(defn.ParameterGroup) if hasattr(defn, "ParameterGroup") else None,
            "type": str(defn.ParameterType) if hasattr(defn, "ParameterType") else None,
            "categories": sorted(cats),
            "is_instance": is_instance,
            "sample_values": sorted(samples)[:10],
        }
        if hasattr(defn, "GUID"):
            entry["guid"] = str(defn.GUID)
            shared_params.append(entry)
        else:
            project_params.append(entry)

    # Built-in parameter usage for common params
    common_builtins = ["Mark", "Comments"]
    for bp_name in common_builtins:
        cats_using = set()
        samples = set()
        for elem in DB.FilteredElementCollector(doc).WhereElementIsNotElementType():
            param = elem.LookupParameter(bp_name)
            if param and param.HasValue:
                if elem.Category:
                    cats_using.add(elem.Category.Name)
                val = get_parameter_value(param)
                if val:
                    samples.add(str(val))
            if len(samples) >= 10 and len(cats_using) >= 5:
                break
        if cats_using:
            builtin_usage[bp_name] = {
                "categories_using": sorted(cats_using),
                "sample_values": sorted(samples)[:10],
            }

    return {
        "shared_parameters": shared_params,
        "project_parameters": project_params,
        "builtin_parameter_usage": builtin_usage,
    }


# ===================================================================
# 6.3.6  Family Info (Discovery)
# ===================================================================

@api.route("/discovery/families", methods=["GET", "POST"])
def discovery_families(uiapp, request):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    data = request.data or {}
    filter_category = data.get("category", None)

    families_dict = {}

    collector = DB.FilteredElementCollector(doc).OfClass(DB.FamilySymbol)
    for fs in collector:
        fam = fs.Family
        if fam is None:
            continue

        cat = fam.FamilyCategory
        if cat is None:
            continue
        cat_name = cat.Name

        if filter_category and cat_name.lower() != filter_category.strip().lower():
            continue

        fam_name = fam.Name
        if fam_name not in families_dict:
            # Determine hosting behavior
            hosting = None
            if hasattr(fam, "FamilyPlacementType"):
                fpt = str(fam.FamilyPlacementType)
                if "Wall" in fpt:
                    hosting = "wall"
                elif "Floor" in fpt:
                    hosting = "floor"
                elif "Ceiling" in fpt:
                    hosting = "ceiling"
                elif "Face" in fpt:
                    hosting = "face"
                else:
                    hosting = "standalone"

            families_dict[fam_name] = {
                "name": fam_name,
                "category": cat_name,
                "is_system_family": False,
                "hosting_behavior": hosting,
                "types": [],
                "instance_parameters": [],
                "placement_count": 0,
            }

        # Add type
        type_name = DB.Element.Name.__get__(fs)
        type_params = {}
        for param in fs.Parameters:
            if not param.IsReadOnly:
                type_params[param.Definition.Name] = get_parameter_value(param)

        families_dict[fam_name]["types"].append({
            "name": type_name,
            "type_id": eid_int(fs.Id),
            "type_parameters": type_params,
        })

    # Count placements and collect instance parameters
    for elem in DB.FilteredElementCollector(doc).OfClass(
        DB.FamilyInstance
    ).WhereElementIsNotElementType():
        fam = elem.Symbol.Family if elem.Symbol else None
        if fam and fam.Name in families_dict:
            families_dict[fam.Name]["placement_count"] += 1
            if not families_dict[fam.Name]["instance_parameters"]:
                inst_params = []
                for param in elem.Parameters:
                    inst_params.append(param.Definition.Name)
                families_dict[fam.Name]["instance_parameters"] = sorted(set(inst_params))

    # Also include system families
    for cat_name in ["Walls", "Floors", "Roofs", "Ceilings"]:
        bic = _resolve_category(cat_name)
        if bic is None:
            continue
        types_collector = (
            DB.FilteredElementCollector(doc)
            .OfCategory(bic)
            .WhereElementIsElementType()
        )
        for et in types_collector:
            tn = DB.Element.Name.__get__(et)
            sys_fam_name = cat_name
            if sys_fam_name not in families_dict:
                families_dict[sys_fam_name] = {
                    "name": sys_fam_name,
                    "category": cat_name,
                    "is_system_family": True,
                    "hosting_behavior": None,
                    "types": [],
                    "instance_parameters": [],
                    "placement_count": 0,
                }
            type_params = {}
            for param in et.Parameters:
                if not param.IsReadOnly:
                    type_params[param.Definition.Name] = get_parameter_value(param)
            families_dict[sys_fam_name]["types"].append({
                "name": tn,
                "type_id": eid_int(et.Id),
                "type_parameters": type_params,
            })

        # Count instances
        inst_collector = (
            DB.FilteredElementCollector(doc)
            .OfCategory(bic)
            .WhereElementIsNotElementType()
        )
        count = inst_collector.GetElementCount()
        if sys_fam_name in families_dict:
            families_dict[sys_fam_name]["placement_count"] = count

        # Instance parameters from first instance
        if count > 0 and not families_dict.get(sys_fam_name, {}).get("instance_parameters"):
            first = inst_collector.FirstElement()
            if first:
                families_dict[sys_fam_name]["instance_parameters"] = sorted(
                    set(p.Definition.Name for p in first.Parameters)
                )

    return {"families": sorted(families_dict.values(), key=lambda f: f["name"])}


# ===================================================================
# 6.3.7  Assembly Detail (Discovery)
# ===================================================================

@api.route("/discovery/assembly/<int:assembly_id>", methods=["GET"])
def discovery_assembly(uiapp, request, assembly_id):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    eid = DB.ElementId(Int64(assembly_id))
    asm = doc.GetElement(eid)
    if asm is None:
        return _error_response("Assembly {} not found".format(assembly_id))

    members = []
    if hasattr(asm, "GetMemberIds"):
        for mid in asm.GetMemberIds():
            me = doc.GetElement(mid)
            if me is None:
                continue
            member_data = {
                "element_id": eid_int(mid),
                "category": me.Category.Name if me.Category else None,
                "type": _get_type_name(doc, me),
                "role": "member",
                "location": serialize_location(me),
                "parameters": get_parameters_dict(me, ["Mark", "Comments"]),
            }
            members.append(member_data)

    # Assembly views
    asm_views = []
    for v in DB.FilteredElementCollector(doc).OfClass(DB.View).WhereElementIsNotElementType():
        try:
            if hasattr(v, "AssociatedAssemblyInstanceId"):
                if v.AssociatedAssemblyInstanceId == eid:
                    asm_views.append({
                        "view_id": eid_int(v.Id),
                        "name": v.Name,
                        "view_type": str(v.ViewType),
                    })
        except Exception:
            pass

    # Assembly sheets
    asm_sheets = []
    for s in DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet):
        try:
            if hasattr(s, "AssociatedAssemblyInstanceId"):
                if s.AssociatedAssemblyInstanceId == eid:
                    asm_sheets.append({
                        "sheet_id": eid_int(s.Id),
                        "sheet_number": s.SheetNumber,
                        "sheet_name": s.Name,
                    })
        except Exception:
            pass

    naming_cat = None
    if hasattr(asm, "NamingCategory") and asm.NamingCategory:
        naming_cat = asm.NamingCategory.Name

    return {
        "assembly_id": assembly_id,
        "name": asm.Name if hasattr(asm, "Name") else str(assembly_id),
        "naming_category": naming_cat,
        "members": members,
        "assembly_views": asm_views,
        "assembly_sheets": asm_sheets,
    }


# ===================================================================
# 6.3.8  View Contents (Discovery)
# ===================================================================

@api.route("/discovery/view/<int:view_id>", methods=["GET"])
def discovery_view(uiapp, request, view_id):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    eid = DB.ElementId(Int64(view_id))
    view = doc.GetElement(eid)
    if view is None or not isinstance(view, DB.View):
        return _error_response("View {} not found".format(view_id))

    # View properties
    level_name = None
    if hasattr(view, "GenLevel") and view.GenLevel:
        level_name = view.GenLevel.Name

    crop_box = None
    try:
        if view.CropBoxActive:
            crop_box = serialize_bounding_box(view.CropBox)
    except Exception:
        pass

    # Visible elements
    collector = DB.FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType()
    by_category = {}
    annotations = {"dimensions": 0, "text_notes": 0, "tags": 0}

    for elem in collector:
        if elem.Category is None:
            continue
        cat_name = elem.Category.Name

        # Count annotations separately
        if isinstance(elem, DB.Dimension):
            annotations["dimensions"] += 1
            continue
        if isinstance(elem, DB.TextNote):
            annotations["text_notes"] += 1
            continue
        if isinstance(elem, DB.IndependentTag):
            annotations["tags"] += 1
            continue

        if cat_name not in by_category:
            by_category[cat_name] = []
        by_category[cat_name].append(eid_int(elem.Id))

    visible_count = sum(len(ids) for ids in by_category.values())

    # Sheets this view is on
    on_sheets = []
    for sheet in DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet):
        placed_views = sheet.GetAllPlacedViews()
        if view.Id in placed_views:
            on_sheets.append({
                "sheet_id": eid_int(sheet.Id),
                "sheet_number": sheet.SheetNumber,
                "sheet_name": sheet.Name,
            })

    return {
        "view_id": view_id,
        "name": view.Name,
        "view_type": str(view.ViewType),
        "level": level_name,
        "scale": view.Scale,
        "crop_box": crop_box,
        "visible_categories": sorted(by_category.keys()),
        "visible_element_count": visible_count,
        "visible_elements_by_category": by_category,
        "annotations": annotations,
        "on_sheets": on_sheets,
    }


# ===================================================================
# 6.3.9  Sheet Index (Discovery)
# ===================================================================

@api.route("/discovery/sheets", methods=["GET"])
def discovery_sheets(uiapp, request):
    doc, err = _require_doc(uiapp)
    if err:
        return err
    sheets = []
    for sheet in DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet):
        views_on = []
        for vid in sheet.GetAllPlacedViews():
            v = doc.GetElement(vid)
            if v:
                views_on.append({
                    "view_id": eid_int(vid),
                    "name": v.Name,
                    "view_type": str(v.ViewType),
                })

        # Title block
        tb_name = None
        for tb_id in sheet.GetDependentElements(
            DB.ElementClassFilter(DB.FamilyInstance)
        ):
            tb = doc.GetElement(tb_id)
            if tb and tb.Category and eid_int(tb.Category.Id) == int(
                DB.BuiltInCategory.OST_TitleBlocks
            ):
                tb_type = doc.GetElement(tb.GetTypeId())
                if tb_type:
                    tb_name = DB.Element.Name.__get__(tb_type)
                break

        # Revision
        revision = None
        rev_param = sheet.LookupParameter("Current Revision")
        if rev_param and rev_param.HasValue:
            revision = rev_param.AsString()

        sheets.append({
            "sheet_id": eid_int(sheet.Id),
            "sheet_number": sheet.SheetNumber,
            "sheet_name": sheet.Name,
            "title_block": tb_name,
            "views_on_sheet": views_on,
            "revision": revision,
        })

    return {"sheets": sheets}


# ===================================================================
# 6.3.10  Warnings (Discovery)
# ===================================================================

@api.route("/discovery/warnings", methods=["GET"])
def discovery_warnings(uiapp, request):
    doc, err = _require_doc(uiapp)
    if err:
        return err

    warnings = []
    if hasattr(doc, "GetWarnings"):
        for w in doc.GetWarnings():
            elem_ids = [eid_int(eid) for eid in w.GetFailingElements()]
            add_ids = [eid_int(eid) for eid in w.GetAdditionalElements()]
            warnings.append({
                "description": w.GetDescriptionText(),
                "severity": str(w.GetSeverity()),
                "element_ids": elem_ids,
                "additional_element_ids": add_ids,
            })

    return {
        "count": len(warnings),
        "warnings": warnings,
    }
