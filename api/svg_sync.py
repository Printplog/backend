import json
import logging
from typing import List, Dict, Any, Tuple
import xml.etree.ElementTree as ET
from .svg_parser import process_element_to_field

logger = logging.getLogger(__name__)

def sync_form_fields_with_patches(instance, patches: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Synchronize form_fields with SVG patches.
    Handles innerText updates (fast) and ID updates (requires re-parsing element rules).
    """
    form_fields = instance.form_fields or []
    if not patches:
        return form_fields, False

    updated_fields = json.loads(json.dumps(form_fields))
    modified = False
    
    print(f"[SVG-Sync] Processing {len(patches)} patches for instance: {instance.id}")
    
    # Check if we have ID changes or other structural changes that require re-parsing
    has_structural_changes = any(p.get('attribute') == 'id' for p in patches)
    
    # 1. Faster lookup maps
    # Map from svgElementId -> Field index OR (Field index, Option Value)
    element_id_map = {}
    for i, field in enumerate(updated_fields):
        el_id = field.get('svgElementId')
        if el_id:
            element_id_map[el_id] = i
            
        # Also map select options
        if field.get('type') == 'select':
            for opt in field.get('options', []):
                opt_el_id = opt.get('svgElementId')
                if opt_el_id:
                    element_id_map[opt_el_id] = (i, opt.get('value'))

    print(f"[SVG-Sync] Element ID Map: {list(element_id_map.keys())}")

    # If we have structural changes, we need the SVG tree to extract element properties
    elements_with_ids = {}
    if has_structural_changes:
        print("[SVG-Sync] Structural changes detected (ID updates). Parsing SVG tree...")
        try:
            svg_content = None
            if hasattr(instance, '_raw_svg_data') and instance._raw_svg_data:
                svg_content = instance._raw_svg_data
            elif instance.svg_file:
                instance.svg_file.open()
                svg_content = instance.svg_file.read().decode('utf-8')
            
            if svg_content:
                svg_root = ET.fromstring(svg_content)
                # Map all elements with IDs for fast lookup
                for el in svg_root.iter():
                    el_id = el.get('id')
                    if el_id:
                        elements_with_ids[el_id] = el
                print(f"[SVG-Sync] Found {len(elements_with_ids)} elements with IDs in SVG.")
        except Exception as e:
            logger.error(f"[SVG-Sync] Failed to parse SVG for sync: {e}")

    for idx, patch in enumerate(patches):
        p_id = patch.get('id')
        p_attr = patch.get('attribute')
        p_val = patch.get('value')
        
        if not p_id: continue
        
        print(f"[SVG-Sync] Patch {idx}: ID={p_id}, ATTR={p_attr}, VAL={p_val}")

        # A. InnerText Update (The most common one)
        if p_attr == 'innerText':
            # Case-insensitive match as a fallback
            match_idx = element_id_map.get(p_id)
            if match_idx is None:
                print(f"[SVG-Sync]   No exact match for '{p_id}'. Trying case-insensitive...")
                # Try lower case match
                p_id_lower = p_id.lower()
                for key in element_id_map:
                    if key.lower() == p_id_lower:
                        match_idx = element_id_map[key]
                        print(f"[SVG-Sync]   Found case-insensitive match: '{key}'")
                        break

            if match_idx is not None:
                if isinstance(match_idx, tuple): # Select Option
                    field_idx, opt_val = match_idx
                    field = updated_fields[field_idx]
                    print(f"[SVG-Sync]   Match found (Select Option): field={field.get('id')}, value={opt_val}")
                    for opt in field.get('options', []):
                        if opt.get('value') == opt_val:
                            print(f"[SVG-Sync]     Updating option text from '{opt.get('displayText')}' to '{p_val}'")
                            opt['displayText'] = p_val
                            opt['label'] = p_val
                            modified = True
                else: # Regular Field
                    field = updated_fields[match_idx]
                    print(f"[SVG-Sync]   Match found (Field): field={field.get('id')}")
                    print(f"[SVG-Sync]     Updating field value from '{field.get('defaultValue')}' to '{p_val}'")
                    field['defaultValue'] = p_val
                    field['currentValue'] = p_val
                    modified = True
            else:
                print(f"[SVG-Sync]   WARNING: No field found for element ID '{p_id}'")
                    
        # B. ID Update (The "Regeneration" piece)
        elif p_attr == 'id' and elements_with_ids:
            old_id = p_id
            new_id = p_val
            print(f"[SVG-Sync]   Handling ID change: {old_id} -> {new_id}")
            
            # Find the element in the tree
            element = elements_with_ids.get(new_id) or elements_with_ids.get(old_id)

            
            if element is not None:
                # Re-run the parser logic for this specific element
                t_fields = []
                t_select_map = {}
                process_element_to_field(element, t_fields, t_select_map)
                
                if t_fields:
                    new_field_data = t_fields[0]
                    base_id = new_field_data['id']
                    
                    # 1. Find existing field by SVG Element ID (the one we are patching)
                    orig_field_idx = None
                    for i, f in enumerate(updated_fields):
                        if f.get('svgElementId') == old_id:
                            orig_field_idx = i
                            break
                    
                    # 2. Find if a field with the target ID already exists (for select fields)
                    target_field_idx = None
                    for i, f in enumerate(updated_fields):
                        if f.get('id') == base_id:
                            target_field_idx = i
                            break

                    if new_field_data.get('type') == 'select':
                        # Handle Select Option update/addition
                        new_opt = new_field_data['options'][0]
                        
                        if target_field_idx is not None:
                            # Existing select field found, update/add option
                            field = updated_fields[target_field_idx]
                            options = field.get('options', [])
                            
                            # Replace if same SVG ID or same value
                            found = False
                            for j, opt in enumerate(options):
                                if opt.get('svgElementId') == old_id or opt.get('svgElementId') == new_id:
                                    options[j] = new_opt
                                    found = True
                                    break
                            
                            if not found:
                                options.append(new_opt)
                            
                            field['options'] = options
                            # If we moved from a regular field to a select option, remove the old regular field
                            if orig_field_idx is not None and orig_field_idx != target_field_idx:
                                updated_fields.pop(orig_field_idx)
                            
                            modified = True
                        else:
                            # New select field
                            updated_fields.append(new_field_data)
                            modified = True
                    else:
                        # Regular field update or addition
                        if orig_field_idx is not None:
                            # Update existing field's properties
                            updated_fields[orig_field_idx].update(new_field_data)
                            modified = True
                        else:
                            # New regular field
                            updated_fields.append(new_field_data)
                            modified = True
                else:
                    # Element is no longer a field according to rules
                    if old_id in element_id_map:
                        idx = element_id_map[old_id]
                        if not isinstance(idx, tuple):
                            updated_fields.pop(idx)
                            modified = True
                            print(f"[SVG-Sync] Removed field '{old_id}' as it no longer matches parser rules.")

    print(f"[SVG-Sync] Done. Modified: {modified}")
    return updated_fields, modified

