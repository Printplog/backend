"""
svg_validator.py

SVG ID Validation Engine for SharpToolz.
Contains the DSL grammar rules and validation logic.
Synced with the frontend (idExtensions.ts / svgIdValidator.ts).

DSL Structure:
  [base_id].[depends_FIELD].[grayscale?].[track_ROLE]    ← depends replaces type
  [base_id].[type].[modifiers...].[track_ROLE]           ← normal field

Modifier prefixes: max_, min_, depends_, select_, link_, date_, gen_, grayscale_, showIf_
Flags (exact match): editable, tracking_id, grayscale, hide_checked, hide_unchecked

IMPORTANT RULES:
  - .depends_FIELD must come FIRST (position 1) after base ID
  - .depends_ REPLACES the need for a field type — no .text/.upload etc. alongside it
  - After .depends_, .grayscale (or .grayscale_N) and .track_ROLE are allowed
  - .track_ROLE must always be LAST
  - Field types (.text, .upload etc.) must come at position 1 (unless .depends_ is there)
  - .showIf_FIELDID[VALUE] — hides the form field until another field matches the value
    e.g. Error_Message.textarea.editable.showIf_Status[Error]
"""

import re
from typing import Optional, List

# ============================================================================
# WHITELIST CONSTANTS (Synced with frontend idExtensions.ts)
# ============================================================================

# Standard field types — must appear at position 1 (immediately after base ID)
VALID_TYPES = [
    "text", "textarea", "upload", "file", "sign", "date",
    "gen", "number", "checkbox", "range", "color", "email",
    "tel", "status", "password", "hide", "hide_checked", "hide_unchecked", "qrcode", "barcode", "fixed"
]
# Note: "depends" is NOT in VALID_TYPES — it's an extension, not a field type.

# Flag extensions (no underscore suffix, matched exactly)
FLAG_EXTENSIONS = [
    "editable",       # Editable after purchase
    "tracking_id",    # Mark as tracking ID field
    "grayscale",      # Full grayscale (100%)
]

# Modifier prefixes (matched by startswith)
VALID_MODIFIER_PREFIXES = [
    "mask_", "max_", "depends_", "select_", "link_", "date_", "gen_", "qrcode_", "barcode_", "grayscale_", "showIf_", "mode_",
]

# ============================================================================
# GRAMMAR RULES — allowedAfter mapping
# ============================================================================

# Maps extension key → list of what must immediately precede it.
# IMPORTANT: "depends" sets lastPartBase to "depends".
# After depends, only track_ is allowed (grayscale is inherited from source — track_ checked separately).
ALLOWED_AFTER = {
    "mask": ["upload", "file", "fixed", "editable", "grayscale"],
    "max":          ["text", "textarea", "gen", "number", "range", "min", "hide", "hide_checked", "hide_unchecked"],
    "min":          ["text", "textarea", "gen", "number", "range", "max", "hide", "hide_checked", "hide_unchecked"],
    "editable":     ["text", "textarea", "gen", "email", "number", "date", "checkbox",
                     "upload", "tel", "password", "range", "color", "file", "status", "sign", "qrcode",
                     "mask", "select", "depends", "hide", "hide_checked", "hide_unchecked", "qrcode", "barcode",
                     "grayscale"],
    "tracking_id":  ["gen", "max", "min", "text", "number", "hide", "hide_checked", "hide_unchecked"],
    "link":         ["tracking_id"],
    "date_format":  ["date", "hide", "hide_checked", "hide_unchecked"],
    "gen_rule":     ["gen", "hide", "hide_checked", "hide_unchecked"],
    "mode":         ["gen", "qrcode", "barcode", "hide", "hide_checked", "hide_unchecked"],
    "grayscale":    ["mask", "upload", "file", "fixed", "depends", "editable", "hide", "hide_checked", "hide_unchecked"],
    "select":       ["editable"],  # track_ is checked separately
    "showIf":       ["text", "textarea", "gen", "email", "number", "date", "checkbox",
                     "upload", "tel", "password", "range", "color", "file", "status", "sign", "qrcode", "barcode",
                     "hide", "hide_checked", "hide_unchecked",
                     "editable", "max", "min", "tracking_id", "date_format", "gen_rule", "select",
                     "grayscale"],
}

# ============================================================================
# VALIDATION FUNCTION
# ============================================================================

def validate_svg_id(element_id: str) -> tuple[bool, Optional[str]]:
    """
    Validates an SVG element ID against the SharpToolz DSL.

    Returns:
        (True, None) if valid
        (False, error_message) if invalid
    """
    if not element_id:
        return False, "ID cannot be empty"

    # Extract link URL BEFORE splitting (URLs contain dots)
    # Inlined to avoid circular imports with svg_parser
    cleaned_id = element_id
    if ".link_" in element_id:
        if ".link_\"" not in element_id:
            return False, "❌ Use quotes for links: .link_\"https://...\""
        
        link_index = element_id.index(".link_\"")
        url_start = link_index + 7
        url_end = element_id.find("\"", url_start)
        
        if url_end == -1:
            return False, "❌ Missing closing quote for .link_\"...\""
            
        # Optional: Check if there's non-quote content between link_ and "
        # In case someone does .link_SOMETHING"url"
        if element_id[link_index + 6] != "\"":
             return False, "❌ Use quotes immediately after .link_: .link_\"https://...\""

        cleaned_id = element_id[:link_index] + element_id[url_end + 1:]

    # 1. Mandatory Extension Rule (Enforce dot to make it an editable field)
    if "." not in cleaned_id:
        return False, "💡 Add '.text' or another extension to make this an editable field!"

    # Split on dots that are NOT inside parentheses — generator-rule tokens
    # (e.g. (dep_Order.Subtotal)) may contain dots and must stay one part.
    parts = re.split(r"\.(?![^(]*\))", cleaned_id)
    base_id = parts[0]

    # 2. Validate Base ID
    if not base_id:
        return False, "Base ID (before the first dot) cannot be empty"

    # 3. Check for empty segments (double dots)
    if any(not p for p in parts):
        return False, "ID contains empty segments (double dots)"

    type_count = 0
    last_part_base = ""

    # 4. Whitelist & Syntax Enforcement
    for i in range(1, len(parts)):
        part = parts[i]
        is_whitelisted = False

        # Check for flag extension (exact match, e.g. "editable", "grayscale")
        is_flag_extension = part in FLAG_EXTENSIONS or part == "mode"

        # Extract base key for prefixed parts (e.g. "max_50" → "max", "grayscale_80" → "grayscale")
        part_base = part.split("_")[0] if not is_flag_extension else part

        # ── SPECIAL: .depends_ must come FIRST ─────────────────────────────
        if part.startswith("depends_"):
            if i != 1:
                return (False,
                        f"❌ '.depends' must come FIRST (immediately after base ID), "
                        f"not after '.{last_part_base}'.")
            if part == "depends_":
                return False, "✍️ Add a field name after '.depends_' (e.g., .depends_Email)."
            is_whitelisted = True
            last_part_base = "depends"
            continue

        # ── SPECIAL: .showIf_ — conditional form field visibility ──────────
        if part.startswith("showIf_"):
            suffix = part[len("showIf_"):]  # e.g. "Status[Error]"
            if not suffix or suffix == "[":
                return False, "✍️ Add a field name after '.showIf_' (e.g., .showIf_Status[Error])."
            if "[" not in suffix or not suffix.endswith("]"):
                return False, "✍️ Use bracket syntax: .showIf_FieldId[Value] (e.g., .showIf_Status[Error])."
            bracket_pos = suffix.index("[")
            field_id_part = suffix[:bracket_pos]
            value_part = suffix[bracket_pos + 1:-1]
            if not field_id_part:
                return False, "✍️ Missing field name in '.showIf_': use .showIf_FieldId[Value]."
            if not value_part:
                return False, "✍️ Missing value in '.showIf_': use .showIf_FieldId[Value]."
            # Duplicate check
            if any(p.startswith("showIf_") for p in parts[1:i]):
                return False, "❌ Duplicate '.showIf_' not allowed."
            # Check allowedAfter
            if last_part_base and "showIf" in ALLOWED_AFTER:
                if last_part_base not in ALLOWED_AFTER["showIf"]:
                    return (False,
                            f"❌ Extension '.showIf_...' is not allowed after '.{last_part_base}'.")
            last_part_base = "showIf"
            continue

        # ── Check modifiers BEFORE field types ─────────────────────────────
        is_modifier_prefix = any(part.startswith(m) for m in VALID_MODIFIER_PREFIXES)
        is_flag = is_flag_extension or part_base in FLAG_EXTENSIONS

        # A. Field Types (only if NOT recognised as a modifier)
        # Note: gen_, qrcode_ and barcode_ at position 1 also count as the "primary type carrier"
        is_primary_carrier = i == 1 and (
            part.startswith("gen_") or part.startswith("qrcode_") or part.startswith("barcode_")
        )

        if (not is_modifier_prefix and not is_flag and part_base in VALID_TYPES) or is_primary_carrier:
            is_whitelisted = True
            type_count += 1

            # Field type must be at position 1
            if i != 1:
                return (False,
                        f"❌ Field type '.{part_base}' must come immediately after the base ID. "
                        f"(Or did you mean to use .depends_ or .qrcode_?)")

        # B. Modifiers / Flag Extensions
        if not is_whitelisted:
            if is_flag_extension:
                is_whitelisted = True
            elif is_modifier_prefix:
                is_whitelisted = True

            if is_whitelisted:
                # Check allowedAfter grammar
                if last_part_base:
                    lookup_key = part if is_flag_extension else part_base
                    if lookup_key in ALLOWED_AFTER:
                        if last_part_base not in ALLOWED_AFTER[lookup_key]:
                            return (False,
                                    f"❌ Extension '.{part}' is not allowed after '.{last_part_base}'.")

                # Check duplicates
                if is_flag_extension:
                    if any(p == part for p in parts[1:i]):
                        return False, f"❌ Duplicate extension '.{part}' not allowed."
                else:
                    if any(p.split("_")[0] == part_base for p in parts[1:i]):
                        return False, f"❌ Duplicate extension '.{part_base}' not allowed."

        # C. Tracking Roles (.track_ROLE must be LAST)
        if part.startswith("track_"):
            is_whitelisted = True
            if i != len(parts) - 1:
                return False, f"⚠️ Move '.{part}' to the very end of the ID."
            if part == "track_":
                return False, "Tracking role is missing a name (e.g., use .track_name)."

        if not is_whitelisted:
            return False, f"❌ '.{part}' is not a valid extension."

        # Missing value (e.g. .max_ without a number)
        if part.endswith("_") and not part.startswith("track_"):
            return False, f"✍️ Add a value after '{part[:-1]}' (e.g., .{part}50)."

        last_part_base = part_base

    # 5. Unique Field Type Rule
    if type_count > 1:
        return False, "Too many field types. Pick one: .text, .textarea, .upload, etc."

    return True, None


def validate_track_position(parts: List[str]) -> bool:
    """Check that .track_ is the last extension (backward-compat helper)."""
    track_index = next((i for i, p in enumerate(parts) if p.startswith("track_")), None)
    if track_index is not None:
        return track_index == len(parts) - 1
    return True
