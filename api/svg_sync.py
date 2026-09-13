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

    Uses field ID strings (not list indices) as lookup keys to avoid stale-index
    bugs when fields are added/removed during iteration.
    """
    form_fields = instance.form_fields or []
    if not patches:
        return form_fields, False

    working_fields: List[Dict] = json.loads(json.dumps(form_fields))
    modified = False

    print(f"[SVG-Sync] Processing {len(patches)} patches for instance: {instance.id}")

    # Index by field ID for O(1) access — eliminates stale list-index issues.
    fields_by_id: Dict[str, Dict] = {}
    fields_order: List[str] = []
    for field in working_fields:
        fid = field.get('id')
        if fid:
            fields_by_id[fid] = field
            fields_order.append(fid)

    # Map svgElementId → field_id string  (or (field_id, option_value) for select options)
    element_id_map: Dict[str, Any] = {}
    for field in working_fields:
        el_id = field.get('svgElementId')
        fid = field.get('id')
        if el_id and fid:
            element_id_map[el_id] = fid
        if field.get('type') == 'select':
            for opt in field.get('options', []):
                opt_el_id = opt.get('svgElementId')
                if opt_el_id and fid:
                    element_id_map[opt_el_id] = (fid, opt.get('value'))

    print(f"[SVG-Sync] Element ID Map keys: {list(element_id_map.keys())}")

    for patch_idx, patch in enumerate(patches):
        p_id   = patch.get('id')
        p_attr = patch.get('attribute')
        p_val  = patch.get('value')

        if not p_id:
            continue

        print(f"[SVG-Sync] Patch {patch_idx}: ID={p_id}, ATTR={p_attr}, VAL={p_val}")

        # ------------------------------------------------------------------ #
        # A. innerText update — update stored text value                      #
        # ------------------------------------------------------------------ #
        if p_attr == 'innerText':
            match = element_id_map.get(p_id)

            if match is None:
                # Fallback: case-insensitive match
                p_id_lower = p_id.lower()
                for key in element_id_map:
                    if key.lower() == p_id_lower:
                        match = element_id_map[key]
                        print(f"[SVG-Sync]   Case-insensitive match: '{key}'")
                        break

            if match is not None:
                if isinstance(match, tuple):  # Select option
                    field_id, opt_val = match
                    field = fields_by_id.get(field_id)
                    if field:
                        for opt in field.get('options', []):
                            if opt.get('value') == opt_val:
                                opt['displayText'] = p_val
                                opt['label'] = p_val
                                modified = True
                else:  # Regular field
                    field = fields_by_id.get(match)
                    if field:
                        print(f"[SVG-Sync]   Text: '{field.get('defaultValue')}' → '{p_val}'")
                        field['defaultValue'] = p_val
                        field['currentValue'] = p_val
                        modified = True
            else:
                print(f"[SVG-Sync]   WARNING: No field found for element ID '{p_id}'")

        # ------------------------------------------------------------------ #
        # B. ID update — re-parse metadata from the NEW id string            #
        # ------------------------------------------------------------------ #
        elif p_attr == 'id':
            old_id = p_id
            new_id = str(p_val)
            print(f"[SVG-Sync]   ID change: '{old_id}' → '{new_id}'")

            orig_match = element_id_map.get(old_id)
            # Robust detection: check map AND string pattern
            is_select_pattern = ".select_" in old_id or ".select_" in new_id
            was_select_option = isinstance(orig_match, tuple) or is_select_pattern

            if was_select_option:
                if isinstance(orig_match, tuple):
                    field_id, old_opt_val = orig_match
                else:
                    # Fallback: extract field_id from ID string
                    field_id = old_id.split('.')[0]
                    old_opt_val = None # Will find it by svgElementId
                
                new_parts = new_id.split('.')
                
                # Extract new base ID and new option label
                new_base_id = new_parts[0]
                new_opt_ext = next((p for p in new_parts if p.startswith("select_")), None)
                new_opt_label = new_opt_ext[7:].replace("_", " ") if new_opt_ext else None

                if not new_opt_label:
                    print(f"[SVG-Sync]   WARNING: New ID '{new_id}' is not a valid select option. Skipping.")
                    continue

                print(f"[SVG-Sync]   Select option update: field='{field_id}', ID: '{old_id}' → '{new_id}', New Label: '{new_opt_label}'")
                
                old_field = fields_by_id.get(field_id)
                if old_field and old_field.get('type') == 'select':
                    # Find and update the option in the old field
                    found = False
                    for opt in old_field.get('options', []):
                        if opt.get('svgElementId') == old_id:
                            # Update metadata
                            opt['svgElementId'] = new_id

                            # If value/displayText was exactly the old label (auto-generated),
                            # then update them to the new label too.
                            # Otherwise, keep them (they represent the text content)
                            old_label = opt.get('label', '')
                            if opt.get('value') == old_label or opt.get('value') == old_id.split('.')[-1].replace('select_', ''):
                                opt['value'] = new_opt_label
                            if opt.get('displayText') == old_label:
                                opt['displayText'] = new_opt_label

                            opt['label'] = new_opt_label
                            found = True
                            modified = True
                            print(f"[SVG-Sync]   Updated option '{old_label}' → '{new_opt_label}'")
                            break

                    # Propagate modifiers from the new option ID to the parent select field.
                    # Any option can carry .editable or .track_ROLE — these belong to the whole field.
                    if found:
                        if 'editable' in new_parts:
                            old_field['editable'] = True
                            print(f"[SVG-Sync]   Propagated editable=True to select field '{field_id}'")
                        track_part = next((p for p in new_parts if p.startswith('track_')), None)
                        if track_part:
                            tracking_role = track_part[6:]  # strip 'track_'
                            old_field['trackingRole'] = tracking_role
                            print(f"[SVG-Sync]   Propagated trackingRole='{tracking_role}' to select field '{field_id}'")
                    
                    if found and new_base_id != field_id:
                        # Move option to a different parent field if base ID changed
                        new_field = fields_by_id.get(new_base_id)
                        if new_field and new_field.get('type') == 'select':
                            # Extract the option we just updated from the old field
                            moved_opt = None
                            options = old_field.get('options', [])
                            for i, o in enumerate(options):
                                if o.get('svgElementId') == new_id:
                                    moved_opt = options.pop(i)
                                    break
                            
                            if moved_opt:
                                if 'options' not in new_field: new_field['options'] = []
                                new_field['options'].append(moved_opt)
                                print(f"[SVG-Sync]   Moved option to new parent field: '{new_base_id}'")
                
                continue

            orig_field_id = orig_match if not was_select_option else None  # str or None

            existing_text = ""
            if orig_field_id:
                existing_field = fields_by_id.get(orig_field_id)
                if existing_field:
                    existing_text = str(
                        existing_field.get('defaultValue') or
                        existing_field.get('currentValue') or ""
                    )

            new_field_data = parse_field_from_id(new_id, existing_text)

            if new_field_data:
                # Optional mask metadata must also be cleared when .mask is removed.
                new_field_data.setdefault('maskSource', None)
                base_id = new_field_data['id']
                target_field = fields_by_id.get(base_id)

                if orig_field_id is not None:
                    # Update the existing field in-place (or rename it)
                    saved_current = fields_by_id[orig_field_id].get('currentValue')

                    if orig_field_id != base_id:
                        # Base ID changed — move to new key
                        del fields_by_id[orig_field_id]
                        order_idx = fields_order.index(orig_field_id)
                        fields_order[order_idx] = base_id
                        fields_by_id[base_id] = new_field_data
                    else:
                        fields_by_id[base_id].update(new_field_data)

                    if saved_current is not None:
                        fields_by_id[base_id]['currentValue'] = saved_current

                    print(f"[SVG-Sync]   Updated field '{base_id}': "
                          f"type={new_field_data.get('type')}, "
                          f"generationRule={new_field_data.get('generationRule')}")
                    modified = True

                elif target_field is not None:
                    # No old field, but a field with this base_id already exists.
                    # Guard: never overwrite a select field with a non-select type —
                    # that would silently destroy the options list (needs full reparse).
                    if target_field.get('type') == 'select' and new_field_data.get('type') != 'select':
                        print(f"[SVG-Sync]   Skipping merge: won't overwrite select '{base_id}' "
                              f"with type={new_field_data.get('type')} (needs full reparse)")
                    else:
                        saved_current = target_field.get('currentValue')
                        target_field.update(new_field_data)
                        if saved_current is not None:
                            target_field['currentValue'] = saved_current
                        modified = True

                else:
                    # Brand-new field
                    fields_by_id[base_id] = new_field_data
                    fields_order.append(base_id)
                    print(f"[SVG-Sync]   Added new field '{base_id}'")
                    modified = True

            else:
                # new_id no longer maps to a valid field — remove the old one
                if orig_field_id and orig_field_id in fields_by_id:
                    del fields_by_id[orig_field_id]
                    fields_order.remove(orig_field_id)
                    print(f"[SVG-Sync]   Removed field '{orig_field_id}' "
                          f"(new id '{new_id}' has no field extension)")
                    modified = True

    updated_fields = [fields_by_id[fid] for fid in fields_order if fid in fields_by_id]
    print(f"[SVG-Sync] Done. modified={modified}, total fields={len(updated_fields)}")
    return updated_fields, modified
