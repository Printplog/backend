from lxml import etree
import json
import logging

logger = logging.getLogger(__name__)

def set_element_attribute(element, attribute, value, namespaces, svg_tree):
    """Sets an attribute on an element, handling namespaces and special 'reorder' logic."""
    if attribute == 'innerText':
        # Clear child elements (like tspans) if we are setting plain text
        for child in list(element):
            element.remove(child)
        element.text = str(value)
        return True
    
    if attribute == 'reorder':
        # value is dict with { index: int, afterId: str|None, beforeId: str|None }
        try:
            if not isinstance(value, dict):
                return False
            
            target_after_id = value.get('afterId') # moved element should be BEFORE this
            target_before_id = value.get('beforeId') # moved element should be AFTER this
            
            parent = element.getparent()
            if parent is None:
                return False
                
            # 1. Find reference element
            ref_el = None
            move_after = False
            
            if target_before_id:
                # Try to find the element that should be BEFORE our item
                xpath = f".//*[@id='{target_before_id}']"
                ref_elements = svg_tree.xpath(xpath, namespaces=namespaces)
                if ref_elements:
                    ref_el = ref_elements[0]
                    move_after = True
            
            if not ref_el and target_after_id:
                # Try to find the element that should be AFTER our item
                xpath = f".//*[@id='{target_after_id}']"
                ref_elements = svg_tree.xpath(xpath, namespaces=namespaces)
                if ref_elements:
                    ref_el = ref_elements[0]
                    move_after = False
            
            if ref_el is not None:
                # Move element to the same parent as reference if they differ?
                # For now assume same parent or move to reference's parent
                ref_parent = ref_el.getparent()
                if ref_parent is not None:
                    # Remove and re-insert
                    if element in parent:
                        parent.remove(element)
                    
                    ref_index = ref_parent.index(ref_el)
                    if move_after:
                        ref_parent.insert(ref_index + 1, element)
                    else:
                        ref_parent.insert(ref_index, element)
                    return True
            else:
                # If no reference found, maybe it moved to start or end of its parent?
                # Or we just leave it. Reordering without a reference is risky in complex SVGs.
                print(f"[SVG-Patcher] Reorder reference not found for {element.get('id')}")
                return False
                
        except Exception as e:
            print(f"[SVG-Patcher] Reorder error: {e}")
            return False

    try:
        # Check if value is None or empty string for deletion
        if value is None or value == "":
            if attribute in element.attrib:
                del element.attrib[attribute]
                return True
            return False
            
        if ':' in attribute:
            prefix, attr_name = attribute.split(':', 1)
            if prefix in namespaces:
                ns_uri = namespaces[prefix]
                element.set(f"{{{ns_uri}}}{attr_name}", str(value))
                return True
        
        # Default case
        element.set(attribute, str(value))
        return True
    except Exception as e:
        print(f"[SVG-Patcher] Error setting attribute {attribute}: {e}")
        return False

def apply_svg_patches(svg_content, patches):
    """
    Applies a list of patches to an SVG string.
    """
    if not patches:
        return svg_content

    try:
        # Parse SVG
        parser = etree.XMLParser(recover=True, remove_blank_text=True)
        svg_tree = etree.fromstring(svg_content.encode('utf-8'), parser=parser)
        
        # Register namespaces
        namespaces = {k if k is not None else 'svg': v for k, v in svg_tree.nsmap.items()}
        if 'svg' not in namespaces: namespaces['svg'] = 'http://www.w3.org/2000/svg'
        if 'xlink' not in namespaces: namespaces['xlink'] = 'http://www.w3.org/1999/xlink'

        applied_count = 0
        for patch in patches:
            element_id = patch.get('id')
            attribute = patch.get('attribute')
            value = patch.get('value')
            
            if not all([element_id, attribute]):
                continue

            # Find the element
            xpath_queries = [
                f".//*[@id='{element_id}']",
                f".//svg:*[@id='{element_id}']",
                f".//*[@name='{element_id}']",
                f".//*[@data-name='{element_id}']"
            ]
            
            elements = []
            for query in xpath_queries:
                try:
                    elements = svg_tree.xpath(query, namespaces=namespaces)
                except Exception:
                    continue
                if elements:
                    break

            if elements:
                success = False
                for element in elements:
                    if set_element_attribute(element, attribute, value, namespaces, svg_tree):
                        success = True
                
                if success:
                    applied_count += 1
            else:
                print(f"[SVG-Patcher] Warning: Element not found: {element_id}")

        print(f"[SVG-Patcher] Stats: {applied_count}/{len(patches)} patches applied")
        
        return etree.tostring(svg_tree, pretty_print=True, encoding='unicode', xml_declaration=False)

    except Exception as e:
        print(f"[SVG-Patcher] Fatal error applying patches: {e}")
        return svg_content

def merge_svg_patches(patches):
    """
    Deduplicate patches. Reorder patches are always kept as they are relative.
    """
    merged = {}
    reorder_patches = []
    
    for patch in patches:
        if patch.get('attribute') == 'reorder':
            reorder_patches.append(patch)
            continue
            
        key = (patch.get('id'), patch.get('attribute'))
        if key[0] and key[1]:
            merged[key] = patch
            
    return list(merged.values()) + reorder_patches
