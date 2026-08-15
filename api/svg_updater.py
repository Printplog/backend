import re
import hashlib
import json
import math
from typing import Any, Dict, List, Tuple
import logging

logger = logging.getLogger(__name__)

from lxml import etree
from django.core.cache import cache
import qrcode
import base64
from io import BytesIO


def _generate_qr_code(data: str) -> str:
    """Generate a QR code as a base64 PNG data URL."""
    if not data:
        return ""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{img_str}"


# Barcode symbologies whose aspect ratio must be preserved on injection (all 2D
# matrices, MaxiCode, and the height-modulated postal codes). Mirrors the frontend
# barcodeSymbologies catalog. Plain linear codes may stretch to fill their box.
_BARCODE_FIXED_ASPECT = frozenset({
    "qrcode", "microqr", "datamatrix", "gs1datamatrix", "gs1qrcode",
    "azteccode", "pdf417", "micropdf417", "maxicode", "codablockf",
    "dotcode", "hanxin",
    "onecode", "postnet", "planet", "royalmail", "auspost", "japanpost", "kix",
})


def _barcode_keeps_aspect(symbology: str) -> bool:
    """Whether a barcode symbology must NOT be stretched when injected."""
    return bool(symbology) and symbology.lower() in _BARCODE_FIXED_ASPECT


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
    svg_hash = hashlib.sha256(svg_content.encode('utf-8')).hexdigest()
    field_updates_str = json.dumps(field_updates or [], sort_keys=True)
    updates_hash = hashlib.sha256(field_updates_str.encode('utf-8')).hexdigest()
    cache_key = f"svg_update_{svg_hash}_{updates_hash}"
    
    # Try to get from cache (cache for 1 hour)
    cached_result = cache.get(cache_key)
    if cached_result:
        return cached_result[0], cached_result[1]

    # Use lxml for much faster parsing of large SVGs (10-100x faster than BeautifulSoup)
    try:
        # Parse SVG with lxml (much faster for large files)
        parser = etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            recover=False,
            huge_tree=False,
        )
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

    # Map for easy access to rotations and other non-value updates
    rotation_updates: Dict[str, float] = {}

    # Apply incoming updates
    for update in field_updates or []:
        field_id = update.get("id")
        if field_id in field_map:
            field_values[field_id] = update.get("value", "")
            if "rotation" in update:
                rotation_updates[field_id] = update.get("rotation")

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
    # Note: We support multiple elements per ID (duplicate IDs or prefix matching)
    element_map: Dict[str, List[etree.Element]] = {}
    for elem in root.iter():
        elem_id = elem.get("id")
        if elem_id:
            if elem_id not in element_map:
                element_map[elem_id] = []
            element_map[elem_id].append(elem)

    # Update SVG elements based on computed values
    for field in form_fields:
        field_id = field.get("id")
        value = computed_values.get(field_id, "")

        # Barcode fields are baked client-side (single-source): the finished PNG
        # lives in `barcodeImage`. Inject it directly — the backend never encodes
        # a barcode. `currentValue` stays the human-readable source text.
        if (field.get("type") or "").lower() == "barcode":
            value = field.get("barcodeImage") or ""

        # Select fields - match frontend logic: hide all first, then show selected
        options = field.get("options")
        if options:
            # First, hide ALL options (matching frontend behavior exactly)
            for option in options:
                svg_element_id = option.get("svgElementId")
                if not svg_element_id:
                    continue
                els = element_map.get(svg_element_id, [])
                for el in els:
                    # Hide all options first - match frontend exactly
                    # Remove any existing style attribute first (frontend line 43)
                    el.attrib.pop("style", None)
                    # Set attributes that will be preserved in serialization
                    el.set("opacity", "0")
                    el.set("visibility", "hidden")
                    el.set("display", "none")
            
            # Then, show only the selected option
            # Frontend uses field.currentValue directly for select comparison
            # Use the value from field_values (which has the updated value from field_updates)
            field_value = str(field_values.get(field_id, ""))
            
            selected_option = None
            for option in options:
                # Compare the technical value (usa, canada, etc.)
                if str(option.get("value")) == field_value:
                    selected_option = option
                    break
            
            # Show the selected option - match frontend exactly
            if selected_option and selected_option.get("svgElementId"):
                selected_els = element_map.get(selected_option.get("svgElementId"), [])
                for selected_el in selected_els:
                    # Use SVG attributes that will be preserved in serialization
                    selected_el.set("opacity", "1")
                    selected_el.set("visibility", "visible")
                    # Remove display attribute to show the element (frontend line 60)
                    selected_el.attrib.pop("display", None)

            # Match frontend logic: use selected option's displayText/label for text elements
            if selected_option:
                # Store the selected option's display value so it can be used for text injection below
                # and the raw value for image injection.
                select_text = selected_option.get("displayText") or selected_option.get("label") or field_value
                # We do NOT continue here, so the code below can handle text/image injection
            else:
                select_text = field_value
            
            logger.info(f"[Select-Updater] Field {field_id}: Value='{field_value}', SelectedOption='{selected_option.get('label') if selected_option else 'None'}', InjectionText='{select_text}'")

            # Use the select_text for text elements if this is a select field
            value = select_text

        # Find ALL target elements for this field
        # Mirror frontend findElements: Exact ID match + prefix matching [id^="baseId."]
        target_ids = {field.get("svgElementId"), field.get("id")}
        target_elements = []
        
        for tid in target_ids:
            if not tid: continue
            # 1. Exact matches
            target_elements.extend(element_map.get(tid, []))
            
            # 2. Prefix matches (e.g. if field is 'Name', find 'Name.text')
            # For efficiency, we only check this if tid is a base ID (not already specialized)
            if "." not in tid:
                for eid, els in element_map.items():
                    if eid.startswith(f"{tid}."):
                        target_elements.extend(els)
        
        # Deduplicate targets
        target_elements = list(dict.fromkeys(target_elements))

        for el in target_elements:
            field_type = (field.get("type") or "text").lower()
            tag_name = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            
            is_image_tag = tag_name in {"image", "use"}
            is_image_field = field_type in {"upload", "file", "sign", "qrcode", "barcode"}
            # Support .depends both as a type and as an extension in the ID for backward compatibility
            is_depends_field = field_type == "depends" or ".depends" in field_id
            
            # Final sanity check: if the value is definitely an image data URL, we should allow updating image tags
            is_image_value = isinstance(value, str) and (
                value.startswith("data:image/") or 
                value.startswith("blob:") or 
                ";base64," in value or
                (value.endswith(".png") or value.endswith(".jpg") or value.endswith(".jpeg"))
            )

            # Special case for QR code generation
            # Check both field_type and the element ID itself for robustness
            # Support both .qrcode and .qrcode_ prefixes
            is_qr_element = (
                field_type == "qrcode" or 
                ".qrcode." in el.get("id", "").lower() or 
                ".qrcode_" in el.get("id", "").lower() or
                el.get("id", "").lower().endswith(".qrcode")
            )
            
            if is_qr_element and not is_image_value:
                value = _generate_qr_code(str(value))
                is_image_value = True
                is_image_field = True # Mark as image field so it passes the check below

            # 1. IMAGE UPDATES
            if is_image_tag:
                # Only allow image updates if the field is an image field, a dependency field,
                # or if the value itself clearly looks like an image (backward compatibility).
                if not (is_image_field or is_depends_field or is_image_value):
                    continue
                
                # Sync transforms
                _normalize_transform(el)

                if value and isinstance(value, str) and value.strip():
                    if field_type == "qrcode":
                        logger.info(f"[SVG-Updater] Setting QR code image for {field_id} (len: {len(value)})")
                    
                    target_el = el
                    if tag_name == "use":
                        href_attr = el.get("href") or el.get("{http://www.w3.org/1999/xlink}href") or ""
                        if href_attr.startswith("#"):
                            referenced_id = href_attr[1:]
                            referenced_els = element_map.get(referenced_id, [])
                            if referenced_els:
                                target_el = referenced_els[0]

                    # Set both modern 'href' and legacy 'xlink:href' for max compatibility
                    target_el.set("href", value)
                    target_el.set("{http://www.w3.org/1999/xlink}href", value)
                    # 2D / postal barcodes must keep their aspect ratio or they
                    # won't scan; everything else stretches to fill its box.
                    if field_type == "barcode" and _barcode_keeps_aspect(field.get("symbology") or ""):
                        el.set("preserveAspectRatio", "xMidYMid meet")
                    else:
                        el.set("preserveAspectRatio", "none")
            
            # 2. VISIBILITY SPECIAL CASES (Can apply to any tag)
            elif field_type == "hide" or field_type == "status":
                visible = _bool_from_value(value)
                if visible:
                    el.set("opacity", "1")
                    el.set("visibility", "visible")
                    if "display" in el.attrib:
                        el.attrib.pop("display")
                else:
                    el.set("opacity", "0")
                    el.set("visibility", "hidden")
                    el.set("display", "none")

            # 3. TEXT UPDATES
            else:
                # Text updates should NOT hit image tags OR come from image fields
                if is_image_tag or is_image_field:
                    continue

                string_value = "" if value is None else str(value)
                for child in list(el):
                    el.remove(child)
                el.text = string_value

            # 4. UNIVERSAL TRANSFORMATIONS (Rotation)
            # Apply rotation to any element that hasn't been skipped
            rotation_val = rotation_updates.get(field_id)
            if rotation_val is None:
                rotation_val = field.get("rotation")
            
            # Inheritance logic (from parent fields for .depends)
            if rotation_val is None and field.get("dependsOn"):
                base_parent_id = field.get("dependsOn").split('[')[0]
                parent_field_rotation = rotation_updates.get(base_parent_id)
                if parent_field_rotation is None:
                     parent_field = field_map.get(base_parent_id)
                     if parent_field:
                         parent_field_rotation = parent_field.get("rotation")
                
                if parent_field_rotation is not None:
                    rotation_val = parent_field_rotation
            
            if rotation_val is not None:
                try:
                    rotation = float(rotation_val)
                    if not math.isnan(rotation):
                        # For rotation, we need center points. 
                        # Text elements use (x,y), Image elements use center.
                        x = float(el.get("x", 0))
                        y = float(el.get("y", 0))
                        
                        if is_image_tag:
                            w_val = el.get("width")
                            h_val = el.get("height")
                            if tag_name == "use" and (w_val is None or h_val is None):
                                href_attr = el.get("href") or el.get("{http://www.w3.org/1999/xlink}href") or ""
                                if href_attr.startswith("#"):
                                    referenced_id = href_attr[1:]
                                    referenced_els = element_map.get(referenced_id, [])
                                    if referenced_els:
                                        ref_el = referenced_els[0]
                                        if w_val is None:
                                            w_val = ref_el.get("width")
                                        if h_val is None:
                                            h_val = ref_el.get("height")
                            w = float(w_val) if w_val is not None else 0
                            h = float(h_val) if h_val is not None else 0
                            cx = x + w / 2
                            cy = y + h / 2
                        else:
                            cx = x
                            cy = y

                        existing_transform = el.get("transform", "")
                        base_rotation = 0
                        rotate_match = re.search(r"rotate\s*\(\s*(-?\d+\.?\d*)", existing_transform)
                        if rotate_match:
                            base_rotation = float(rotate_match.group(1))
                        
                        total_rotation = base_rotation + rotation
                        rotation_str = f"rotate({total_rotation}, {cx}, {cy})" if total_rotation != 0 else ""
                        
                        if "rotate(" in existing_transform:
                            new_transform = re.sub(r"rotate\([^)]+\)", rotation_str, existing_transform).strip()
                        elif rotation_str:
                            new_transform = f"{existing_transform} {rotation_str}".strip()
                        else:
                            new_transform = existing_transform
                        
                        if new_transform:
                            el.set("transform", new_transform)
                        else:
                            el.attrib.pop("transform", None)
                except (ValueError, TypeError):
                    pass

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
