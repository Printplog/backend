import re
import hashlib
import json
import math
from typing import Any, Dict, List, Tuple

from lxml import etree
from django.core.cache import cache


def _extract_from_dependency(depends_on: str, field_values: Dict[str, Any]) -> str:
    """
    Mirror frontend dependency extraction logic.
    Supports:
      - field_name
      - field_name[w1], field_name[w2]
      - field_name[ch1], field_name[ch1,2,5], field_name[ch1-4]
    """
    match = re.match(r"^(.+)\[(w|ch)(.+)\]$", depends_on)
    if match:
        field_name = match.group(1)
        extract_type = match.group(2)
        extract_pattern = match.group(3)
        field_value = field_values.get(field_name, "")
        if isinstance(field_value, str) and (
            field_value.startswith("data:image/") or field_value.startswith("blob:")
        ):
            return field_value
        string_value = str(field_value or "")
        if extract_type == "w":
            return _extract_word(string_value, extract_pattern)
        if extract_type == "ch":
            return _extract_chars(string_value, extract_pattern)
    field_value = field_values.get(depends_on, "")
    if isinstance(field_value, str) and (
        field_value.startswith("data:image/") or field_value.startswith("blob:")
    ):
        return field_value
    return str(field_value or "")


def _extract_word(text: str, pattern: str) -> str:
    words = text.strip().split()
    try:
        index = int(pattern) - 1
    except ValueError:
        return ""
    return words[index] if 0 <= index < len(words) else ""


def _extract_chars(text: str, pattern: str) -> str:
    if "," in pattern:
        indices = []
        for part in pattern.split(","):
            try:
                indices.append(int(part.strip()) - 1)
            except ValueError:
                continue
        return "".join(text[i] for i in indices if 0 <= i < len(text))
    if "-" in pattern:
        try:
            start, end = [int(x.strip()) for x in pattern.split("-")]
        except ValueError:
            return ""
        return text[start - 1 : end]
    try:
        index = int(pattern) - 1
    except ValueError:
        return ""
    return text[index] if 0 <= index < len(text) else ""


def _bool_from_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    value_str = str(value).strip().lower()
    return value_str in {"true", "1", "yes", "y"}


def _normalize_transform(el):
    """
    Consolidate transforms from both 'style' and 'transform' attribute.
    Ensures everything is in the 'transform' attribute for backend engines.
    """
    style = el.get("style", "")
    attr_transform = el.get("transform", "")

    # Simple regex to find transform: ...; in style
    style_transform_match = re.search(r"transform\s*:\s*([^;]+)", style)
    if not style_transform_match:
        return

    style_transform = style_transform_match.group(1).strip()

    # Get element dimensions for center calculation
    try:
        x = float(el.get("x", 0))
        y = float(el.get("y", 0))
        w = float(el.get("width", 0))
        h = float(el.get("height", 0))
    except (ValueError, TypeError):
        x = y = w = h = 0
        
    cx = x + w / 2
    cy = y + h / 2

    # Convert CSS transforms to SVG attribute format
    # 1. Convert translate(Xpx, Ypx) to translate(X, Y)
    normalized = re.sub(r"translate\(([^,)]+)px\s*,\s*([^,)]+)px\)", r"translate(\1, \2)", style_transform)
    normalized = re.sub(r"translate\(([^,)]+)px\)", r"translate(\1)", normalized)

    # 2. Convert rotate(Xdeg) to rotate(X, cx, cy)
    # SVG attributes MUST NOT have 'deg' units.
    has_dimensions = el.get("width") is not None and el.get("height") is not None
    
    def rotate_replacer(match):
        p1 = match.group(1)
        # Always strip deg
        angle = p1.replace("deg", "").strip()
        
        if "," not in p1 and has_dimensions:
            return f"rotate({angle}, {cx}, {cy})"
        
        # If it has commas or no dimensions, just ensure it's a valid number sequence
        return f"rotate({angle}{',' + p1.split(',', 1)[1] if ',' in p1 else ''})"

    normalized = re.sub(r"rotate\(([^)]+)\)", rotate_replacer, normalized)

    # Merge them
    combined = f"{attr_transform} {normalized}".strip()
    el.set("transform", combined)

    # Clean up style
    new_style = re.sub(r"transform\s*:\s*[^;]+;?", "", style).strip()
    new_style = re.sub(r"transform-origin\s*:\s*[^;]+;?", "", new_style).strip()
    new_style = re.sub(r"transform-box\s*:\s*[^;]+;?", "", new_style).strip()

    if new_style:
        el.set("style", new_style)
    else:
        el.attrib.pop("style", None)


def update_svg_from_field_updates(
    svg_content: str, form_fields: List[Dict[str, Any]], field_updates: List[Dict[str, Any]]
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Apply field updates to SVG content by mirroring frontend updateSvgFromFormData logic.
    Uses caching to avoid reprocessing the same SVG with the same field updates.

    Returns tuple of (updated_svg, updated_field_values)
    """
    if not svg_content or not form_fields:
        return svg_content, form_fields

    # Create cache key from SVG hash and field updates
    # This allows us to cache processed results for identical inputs
    svg_hash = hashlib.md5(svg_content.encode('utf-8')).hexdigest()
    field_updates_str = json.dumps(field_updates or [], sort_keys=True)
    cache_key = f"svg_update_{svg_hash}_{hashlib.md5(field_updates_str.encode('utf-8')).hexdigest()}"
    
    # Try to get from cache (cache for 1 hour)
    cached_result = cache.get(cache_key)
    if cached_result:
        return cached_result[0], cached_result[1]

    # Use lxml for much faster parsing of large SVGs (10-100x faster than BeautifulSoup)
    try:
        # Parse SVG with lxml (much faster for large files)
        parser = etree.XMLParser(recover=True, huge_tree=True)
        root = etree.fromstring(svg_content.encode('utf-8'), parser=parser)
    except Exception:
        # Fallback to original content if parsing fails
        return svg_content, form_fields

    # Build namespace map for xlink
    nsmap = {'xlink': 'http://www.w3.org/1999/xlink'}
    
    field_map = {field.get("id"): field for field in form_fields}
    field_values: Dict[str, Any] = {}

    # Initialize with current or default values
    for field in form_fields:
        field_values[field.get("id")] = field.get("currentValue") or field.get("defaultValue") or ""

    # Apply incoming updates
    for update in field_updates or []:
        field_id = update.get("id")
        if field_id in field_map:
            field_values[field_id] = update.get("value", "")

    # Apply dependency extraction pass
    computed_values: Dict[str, Any] = {}
    for field in form_fields:
        field_id = field.get("id")
        depends_on = field.get("dependsOn")
        if depends_on:
            computed_values[field_id] = _extract_from_dependency(depends_on, field_values)
        else:
            computed_values[field_id] = field_values.get(field_id, "")

    # Build a lookup map for faster element finding (O(1) instead of O(n))
    element_map = {}
    for elem in root.iter():
        elem_id = elem.get("id")
        if elem_id:
            element_map[elem_id] = elem

    # Update SVG elements based on computed values
    for field in form_fields:
        field_id = field.get("id")
        value = computed_values.get(field_id, "")

        # Select fields - match frontend logic: hide all first, then show selected
        options = field.get("options")
        if options:
            # First, hide ALL options (matching frontend behavior exactly)
            for option in options:
                svg_element_id = option.get("svgElementId")
                if not svg_element_id:
                    continue
                el = element_map.get(svg_element_id)
                if el is None:
                    continue
                # Hide all options first - match frontend exactly
                # Remove any existing style attribute first (frontend line 43)
                el.attrib.pop("style", None)
                # Set attributes that will be preserved in serialization
                el.set("opacity", "0")
                el.set("visibility", "hidden")
                el.set("display", "none")
            
            # Then, show only the selected option
            # Frontend uses field.currentValue directly for select comparison (line 51)
            # Use the value from field_values (which has the updated value from field_updates)
            field_value = str(field_values.get(field_id, ""))
            
            selected_option = None
            for option in options:
                option_value = str(option.get("value"))
                if option_value == field_value:
                    selected_option = option
                    break
            
            # Show the selected option - match frontend exactly
            if selected_option and selected_option.get("svgElementId"):
                selected_el = element_map.get(selected_option.get("svgElementId"))
                if selected_el is not None:
                    # Use SVG attributes that will be preserved in serialization
                    selected_el.set("opacity", "1")
                    selected_el.set("visibility", "visible")
                    # Remove display attribute to show the element (frontend line 60)
                    selected_el.attrib.pop("display", None)
            continue

        svg_element_id = field.get("svgElementId")
        if not svg_element_id:
            continue
        el = element_map.get(svg_element_id)
        if el is None:
            continue

        field_type = (field.get("type") or "text").lower()

        if field_type in {"upload", "file", "sign"}:
            # Sync transforms so we can apply rotation properly
            _normalize_transform(el)

            if value and isinstance(value, str) and value.strip():
                el.set("{http://www.w3.org/1999/xlink}href", value)
                el.set("preserveAspectRatio", "none")
            
            # Apply rotation if present
            rotation_val = field.get("rotation")
            
            # Inheritance logic: If this field depends on another field AND has no rotation of its own,
            # try to inherit the rotation from the parent field.
            if rotation_val is None and field.get("dependsOn"):
                base_parent_id = field.get("dependsOn").split('[')[0]
                parent_field = field_map.get(base_parent_id)
                if parent_field and parent_field.get("rotation") is not None:
                    rotation_val = parent_field.get("rotation")
            
            if rotation_val is not None:
                try:
                    rotation = float(rotation_val)
                    if math.isnan(rotation):
                        continue
                    
                    x = float(el.get("x", 0))
                    y = float(el.get("y", 0))
                    w = float(el.get("width", 0))
                    h = float(el.get("height", 0))
                    cx = x + w / 2
                    cy = y + h / 2
                    
                    existing_transform = el.get("transform", "")
                    base_rotation = 0
                    
                    # Parse existing rotation if present. We look for rotate(angle, ...)
                    rotate_match = re.search(r"rotate\s*\(\s*(-?\d+\.?\d*)", existing_transform)
                    if rotate_match:
                        base_rotation = float(rotate_match.group(1))
                    
                    # Add user rotation to base rotation
                    total_rotation = base_rotation + rotation
                    rotation_str = f"rotate({total_rotation}, {cx}, {cy})" if total_rotation != 0 else ""
                    
                    if "rotate(" in existing_transform:
                        # Replace existing rotation with combined rotation
                        new_transform = re.sub(r"rotate\([^)]+\)", rotation_str, existing_transform).strip()
                    elif rotation_str:
                        # Append new rotation
                        new_transform = f"{existing_transform} {rotation_str}".strip()
                    else:
                        new_transform = existing_transform
                    
                    if new_transform:
                        el.set("transform", new_transform)
                    else:
                        el.attrib.pop("transform", None)
                except (ValueError, TypeError):
                    pass
        elif field_type == "hide":
            visible = _bool_from_value(value)
            if visible:
                el.set("opacity", "1")
                el.set("visibility", "visible")
                el.attrib.pop("display", None)
            else:
                el.set("opacity", "0")
                el.set("visibility", "hidden")
                el.set("display", "none")
        else:
            string_value = "" if value is None else str(value)
            # For lxml, set text content (this replaces existing text)
            # Remove all children first to ensure clean text replacement
            for child in list(el):
                el.remove(child)
            el.text = string_value

    # Update stored values to reflect latest state
    for field in form_fields:
        field_id = field.get("id")
        if field_id in computed_values:
            field["currentValue"] = computed_values[field_id]

    # Convert back to string (lxml is faster at serialization too)
    result = (etree.tostring(root, encoding='unicode', pretty_print=False), form_fields)
    
    # Cache the result for 1 hour (3600 seconds)
    # Only cache if SVG is reasonably sized (< 5MB) to avoid memory issues
    if len(svg_content) < 5 * 1024 * 1024:  # 5MB limit
        cache.set(cache_key, result, 3600)
    
    return result

