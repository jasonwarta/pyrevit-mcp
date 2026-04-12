# -*- coding: utf-8 -*-
"""Revit object to JSON-safe representation converters.

This module converts common Revit API types into JSON-serializable Python
objects.  It runs inside Revit's process (IronPython / CPython via pyRevit)
and therefore has access to the Revit API namespaces.

Conversion table (Section 6.7 of spec):
    ElementId      -> int
    XYZ            -> {"x": float, "y": float, "z": float}
    Line           -> {"start": XYZ, "end": XYZ}
    Parameter      -> value (str | int | float | int(ElementId))
    Element        -> ElementSummary dict
    BoundingBoxXYZ -> {"min": XYZ, "max": XYZ}
    Transform      -> {"origin": XYZ, "basis_x": XYZ, "basis_y": XYZ, "basis_z": XYZ}
"""

import json
import math

from pyrevit.api import DB


# ---------------------------------------------------------------------------
# ElementId compatibility (Revit 2024+ uses .Value, older uses .IntegerValue)
# ---------------------------------------------------------------------------

def eid_int(eid):
    """Extract the integer value from an ElementId, compatible with all Revit versions."""
    if hasattr(eid, "Value"):
        return int(eid.Value)  # Revit 2024+ (.Value returns Int64, cast to Python int)
    return int(eid.IntegerValue)  # Revit 2022-2023


# ---------------------------------------------------------------------------
# Primitive converters
# ---------------------------------------------------------------------------

def serialize_xyz(xyz):
    """Convert an XYZ point to a JSON-safe dict."""
    if xyz is None:
        return None
    return {
        "x": round(xyz.X, 6),
        "y": round(xyz.Y, 6),
        "z": round(xyz.Z, 6),
    }


def serialize_element_id(eid):
    """Convert an ElementId to an integer."""
    if eid is None or eid == DB.ElementId.InvalidElementId:
        return None
    return eid_int(eid)


def serialize_line(line):
    """Convert a Revit Line to start/end dict."""
    if line is None:
        return None
    return {
        "start": serialize_xyz(line.GetEndPoint(0)),
        "end": serialize_xyz(line.GetEndPoint(1)),
    }


def serialize_bounding_box(bb):
    """Convert a BoundingBoxXYZ to min/max dict."""
    if bb is None:
        return None
    return {
        "min": serialize_xyz(bb.Min),
        "max": serialize_xyz(bb.Max),
    }


def serialize_transform(xform):
    """Convert a Transform to origin + basis vectors dict."""
    if xform is None:
        return None
    return {
        "origin": serialize_xyz(xform.Origin),
        "basis_x": serialize_xyz(xform.BasisX),
        "basis_y": serialize_xyz(xform.BasisY),
        "basis_z": serialize_xyz(xform.BasisZ),
    }


# ---------------------------------------------------------------------------
# Parameter value extraction
# ---------------------------------------------------------------------------

def get_parameter_value(param):
    """Extract a JSON-safe value from a Revit Parameter."""
    if param is None or not param.HasValue:
        return None

    st = param.StorageType
    if st == DB.StorageType.String:
        return param.AsString()
    elif st == DB.StorageType.Integer:
        return param.AsInteger()
    elif st == DB.StorageType.Double:
        return round(param.AsDouble(), 6)
    elif st == DB.StorageType.ElementId:
        return serialize_element_id(param.AsElementId())
    else:
        return param.AsValueString()


def get_parameters_dict(element, parameter_names=None):
    """Return a dict of parameter name -> value for an element.

    If *parameter_names* is ``None`` all parameters are returned.
    If it is an explicit list, only those parameters are included.
    """
    result = {}
    if parameter_names is not None:
        for name in parameter_names:
            param = element.LookupParameter(name)
            if param is None:
                # Case-insensitive fallback
                for p in element.Parameters:
                    if p.Definition.Name.strip().lower() == name.strip().lower():
                        param = p
                        break
            if param is not None:
                result[param.Definition.Name] = get_parameter_value(param)
    else:
        for param in element.Parameters:
            result[param.Definition.Name] = get_parameter_value(param)
    return result


# ---------------------------------------------------------------------------
# Location helpers
# ---------------------------------------------------------------------------

def serialize_location(element):
    """Return a simplified location dict for an element."""
    loc = element.Location
    if loc is None:
        return None
    if isinstance(loc, DB.LocationPoint):
        return {
            "type": "point",
            "x": round(loc.Point.X, 6),
            "y": round(loc.Point.Y, 6),
            "z": round(loc.Point.Z, 6),
        }
    if isinstance(loc, DB.LocationCurve):
        curve = loc.Curve
        return {
            "type": "curve",
            "start": serialize_xyz(curve.GetEndPoint(0)),
            "end": serialize_xyz(curve.GetEndPoint(1)),
        }
    return None


# ---------------------------------------------------------------------------
# Element summary
# ---------------------------------------------------------------------------

def serialize_element_summary(element, parameter_names=None):
    """Convert a Revit Element to an ElementSummary dict (Section 4.1.3)."""
    if element is None:
        return None

    cat = element.Category
    category_name = cat.Name if cat else None

    # Family and type
    family_name = None
    type_name = None
    elem_type = element.Document.GetElement(element.GetTypeId())
    if elem_type is not None:
        type_name = getattr(elem_type, "Name", None) or DB.Element.Name.__get__(elem_type)
        # For non-system families, get the family name
        if hasattr(elem_type, "Family") and elem_type.Family is not None:
            family_name = elem_type.Family.Name
        elif hasattr(elem_type, "FamilyName"):
            family_name = elem_type.FamilyName

    name = None
    try:
        name = element.Name
    except Exception:
        mark_param = element.LookupParameter("Mark")
        if mark_param and mark_param.HasValue:
            name = mark_param.AsString()

    params = {}
    if parameter_names is not None:
        params = get_parameters_dict(element, parameter_names)

    return {
        "element_id": eid_int(element.Id),
        "category": category_name,
        "family": family_name,
        "type": type_name,
        "name": name,
        "parameters": params,
        "location": serialize_location(element),
    }


def serialize_element_summary_full(element):
    """Like serialize_element_summary but with ALL parameters."""
    summary = serialize_element_summary(element)
    if summary is not None:
        summary["parameters"] = get_parameters_dict(element)
    return summary


# ---------------------------------------------------------------------------
# Part instance (Section 4.1.5)
# ---------------------------------------------------------------------------

def serialize_part_instance(element):
    """Convert a family instance to a PartInstance dict."""
    if element is None:
        return None

    elem_type = element.Document.GetElement(element.GetTypeId())
    family_name = None
    type_name = None
    if elem_type is not None:
        type_name = getattr(elem_type, "Name", None) or DB.Element.Name.__get__(elem_type)
        if hasattr(elem_type, "Family") and elem_type.Family is not None:
            family_name = elem_type.Family.Name
        elif hasattr(elem_type, "FamilyName"):
            family_name = elem_type.FamilyName

    params = get_parameters_dict(element)

    location = {"x": 0.0, "y": 0.0, "z": 0.0}
    rotation_degrees = 0.0
    loc = element.Location
    if isinstance(loc, DB.LocationPoint):
        location = {
            "x": round(loc.Point.X, 6),
            "y": round(loc.Point.Y, 6),
            "z": round(loc.Point.Z, 6),
        }
        rotation_degrees = round(math.degrees(loc.Rotation), 6)

    return {
        "element_id": eid_int(element.Id),
        "family": family_name,
        "type": type_name,
        "parameters": params,
        "location": location,
        "rotation_degrees": rotation_degrees,
    }


# ---------------------------------------------------------------------------
# Generic recursive serializer (Section 11.3)
# ---------------------------------------------------------------------------

def serialize(obj):
    """Recursively convert an object to a JSON-safe representation.

    Handles None, primitives, lists, dicts, and common Revit types.
    Falls back to str() if nothing else works.
    """
    if obj is None:
        return None
    if isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [serialize(item) for item in obj]
    if isinstance(obj, dict):
        return {str(k): serialize(v) for k, v in obj.items()}

    # Revit types
    if isinstance(obj, DB.ElementId):
        return serialize_element_id(obj)
    if isinstance(obj, DB.XYZ):
        return serialize_xyz(obj)
    if isinstance(obj, DB.BoundingBoxXYZ):
        return serialize_bounding_box(obj)
    if isinstance(obj, DB.Transform):
        return serialize_transform(obj)
    if isinstance(obj, DB.Element):
        return serialize_element_summary(obj)

    # Fallback: try json.dumps, then str()
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)
