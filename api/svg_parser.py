from defusedxml import ElementTree as SafeET
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Element
import logging
import re
from typing import Optional, Dict, List, Any, Tuple


logger = logging.getLogger(__name__)

# ============================================================================
# EXTENSION REGISTRY - Central configuration for all supported extensions
# ============================================================================

FIELD_TYPES = [
    "text", "textarea", "checkbox", "date", "upload",
    "number", "email", "tel", "gen", "password",
    "range", "color", "file", "status", "sign", "qrcode", "barcode",
    "hide", "hide_checked", "hide_unchecked"
]


EXTENSION_PREFIXES = {
    "max_": "max_value",       # Character/number limit
    "depends_": "dependency",   # Field synchronization with extraction support
    "track_": "tracking_role",  # Tracking role mapping
    "select_": "select_option", # Dropdown option
    "link_": "link_url",        # External link
    "date_": "date_format",     # Date format specification (MM/DD/YYYY, MMM_DD, etc.)
    "gen_": "generation_rule",  # Generation rule (rn[12], rc[6], etc.)
}

FLAG_EXTENSIONS = [
    "editable",     # Editable after purchase
    "tracking_id",  # Mark as tracking ID field
    "hide_checked", # Hide field (visible by default)
    "hide_unchecked" # Hide field (hidden by default)
]


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def split_svg_id(element_id: str) -> List[str]:
    """
    Split an SVG ID into parts based on dots, but NOT dots inside parentheses.
    Example: "base.qrcode_Name:_(dep_F.Subtotal)" -> ["base", "qrcode_Name:_(dep_F.Subtotal)"]
    """
    if not element_id:
        return []
    # Use negative lookahead to split on dots that are not followed by a closing parenthesis 
    # without an opening one (simple heuristic for "outside parentheses")
    return re.split(r"\.(?![^(]*\))", element_id)


def _fix_id_value(element_id: str) -> str | None:
    """
    Given a raw id string, return a fixed version or None if no fix is needed.
    Specifically handles cases where .depends_ is not the first extension.
    """
    if '.' not in element_id:
        # Even if no dot, we should fix @ if present for base IDs
        if "@" in element_id:
            return element_id.replace("@", "_")
        return None
    
    # Pre-sanitize @ in the whole ID
    original_id = element_id
    if "@" in element_id:
        element_id = element_id.replace("@", "_")

    parts = split_svg_id(element_id)
    base_id = parts[0]
    extensions = parts[1:]

    raw_depends = next((e for e in extensions if e.startswith('depends_') or e.startswith('depend_')), None)

    if not raw_depends:
        return None

    # Canonicalize typo 'depend_' → 'depends_'
    if raw_depends.startswith('depend_') and not raw_depends.startswith('depends_'):
        depends_val = f"depends_{raw_depends[7:]}"
    else:
        depends_val = raw_depends

    grayscale_val = next((e for e in extensions if e == 'grayscale' or e.startswith('grayscale_')), None)
    track_val = next((e for e in extensions if e.startswith('track_')), None)

    final_extensions = [depends_val]
    if grayscale_val:
        final_extensions.append(grayscale_val)
    if track_val:
        final_extensions.append(track_val)

    new_id = f"{base_id}.{'.'.join(final_extensions)}"
    return new_id if new_id != original_id else None


def fix_svg_element_ids(svg_content: str) -> Tuple[str, int]:
    """
    Fix invalid element IDs where extensions are in wrong order.

    Uses an XML-parser-based approach: parses the SVG with ElementTree and
    walks only the actual XML element 'id' attributes to apply fixes.
    This is safe for files with embedded base64 image data because the fix
    only touches real element attributes, never raw string content.

    Returns:
        tuple: (fixed_svg_content, number_of_fixes_made)
    """
    logger.info("[SVG-ID-Fixer] Starting ID fix scan...")
    fix_count = 0

    try:
        # Parse SVG — register all namespaces from the document first so they
        # are not rewritten (e.g. xlink, svg, etc.)
        namespaces: dict[str, str] = {}
        for event, elem in SafeET.iterparse(
            __import__('io').StringIO(svg_content), events=['start-ns']
        ):
            prefix, uri = elem
            if prefix not in namespaces:
                namespaces[prefix] = uri
                ET.register_namespace(prefix, uri)

        # Also register the default SVG namespace
        ET.register_namespace('', 'http://www.w3.org/2000/svg')
        ET.register_namespace('xlink', 'http://www.w3.org/1999/xlink')

        root = SafeET.fromstring(svg_content)

        for elem in root.iter():
            elem_id = elem.get('id')
            if not elem_id:
                continue
            fixed = _fix_id_value(elem_id)
            if fixed is not None:
                logger.info(f"[SVG-ID-Fixer] Fixed: {elem_id} → {fixed}")
                elem.set('id', fixed)
                fix_count += 1

        logger.info(f"[SVG-ID-Fixer] Completed. Fixed {fix_count} IDs.")

        if fix_count == 0:
            # Nothing changed — return original string untouched
            return svg_content, 0

        fixed_svg = ET.tostring(root, encoding='unicode', xml_declaration=False)
        return fixed_svg, fix_count

    except ET.ParseError as e:
        # SVG is not valid XML — skip fixing and return as-is so the next step
        # can produce a proper error message.
        logger.warning(f"[SVG-ID-Fixer] Skipped (SVG not well-formed): {e}")
        return svg_content, 0


def extract_link_url(element_id: str) -> tuple[str, Optional[str]]:
    """
    Extract link URL from element ID and return cleaned ID.
    Supports both legacy link_URL and new link_"URL" syntax.
    URLs are extracted BEFORE splitting by dots to preserve URL structure.
    
    Returns:
        tuple: (cleaned_element_id, url)
    """
    if ".link_\"" in str(element_id):
        # New correct syntax: .link_"URL"
        id_str = str(element_id)
        start = id_str.find(".link_\"")
        url_start = start + 7  # len(".link_\"")
        url_end = id_str.find("\"", url_start)
        
        if url_end != -1:
            url = id_str[url_start:url_end]
            # Remove link portion and join parts before and after
            cleaned_id = id_str[:start] + id_str[url_end + 1:]
            return cleaned_id, url

    return str(element_id), None


def is_element_visible(element: Element) -> bool:
    """
    Check if an SVG element is visible based on its attributes.
    """
    opacity = element.attrib.get("opacity", "1")
    visibility = element.attrib.get("visibility", "visible")
    display = element.attrib.get("display", "")
    
    return not (opacity == "0" or visibility == "hidden" or display == "none")


def get_extension_value(part: str, prefix: str) -> str:
    """
    Extract value from an extension part.
    Example: get_extension_value("max_50", "max_") -> "50"
    """
    return part.replace(prefix, "")


def _parse_barcode_carrier(value: str) -> Tuple[str, str, bool]:
    """
    Parse the value after 'barcode_': (symbology)(content rule), optional AUTO: prefix.

    Single-carrier format, modelled on .qrcode_. Returns (symbology, rule, is_auto).
    Examples:
      "(pdf417)(dep_OrderId)"      -> ("pdf417", "(dep_OrderId)", False)
      "AUTO:(code128)(rn[12])"     -> ("code128", "(rn[12])", True)
      "(ean13)"                    -> ("ean13", "", False)
      "pdf417"  (legacy, no parens)-> ("pdf417", "", False)
    """
    v = value or ""
    is_auto = False
    if v.startswith("AUTO:"):
        is_auto = True
        v = v[5:]
    m = re.match(r"^\(([^)]*)\)(.*)$", v)
    if m:
        return m.group(1), m.group(2), is_auto
    # Backward-compat: bare symbology with no parentheses.
    sym_match = re.match(r"^[^(]*", v)
    sym = sym_match.group(0) if sym_match else ""
    return sym, v[len(sym):], is_auto


# ============================================================================
# SVG ID VALIDATION — extracted to svg_validator.py for clean code
# ============================================================================
from .svg_validator import (
    VALID_TYPES,
    VALID_MODIFIER_PREFIXES as VALID_MODIFIERS,
    FLAG_EXTENSIONS as VALIDATOR_FLAG_EXTENSIONS,
    ALLOWED_AFTER,
    validate_svg_id,
    validate_track_position,
)



def get_field_name(base_id: str) -> str:
    """
    Convert base_id to human-readable name.
    Example: "customer_name" -> "Customer Name"
    """
    return base_id.replace("_", " ").title()


# ============================================================================
# SELECT FIELD HANDLING
# ============================================================================

def create_select_option(element_id: str, element: Element, parts: List[str], option_text: str = "") -> Dict[str, Any]:
    """
    Create a select option dictionary from element data.

    Convention:
      - label  = the part after .select_ (e.g. "Black" from .select_Black)
                 This is the human-readable text shown in the form dropdown.
      - value  = the SVG element's text content (e.g. "BLK")
                 This is what gets stored, sent to the backend, and inserted/shown
                 in the document. For image-bearing elements it would be image data.
    """
    select_part = next(p for p in parts if p.startswith("select_"))
    # Label is the human-readable key after select_ (e.g. "Black" from select_Black)
    label = get_extension_value(select_part, "select_").replace("_", " ")
    # Value is the SVG element's text content (e.g. "BLK"), fallback to label
    value = option_text.strip() or label

    return {
        "value": value,
        "label": label,
        "svgElementId": element_id,
        "displayText": value,
    }



def extract_select_modifiers(parts: List[str]) -> Dict[str, Any]:
    """
    Extract tracking role and editable flag from select option parts.
    """
    tracking_role = None
    editable = False
    
    track_part = next((p for p in parts if p.startswith("track_")), None)
    if track_part:
        tracking_role = get_extension_value(track_part, "track_")
    
    if "editable" in parts:
        editable = True
    
    return {
        "tracking_role": tracking_role,
        "editable": editable
    }


def create_select_field(base_id: str, element_id: str, editable: bool) -> Dict[str, Any]:
    """
    Create a new select field dictionary.
    """
    return {
        "id": base_id,
        "name": get_field_name(base_id),
        "type": "select",
        "svgElementId": element_id,
        "options": [],
        "defaultValue": "",
        "currentValue": "",
        "editable": editable,
    }


def update_select_field(field: Dict[str, Any], option: Dict[str, Any], 
                       is_visible: bool, modifiers: Dict[str, Any]):
    """
    Update select field with new option and modifiers.
    """
    curr_before = field.get("currentValue")
    # Set currentValue to visible option (the one shown in SVG)
    # Only set if not already set, so we don't default to the last visible option encountered
    if is_visible and not field.get("currentValue"):
        field["currentValue"] = option["value"]
        logger.info(f"[Select-Parser] Setting currentValue for {field['id']} to {option['value']} (visible: {is_visible}, before: '{curr_before}')")
    
    # Set defaultValue to first option if not set
    if not field.get("defaultValue") and field["options"]:
        field["defaultValue"] = field["options"][0]["value"]
        logger.info(f"[Select-Parser] Setting defaultValue for {field['id']} to {field['defaultValue']} (options_count: {len(field['options'])})")
    
    # Set tracking role if present
    if modifiers["tracking_role"]:
        field["trackingRole"] = modifiers["tracking_role"]
    
    # Set editable if any option has it
    if modifiers["editable"]:
        field["editable"] = True


# ============================================================================
# REGULAR FIELD HANDLING
# ============================================================================

def parse_field_extensions(parts: List[str]) -> Dict[str, Any]:
    """
    Parse all extensions from parts and return extracted values.
    """
    result = {
        "field_type": parts[0],  # Default to base_id
        "max_value": None,
        "dependency": None,
        "tracking_role": None,
        "date_format": None,
        "generation_rule": None,
        "symbology": None,  # bwip-js bcid for barcode fields (e.g. "code128", "ean13")
        "editable": False,
        "is_tracking_id": False,
        "requires_grayscale": False,
        "grayscale_intensity": None,
        "show_if": None,  # {"fieldId": str, "value": str} — conditional form field visibility
    }
    
    for part in parts[1:]:
        # Handle prefixed extensions
        if part.startswith("max_"):
            # Check if this is a max_ with generation rule like max_(A[10])
            max_content = get_extension_value(part, "max_")
            if max_content.startswith("(") and max_content.endswith(")"):
                # This is a generation rule for padding, e.g., max_(A[10])
                result["max_generation"] = max_content
            else:
                try:
                    result["max_value"] = str(max_content)
                except ValueError:
                    pass
        
        elif part.startswith("depends_"):
            # Extract dependency with optional extraction pattern
            # e.g., "field_name[w1]" or "field_name[ch1-4]"
            result["dependency"] = get_extension_value(part, "depends_")
        
        elif part.startswith("track_"):
            # Only set if it's the last extension
            if parts.index(part) == len(parts) - 1:
                result["tracking_role"] = get_extension_value(part, "track_")
        
        elif part.startswith("date_"):
            # Extract date format (e.g., "MM/DD/YYYY" from "date_MM/DD/YYYY" or "MMM_DD" from "date_MMM_DD")
            # Keep underscores as-is; frontend will convert them to spaces
            date_format = get_extension_value(part, "date_")
            result["date_format"] = date_format
            # If date_FORMAT is specified, field type should be "date"
            if result["field_type"] == parts[0]:  # Only set if not already set by another extension
                result["field_type"] = "date"
        
        elif part.startswith("gen_"):
            # Extract generation rule
            # e.g., "gen_(rn[12])" or "gen_FL(rn[12])(rc[6])"
            result["generation_rule"] = get_extension_value(part, "gen_")
            # Set field type to gen ONLY if not already set to something specialized (like qrcode)
            if result["field_type"] in [parts[0], "text", "textarea"]:
                result["field_type"] = "gen"
        
        elif part.startswith("qrcode_"):
            # Extract generation rule for QR codes
            result["generation_rule"] = get_extension_value(part, "qrcode_")
            result["field_type"] = "qrcode"

        elif part.startswith("barcode_"):
            # Single-carrier format: barcode_(symbology)(content rule), optional AUTO:.
            # Mirrors how .qrcode_ keeps everything in one carrier.
            sym, rule, is_auto = _parse_barcode_carrier(get_extension_value(part, "barcode_"))
            result["field_type"] = "barcode"
            result["symbology"] = sym or None
            if rule:
                result["generation_rule"] = ("AUTO:" + rule) if is_auto else rule
            elif is_auto:
                result["generation_rule"] = "AUTO:"

        # Handle flag extensions
        elif part == "tracking_id":
            result["field_type"] = "gen"
            result["is_tracking_id"] = True
        
        elif part == "editable":
            result["editable"] = True

        elif part.startswith("showIf_"):
            # Format: showIf_FieldId[Value]  e.g. showIf_Status[Error]
            suffix = part[len("showIf_"):]
            if "[" in suffix and suffix.endswith("]"):
                bracket_pos = suffix.index("[")
                field_id = suffix[:bracket_pos]
                value = suffix[bracket_pos + 1:-1]
                if field_id and value:
                    result["show_if"] = {"fieldId": field_id, "value": value}

        if part == "grayscale":
            result["requires_grayscale"] = True
            result["grayscale_intensity"] = "100"

        elif part.startswith("grayscale_"):
            result["requires_grayscale"] = True
            intensity_raw = get_extension_value(part, "grayscale_")
            try:
                intensity_value = int(float(intensity_raw))
                result["grayscale_intensity"] = str(max(0, min(100, intensity_value)))
            except ValueError:
                logger.warning(
                    "Invalid grayscale intensity '%s' on element '%s'; defaulting to 100",
                    intensity_raw,
                    parts[0],
                )
                result["grayscale_intensity"] = "100"

        # Handle field type extensions
        elif part.startswith("hide") or part in FIELD_TYPES:
            new_type = "hide" if part.startswith("hide") else part
            # Only update if it's currently the default (first part) or we're explicitly setting it to a specialized type.
            # Avoid overwriting 'qrcode' or 'gen' with 'text' or 'textarea'.
            if result["field_type"] == parts[0] or (new_type not in ["text", "textarea"]):
                result["field_type"] = new_type
            
            # All hide variants use inverted logic: Checked (true) means Hidden (false visibility)
            if result["field_type"] == "hide":
                result["inverted"] = True
    
    if result["requires_grayscale"] and result["grayscale_intensity"] is None:
        result["grayscale_intensity"] = "100"

    return result


def get_default_value(field_type: str, text_content: str, parts: List[str]) -> Any:
    """
    Determine the default value based on field type.
    """
    if field_type == "checkbox":
        return False
    
    elif field_type == "hide":
        hide_part = next((p for p in parts if p.startswith("hide")), "hide")
        # .hide and .hide_checked start CHECKED (True) -> Hidden
        # .hide_unchecked starts UNCHECKED (False) -> Visible
        return hide_part != "hide_unchecked"
    
    else:
        return text_content


def create_regular_field(base_id: str, element_id: str, extensions: Dict[str, Any], 
                        default_value: Any, url: Optional[str], helper_text: Optional[str] = None) -> Dict[str, Any]:
    """
    Create a regular (non-select) field dictionary.
    """
    field = {
        "id": base_id,
        "name": get_field_name(base_id),
        "type": extensions["field_type"],
        "svgElementId": element_id,
        "defaultValue": default_value,
        "currentValue": default_value,
        "isTrackingId": extensions["is_tracking_id"],
        "editable": extensions["editable"],
    }
    
    # Add optional properties
    if extensions["tracking_role"]:
        field["trackingRole"] = extensions["tracking_role"]
    
    if extensions["max_value"] is not None:
        field["max"] = extensions["max_value"]
    
    if extensions["dependency"]:
        field["dependsOn"] = extensions["dependency"]
    
    if extensions["date_format"]:
        field["dateFormat"] = extensions["date_format"]
    
    if extensions["generation_rule"]:
        field["generationRule"] = extensions["generation_rule"]
        if extensions["generation_rule"].startswith("AUTO:"):
            field["generationMode"] = "auto"

    if extensions.get("symbology"):
        field["symbology"] = extensions["symbology"]
    
    if extensions.get("max_generation"):
        field["maxGeneration"] = extensions["max_generation"]
    
    if url:
        field["link"] = url
    
    if helper_text:
        field["helperText"] = helper_text

    if extensions.get("requires_grayscale"):
        field["requiresGrayscale"] = True
        field["grayscaleIntensity"] = extensions.get("grayscale_intensity", 100)

    if extensions.get("show_if"):
        field["showIf"] = extensions["show_if"]

    return field


# ============================================================================
# ID-ONLY FIELD PARSER
# ============================================================================

def parse_field_from_id(element_id: str, text_content: str = "") -> Optional[Dict[str, Any]]:
    """
    Parse a field definition directly from an SVG element ID string.

    This does not require an ET.Element — all field metadata (type, generationRule,
    max, dependsOn, etc.) is encoded in the ID itself.

    text_content: preserved from the existing form_field's defaultValue so the
    user's text isn't lost when only the ID metadata changes.

    Returns a field dict on success, or None if the ID doesn't produce a valid field.
    """
    if not element_id:
        return None

    # Select option IDs need multi-element context — skip them here
    if any(p.startswith("select_") for p in split_svg_id(element_id)):
        return None

    # Known explicit field types (mapped by parse_field_extensions via extension parts)
    KNOWN_FIELD_TYPES = {
        "text", "textarea", "select", "checkbox", "date", "upload",
        "file", "sign", "gen", "status", "hide", "number", "range",
        "color", "email", "tel", "url", "password", "qrcode", "barcode",
    }

    try:
        # Extract link URL before splitting (URLs contain dots)
        cleaned_id, url = extract_link_url(element_id)

        parts = split_svg_id(cleaned_id)
        if not parts or not parts[0]:
            return None

        if not validate_track_position(parts):
            return None

        extensions = parse_field_extensions(parts)

        # Normalize: if field_type was not set by any extension it defaults to parts[0].
        # In that case, treat the field as plain text (mirrors the full element parser).
        if extensions["field_type"] not in KNOWN_FIELD_TYPES:
            extensions["field_type"] = "text"

        default_value = get_default_value(extensions["field_type"], text_content, parts)

        return create_regular_field(
            base_id=parts[0],
            element_id=element_id,
            extensions=extensions,
            default_value=default_value,
            url=url,
            helper_text=None,
        )
    except Exception as e:
        logger.warning(f"[parse_field_from_id] Failed for id='{element_id}': {e}")
        return None


# ============================================================================
# MAIN PARSER FUNCTION
# ============================================================================

    
def process_element_to_field(element: Element, fields_list: List[Dict[str, Any]], select_options_map: Dict[str, List[Dict[str, Any]]]):
    """
    Process a single SVG element and either update existing fields or add new ones.
    """
    # Prioritize data-name (preserves original naming with quotes/dots/slashes)
    # Prefer 'id' over 'data-name' for Sharptoolz DSL compatibility.
    # Data-name often contains 'excess' metadata from design tools (e.g. Passenger_Name@name).
    original_element_id = (element.attrib.get("id") or element.attrib.get("data-name") or "").strip()
    
    # Sanitize: some design tools (Inkscape/Figma) inject '@' or other characters into IDs.
    # Replacing '@' with '_' ensures the IDs remain valid DSL identifiers.
    if "@" in original_element_id:
        original_element_id = original_element_id.replace("@", "_")
    if not original_element_id:
        return

    # Robust text extraction: Handle multiline text (tspans)
    text_parts = []
    if element.text is not None and element.text.strip():
        text_parts.append(element.text.strip())
        
    for child in element:
        if child.text is not None and child.text.strip():
            text_parts.append(child.text.strip())
        if child.tail is not None and child.tail.strip():
            text_parts.append(child.tail.strip())
    
    text_content = "\n".join(text_parts)
    
    # Plain IDs (no dot) are not form fields — skip silently, no warning needed
    if "." not in original_element_id:
        return

    # 1. Validate ID against DSL
    is_valid, error = validate_svg_id(original_element_id)
    if not is_valid:
        logger.warning(f"Skipping element '{original_element_id}': {error}")
        return

    # Extract link URL before splitting (URLs contain dots)
    element_id, url = extract_link_url(original_element_id)
    
    # Split ID into parts
    parts = split_svg_id(element_id)
    base_id = parts[0]
    
    # ====================================================================
    # HANDLE SELECT FIELDS
    # ====================================================================
    if any(p.startswith("select_") for p in parts):
        option = create_select_option(original_element_id, element, parts, text_content)
        modifiers = extract_select_modifiers(parts)
        
        # Create select field if first option
        if base_id not in select_options_map:
            select_options_map[base_id] = []
            field = create_select_field(base_id, original_element_id, modifiers["editable"])
            fields_list.append(field)
        
        # Add option to map
        select_options_map[base_id].append(option)
        
        # Update the field with option and modifiers
        for field in fields_list:
            if field["id"] == base_id:
                field["options"] = select_options_map[base_id]
                update_select_field(field, option, is_element_visible(element), modifiers)
                break
        
        return
    
    # ====================================================================
    # HANDLE REGULAR FIELDS
    # ====================================================================
    
    # Validate track_ position
    if not validate_track_position(parts):
        logger.warning(f"Skipping element {element_id}: track_ extension must be last")
        return
    
    # Parse all extensions
    extensions = parse_field_extensions(parts)

    has_depends = any(p.startswith("depends_") for p in parts[1:])
    if extensions.get("requires_grayscale") and extensions["field_type"] not in {"upload", "file"} and not has_depends:
        logger.warning(
            "Grayscale extension on non-upload field '%s' (element ID: %s)",
            base_id,
            original_element_id,
        )
    
    # Get default value
    default_value = get_default_value(extensions["field_type"], text_content, parts)
    
    # Extract helper text from data-helper attribute
    helper_text = element.attrib.get("data-helper", "")
    
    # Create field
    field = create_regular_field(base_id, original_element_id, extensions, default_value, url, helper_text)
    fields_list.append(field)



def parse_svg_to_form_fields(svg_text: str) -> List[Dict[str, Any]]:
    """
    Parse SVG text and convert elements with IDs into form field definitions.
    Elements with the same base_id are merged into a single field.
    """
    try:
        root = SafeET.fromstring(svg_text)
    except ET.ParseError as e:
        logger.error(f"Failed to parse SVG: {e}")
        return []
    
    # Robust element discovery: Iterate through all nodes and manually check for ID/data-name attributes.
    # This is namespace-agnostic and works across different versions of xml.etree.
    elements = []
    for el in root.iter():
        if "id" in el.attrib or "data-name" in el.attrib:
            elements.append(el)
    
    # De-duplicate elements (some might have both)
    elements = list({id(el): el for el in elements}.values())
    
    fields_map: Dict[str, Dict[str, Any]] = {}
    select_options_map: Dict[str, List[Dict[str, Any]]] = {}
    
    for element in elements:
        # Temporary list to catch the new field from this element
        temp_list = []
        process_element_to_field(element, temp_list, select_options_map)
        
        if not temp_list:
            continue
            
        new_field = temp_list[0]
        base_id = new_field["id"]
        
        if base_id not in fields_map:
            fields_map[base_id] = new_field
        else:
            existing_field = fields_map[base_id]
            # Warn on genuine duplicates (not select options, which intentionally share a base_id)
            if new_field["type"] != "select" and existing_field.get("type") != "select":
                logger.warning(
                    "[Duplicate Base ID] '%s' appears more than once. "
                    "Only the first occurrence is used — rename the duplicate element.",
                    base_id,
                )
            # Merge logic:
            # 1. Select type takes precedence
            if new_field["type"] == "select":
                # Transfer options and modifiers if moving from regular to select
                new_field["currentValue"] = existing_field.get("currentValue") or new_field.get("currentValue")
                new_field["touched"] = existing_field.get("touched") or new_field.get("touched")
                # Preserve editable/trackingRole accumulated from prior options
                if existing_field.get("editable"):
                    new_field["editable"] = True
                if existing_field.get("trackingRole"):
                    new_field.setdefault("trackingRole", existing_field["trackingRole"])
                fields_map[base_id] = new_field
            elif existing_field["type"] == "select":
                # Keep select but maybe update current/default value if this new element is visible
                # update_select_field already handles some of this via select_options_map indirectly
                pass

            # Additional merging (editable, trackingRole, etc.) — always target the live map entry
            live_field = fields_map[base_id]
            if new_field.get("editable"):
                live_field["editable"] = True
            if new_field.get("trackingRole"):
                live_field["trackingRole"] = new_field["trackingRole"]
    
    # Post-process select fields: accumulate editable/trackingRole from ALL option IDs.
    # process_element_to_field uses a fresh temp_list per call, so only the FIRST option's
    # modifiers get applied inline. This pass covers 2nd+ options.
    for field in fields_map.values():
        if field.get('type') == 'select':
            for opt in field.get('options', []):
                opt_parts = split_svg_id(opt.get('svgElementId', ''))
                if 'editable' in opt_parts:
                    field['editable'] = True
                track_part = next((p for p in opt_parts if p.startswith('track_')), None)
                if track_part:
                    field['trackingRole'] = track_part[6:]  # strip 'track_'

    return list(fields_map.values())
