import base64
import copy
import re
import secrets
import string
from datetime import datetime
from io import BytesIO

from PIL import Image
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import EmailValidator, URLValidator
from django.utils import timezone
from rest_framework.exceptions import ValidationError


DIRECT_INPUT_TYPES = frozenset({
    "text", "textarea", "email", "tel", "url", "password", "number", "range",
    "select", "checkbox", "hide", "date", "upload", "file", "sign", "gen",
    "color", "qrcode", "barcode",
})

ASSET_TYPES = frozenset({"upload", "file", "sign"})
BOOLEAN_TYPES = frozenset({"checkbox", "hide"})
MAX_ASSET_BYTES = 5 * 1024 * 1024
MAX_VALUE_LENGTH = 20_000
MAX_CODE_LENGTH = 4_000
_IMAGE_DATA_URL = re.compile(r"^data:image/(png|jpeg|jpg|webp);base64,([A-Za-z0-9+/=\s]+)$", re.I)


def _random_numbers(count: int) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(max(0, min(count, 512))))


def _random_chars(count: int, kind: str = "rc") -> str:
    alphabet = string.ascii_letters
    if kind == "ru":
        alphabet = string.ascii_uppercase
    elif kind == "rl":
        alphabet = string.ascii_lowercase
    return "".join(secrets.choice(alphabet) for _ in range(max(0, min(count, 512))))


def _extract_chars(text: str, pattern: str) -> str:
    if "," in pattern:
        output = []
        for part in pattern.split(","):
            try:
                index = int(part.strip()) - 1
            except ValueError:
                continue
            if 0 <= index < len(text):
                output.append(text[index])
        return "".join(output)
    if "-" in pattern:
        try:
            start, end = [int(part.strip()) for part in pattern.split("-", 1)]
        except ValueError:
            return ""
        return text[max(0, start - 1):max(0, end)]
    try:
        index = int(pattern) - 1
    except ValueError:
        return ""
    return text[index] if 0 <= index < len(text) else ""


def _extract_reference(pattern: str, values: dict) -> str:
    match = re.match(r"^(dep_)?(.+?)(?:\[(w|ch)(.+)\])?$", pattern)
    if not match:
        return str(values.get(pattern, values.get(pattern.removeprefix("dep_"), "")) or "")
    is_dependency, field_name, extract_type, extract_pattern = match.groups()
    value = values.get(f"dep_{field_name}") if is_dependency else None
    if value is None or value == "":
        value = values.get(field_name, "")
    text = str(value or "")
    if extract_type == "w":
        try:
            return text.strip().split()[int(extract_pattern) - 1]
        except (ValueError, IndexError):
            return ""
    if extract_type == "ch":
        return _extract_chars(text, extract_pattern)
    return text


def _generate_pattern(content: str, values: dict) -> str:
    match = re.fullmatch(r"(rn|rc|ru|rl)\[(\d+)\]", content)
    if match:
        kind, raw_count = match.groups()
        count = int(raw_count)
        return _random_numbers(count) if kind == "rn" else _random_chars(count, kind)
    match = re.fullmatch(r"date\[(.+)\]", content)
    if match:
        now = timezone.localtime()
        date_format = match.group(1)
        replacements = {
            "YYYY": f"{now.year:04d}", "MM": f"{now.month:02d}", "DD": f"{now.day:02d}",
            "HH": f"{now.hour:02d}", "mm": f"{now.minute:02d}", "ss": f"{now.second:02d}",
        }
        for token, replacement in replacements.items():
            date_format = date_format.replace(token, replacement)
        return date_format
    if content.startswith("env_"):
        env_name = content[4:].upper()
        return {
            "PLATFORM": "SharpToolz",
            "YEAR": str(timezone.localdate().year),
            "USER_ID": f"U-{_random_numbers(6)}",
        }.get(env_name, "")
    duplicate = re.fullmatch(r"(.+)\[(\d+)\]", content)
    if duplicate:
        character, raw_count = duplicate.groups()
        count = min(int(raw_count), 512)
        if character in values and values[character] is not None and values[character] != "":
            return str(values[character]) * count
        return character * count
    return _extract_reference(content, values)


def generate_value(rule: str, values: dict, max_length=None) -> str:
    if not rule:
        return ""
    if rule.startswith("AUTO:"):
        rule = rule[5:]
    tokens = re.findall(r"[^()]+|\([^)]+\)", rule)
    parts = []
    fill_indices = []
    for token in tokens:
        if token.startswith("(") and token.endswith(")"):
            content = token[1:-1]
            fill = re.fullmatch(r"(.+?)\[fill\]", content)
            if fill and max_length is not None:
                fill_indices.append(len(parts))
                parts.append(("fill", fill.group(1) or "<"))
            else:
                parts.append(("value", _generate_pattern(content, values)))
        else:
            parts.append(("value", token.replace("_", " ")))
    current_length = sum(len(value) for kind, value in parts if kind == "value")
    fill_needed = max(0, int(max_length) - current_length) if max_length is not None else 0
    result = "".join(
        value * (fill_needed if kind == "fill" and index == fill_indices[-1] else 0)
        if kind == "fill" else value
        for index, (kind, value) in enumerate(parts)
    )
    if max_length is not None:
        result = result[:int(max_length)]
    return result.replace("\\n", "\n")


def _apply_max_generation(value: str, rule: str | None) -> str:
    if not rule:
        return value
    match = re.fullmatch(r"\((.+)\[(\d+)\]\)", rule)
    if not match:
        return value
    character, raw_length = match.groups()
    return value + character * max(0, int(raw_length) - len(value))


def _validate_image(value, field_id: str) -> str:
    if not isinstance(value, str):
        raise ValidationError({"values": {field_id: "An image data URL is required."}})
    match = _IMAGE_DATA_URL.fullmatch(value)
    if not match:
        raise ValidationError({"values": {field_id: "Only PNG, JPEG, or WebP image data is allowed."}})
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValidationError({"values": {field_id: "The image data is invalid."}}) from exc
    if len(raw) > MAX_ASSET_BYTES:
        raise ValidationError({"values": {field_id: "The image must be 5 MB or smaller."}})
    try:
        image = Image.open(BytesIO(raw))
        image.verify()
        if image.width * image.height > 25_000_000:
            raise ValueError("too many pixels")
    except Exception as exc:
        raise ValidationError({"values": {field_id: "The image could not be verified."}}) from exc
    return value


def _validate_value(field: dict, value):
    field_id = field.get("id", "field")
    field_type = (field.get("type") or "text").lower()
    if field_type in ASSET_TYPES:
        return "" if value is None or value == "" else _validate_image(value, field_id)
    if field_type in BOOLEAN_TYPES:
        if not isinstance(value, bool):
            raise ValidationError({"values": {field_id: "A boolean value is required."}})
        return value
    if field_type in {"number", "range"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError({"values": {field_id: "A number is required."}})
        maximum = field.get("max")
        if maximum is not None and value > maximum:
            raise ValidationError({"values": {field_id: f"Value cannot exceed {maximum}."}})
        return value
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationError({"values": {field_id: "A string value is required."}})
    limit = MAX_CODE_LENGTH if field_type in {"barcode", "qrcode"} else MAX_VALUE_LENGTH
    if field.get("max") is not None:
        limit = min(limit, int(field["max"]))
    if len(value) > limit:
        raise ValidationError({"values": {field_id: f"Value cannot exceed {limit} characters."}})
    if field.get("options"):
        allowed = {str(option.get("value")) for option in field["options"]}
        if value not in allowed:
            raise ValidationError({"values": {field_id: "Select one of the configured options."}})
    try:
        if field_type == "email" and value:
            EmailValidator()(value)
        if field_type == "url" and value:
            URLValidator(schemes=["http", "https"])(value)
    except DjangoValidationError as exc:
        raise ValidationError({"values": {field_id: exc.messages[0]}}) from exc
    if field_type == "color" and value and not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValidationError({"values": {field_id: "A six-digit hex color is required."}})
    return value


def _extract_dependency(depends_on: str, values: dict) -> str:
    match = re.match(r"^(.+)\[(w|ch|date)(.*)\]$", depends_on)
    if not match:
        return str(values.get(depends_on.split("[")[0], "") or "")
    field_id, kind, pattern = match.groups()
    pattern = pattern.removeprefix(":")
    raw_value = values.get(field_id, "")
    if isinstance(raw_value, str) and raw_value.startswith("data:image/"):
        return raw_value
    text = str(raw_value or "")
    if kind == "w":
        try:
            return text.strip().split()[int(pattern) - 1]
        except (ValueError, IndexError):
            return ""
    if kind == "ch":
        return _extract_chars(text, pattern)
    if kind == "date":
        cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", text, flags=re.I)
        parsed = None
        for candidate in (cleaned, cleaned.replace(",", "")):
            try:
                parsed = datetime.fromisoformat(candidate)
                break
            except ValueError:
                continue
        if not parsed:
            return text
        output = pattern or "MM/dd/yyyy"
        replacements = {
            "yyyy": f"{parsed.year:04d}", "YYYY": f"{parsed.year:04d}",
            "MM": f"{parsed.month:02d}", "dd": f"{parsed.day:02d}", "DD": f"{parsed.day:02d}",
        }
        for token, replacement in replacements.items():
            output = output.replace(token, replacement)
        return output
    return text


def compile_document_fields(template_fields, supplied_values, barcode_images=None):
    if not isinstance(supplied_values, dict):
        raise ValidationError({"values": "An object keyed by template field ID is required."})
    if len(supplied_values) > 500:
        raise ValidationError({"values": "Too many values were supplied."})
    fields = copy.deepcopy(template_fields or [])
    field_map = {field.get("id"): field for field in fields if field.get("id")}
    unknown = set(supplied_values) - set(field_map)
    if unknown:
        raise ValidationError({"values": f"Unknown field IDs: {', '.join(sorted(unknown))}."})

    values = {
        field_id: field.get("currentValue", field.get("defaultValue", ""))
        for field_id, field in field_map.items()
    }
    for field_id, raw_value in supplied_values.items():
        field = field_map[field_id]
        field_type = (field.get("type") or "").lower()
        is_generated = (
            field.get("generationMode") == "auto"
            or str(field.get("generationRule") or "").startswith("AUTO:")
            or bool(field.get("isTrackingId"))
            or (field_type in {"qrcode", "barcode"} and bool(field.get("generationRule")))
        )
        if field.get("dependsOn") or field_type == "status" or field_type not in DIRECT_INPUT_TYPES or is_generated:
            raise ValidationError({"values": {field_id: "This field is generated or managed by the template."}})
        values[field_id] = _validate_value(field, raw_value)

    display_values = dict(values)
    for field_id, field in field_map.items():
        if field.get("options"):
            selected = next(
                (option for option in field["options"] if str(option.get("value")) == str(values.get(field_id, ""))),
                None,
            )
            if selected:
                display_values[field_id] = selected.get("displayText") or selected.get("label") or values[field_id]

    for field in fields:
        field_id = field.get("id")
        field_type = (field.get("type") or "").lower()
        rule = field.get("generationRule") or ""
        is_generated = (
            field.get("generationMode") == "auto"
            or rule.startswith("AUTO:")
            or bool(field.get("isTrackingId"))
            or (field_type in {"qrcode", "barcode"} and bool(rule))
        )
        if is_generated:
            generated = generate_value(rule, display_values, field.get("max")) if rule else ""
            if not generated:
                length = min(int(field.get("max") or 8), 100)
                generated = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(length))
            generated = _apply_max_generation(generated, field.get("maxGeneration"))
            values[field_id] = generated
            display_values[field_id] = generated

    for field in fields:
        field_id = field.get("id")
        if field.get("dependsOn"):
            values[field_id] = _extract_dependency(field["dependsOn"], display_values)
            display_values[field_id] = values[field_id]

    barcode_images = barcode_images or {}
    if not isinstance(barcode_images, dict):
        raise ValidationError({"barcode_images": "An object is required."})
    if len(barcode_images) > 500:
        raise ValidationError({"barcode_images": "Too many barcode images were supplied."})
    unknown_barcode_ids = set(barcode_images) - set(field_map)
    if unknown_barcode_ids:
        raise ValidationError({
            "barcode_images": f"Unknown field IDs: {', '.join(sorted(unknown_barcode_ids))}."
        })
    for field in fields:
        field_id = field.get("id")
        field["currentValue"] = values.get(field_id, "")
        if (field.get("type") or "").lower() == "barcode" and field_id in barcode_images:
            field["barcodeImage"] = _validate_image(barcode_images[field_id], field_id)

    tracking_id = next(
        (str(field.get("currentValue")) for field in fields if field.get("isTrackingId") and field.get("currentValue")),
        None,
    )
    return fields, tracking_id


def apply_editable_updates(document_fields, supplied_values, barcode_images=None):
    if not isinstance(supplied_values, dict):
        raise ValidationError({"values": "An object keyed by field ID is required."})
    fields = copy.deepcopy(document_fields or [])
    field_map = {field.get("id"): field for field in fields if field.get("id")}
    unknown = set(supplied_values) - set(field_map)
    if unknown:
        raise ValidationError({"values": f"Unknown field IDs: {', '.join(sorted(unknown))}."})
    values = {
        field_id: field.get("currentValue", field.get("defaultValue", ""))
        for field_id, field in field_map.items()
    }
    for field_id, raw_value in supplied_values.items():
        field = field_map[field_id]
        if not field.get("editable") or field.get("dependsOn") or field.get("isTrackingId"):
            raise ValidationError({"values": {field_id: "This field cannot be edited after creation."}})
        values[field_id] = _validate_value(field, raw_value)

    display_values = dict(values)
    for field_id, field in field_map.items():
        if field.get("options"):
            selected = next(
                (option for option in field["options"] if str(option.get("value")) == str(values.get(field_id, ""))),
                None,
            )
            if selected:
                display_values[field_id] = selected.get("displayText") or selected.get("label") or values[field_id]
    for field in fields:
        field_id = field.get("id")
        if field.get("dependsOn"):
            values[field_id] = _extract_dependency(field["dependsOn"], display_values)
            display_values[field_id] = values[field_id]

    barcode_images = barcode_images or {}
    if not isinstance(barcode_images, dict):
        raise ValidationError({"barcode_images": "An object is required."})
    if len(barcode_images) > 500:
        raise ValidationError({"barcode_images": "Too many barcode images were supplied."})
    unknown_barcode_ids = set(barcode_images) - set(field_map)
    if unknown_barcode_ids:
        raise ValidationError({
            "barcode_images": f"Unknown field IDs: {', '.join(sorted(unknown_barcode_ids))}."
        })
    for field in fields:
        field_id = field.get("id")
        field["currentValue"] = values.get(field_id, "")
        if (field.get("type") or "").lower() == "barcode" and field_id in barcode_images:
            if not field.get("editable"):
                raise ValidationError({"barcode_images": {field_id: "This barcode cannot be edited."}})
            field["barcodeImage"] = _validate_image(barcode_images[field_id], field_id)
    return fields
