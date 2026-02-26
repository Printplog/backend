import json
import logging
from typing import List, Dict, Any, Tuple
from .svg_parser import parse_field_from_id

logger = logging.getLogger(__name__)


def sync_form_fields_with_patches(instance, patches: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Synchronize form_fields with SVG patches.

    Handles two patch types:
      - innerText: update the field's defaultValue / currentValue
      - id:        re-derive field metadata directly from the NEW id string
                   using parse_field_from_id() — no SVG file loading needed.
    """
    form_fields = instance.form_fields or []
    if not patches:
        return form_fields, False

    updated_fields = json.loads(json.dumps(form_fields))
    modified = False

    print(f"[SVG-Sync] Processing {len(patches)} patches for instance: {instance.id}")

    # Build lookup: svgElementId -> field index  (or (field_index, option_value) for selects)
    element_id_map: Dict[str, Any] = {}
    for i, field in enumerate(updated_fields):
        el_id = field.get('svgElementId')
        if el_id:
            element_id_map[el_id] = i

        if field.get('type') == 'select':
            for opt in field.get('options', []):
                opt_el_id = opt.get('svgElementId')
                if opt_el_id:
                    element_id_map[opt_el_id] = (i, opt.get('value'))

    print(f"[SVG-Sync] Element ID Map keys: {list(element_id_map.keys())}")

    for idx, patch in enumerate(patches):
        p_id  = patch.get('id')
        p_attr = patch.get('attribute')
        p_val  = patch.get('value')

        if not p_id:
            continue

        print(f"[SVG-Sync] Patch {idx}: ID={p_id}, ATTR={p_attr}, VAL={p_val}")

        # ------------------------------------------------------------------ #
        # A. innerText update — just update the field's stored text value     #
        # ------------------------------------------------------------------ #
        if p_attr == 'innerText':
            match_idx = element_id_map.get(p_id)

            if match_idx is None:
                # Fallback: case-insensitive match
                p_id_lower = p_id.lower()
                for key in element_id_map:
                    if key.lower() == p_id_lower:
                        match_idx = element_id_map[key]
                        print(f"[SVG-Sync]   Case-insensitive match: '{key}'")
                        break

            if match_idx is not None:
                if isinstance(match_idx, tuple):  # Select option
                    field_idx, opt_val = match_idx
                    field = updated_fields[field_idx]
                    for opt in field.get('options', []):
                        if opt.get('value') == opt_val:
                            opt['displayText'] = p_val
                            opt['label'] = p_val
                            modified = True
                else:  # Regular field
                    field = updated_fields[match_idx]
                    print(f"[SVG-Sync]   Text: '{field.get('defaultValue')}' → '{p_val}'")
                    field['defaultValue'] = p_val
                    field['currentValue'] = p_val
                    modified = True
            else:
                print(f"[SVG-Sync]   WARNING: No field found for element ID '{p_id}'")

        # ------------------------------------------------------------------ #
        # B. ID update — re-parse metadata directly from the NEW id string.   #
        #                                                                      #
        # Strategy:                                                            #
        #  1. Find the existing form_field by old_id (p_id)                   #
        #  2. Call parse_field_from_id(new_id, existing_text) to get          #
        #     the new field definition (generationRule, type, max, etc.)      #
        #  3. Update the form_field in-place — preserving currentValue so     #
        #     the user's data is not wiped                                     #
        # ------------------------------------------------------------------ #
        elif p_attr == 'id':
            old_id = p_id
            new_id = str(p_val)
            print(f"[SVG-Sync]   ID change: '{old_id}' → '{new_id}'")

            # 1. Find existing field by the old svgElementId
            orig_field_idx = element_id_map.get(old_id)
            if isinstance(orig_field_idx, tuple):
                orig_field_idx = None  # It's a select option — skip (select rebuilds need full parse)

            # Preserve existing text content so it isn't lost on a metadata-only ID change
            existing_text = ""
            if orig_field_idx is not None:
                existing_text = str(
                    updated_fields[orig_field_idx].get('defaultValue') or
                    updated_fields[orig_field_idx].get('currentValue') or ""
                )

            # 2. Parse the NEW id to get fresh field metadata
            new_field_data = parse_field_from_id(new_id, existing_text)

            if new_field_data:
                base_id = new_field_data['id']

                # Check if a field with the target base id already exists
                target_field_idx = None
                for i, f in enumerate(updated_fields):
                    if f.get('id') == base_id and i != orig_field_idx:
                        target_field_idx = i
                        break

                # 3. Apply the update
                if orig_field_idx is not None:
                    existing_current_value = updated_fields[orig_field_idx].get('currentValue')
                    updated_fields[orig_field_idx].update(new_field_data)
                    # Preserve the user's filled-in value through a metadata change
                    if existing_current_value is not None:
                        updated_fields[orig_field_idx]['currentValue'] = existing_current_value
                    print(f"[SVG-Sync]   Updated field '{base_id}': "
                          f"type={new_field_data.get('type')}, "
                          f"generationRule={new_field_data.get('generationRule')}")
                    modified = True
                elif target_field_idx is not None:
                    # Merge into already-existing field with the same base id
                    existing_current_value = updated_fields[target_field_idx].get('currentValue')
                    updated_fields[target_field_idx].update(new_field_data)
                    if existing_current_value is not None:
                        updated_fields[target_field_idx]['currentValue'] = existing_current_value
                    modified = True
                else:
                    # Brand-new field — add it
                    updated_fields.append(new_field_data)
                    print(f"[SVG-Sync]   Added new field '{base_id}'")
                    modified = True
            else:
                # new_id no longer maps to a valid field — remove the old one
                if orig_field_idx is not None and not isinstance(orig_field_idx, tuple):
                    removed = updated_fields.pop(orig_field_idx)
                    print(f"[SVG-Sync]   Removed field '{removed.get('id')}' (new id has no field extension)")
                    modified = True

    print(f"[SVG-Sync] Done. modified={modified}, total fields={len(updated_fields)}")
    return updated_fields, modified
