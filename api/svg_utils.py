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
                logger.warning("[SVG-Patcher] Reorder reference not found for %s", element.get('id'))
                return False

        except Exception as e:
            logger.error("[SVG-Patcher] Reorder error: %s", e)
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
    Ensures deterministic IDs are present before applying.
    """
    if not svg_content:
        return svg_content

    try:
        # Parse SVG
        parser = etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            recover=False,
            remove_blank_text=True,
            huge_tree=False,
        )
        svg_tree = etree.fromstring(svg_content.encode('utf-8'), parser=parser)
        
        # Register namespaces
        namespaces = {k if k is not None else 'svg': v for k, v in svg_tree.nsmap.items()}
        if 'svg' not in namespaces: namespaces['svg'] = 'http://www.w3.org/2000/svg'
        if 'xlink' not in namespaces: namespaces['xlink'] = 'http://www.w3.org/1999/xlink'

        # 1. Ensure all elements have deterministic IDs (matching the Frontend's algorithm)
        # We use data-internal-id as our primary lookup for patches
        id_count = {}
        
        non_editable_tags = [
            'defs', 'style', 'lineargradient', 'radialgradient',
            'pattern', 'clippath', 'mask', 'filter', 'fegaussianblur', 'feoffset', 'feflood', 'fecomposite', 'femerge', 'femergenode'
        ]

        for element in svg_tree.xpath(".//*"):
            tag = etree.QName(element).localname.lower()
            if tag == 'svg': continue
            
            # SKIP logic must match useSvgStore.ts exactly
            if tag in non_editable_tags:
                continue
            
            # Smarter text check matching store's multi-line / tspan handling
            text_parts = []
            if element.text:
                text_parts.append(element.text.strip())
            for child in element:
                if child.text:
                    text_parts.append(child.text.strip())
            
            # Join with newline to match frontend store's join("\n")
            full_text = "\n".join([p for p in text_parts if p]).strip().lower()
            if full_text == "test document":
                continue
            
            existing_id = element.get('id')
            existing_internal_id = element.get('data-internal-id')
            
            base_id = existing_id or existing_internal_id or f"el-{tag}"
            id_count[base_id] = id_count.get(base_id, 0) + 1
            final_id = f"{base_id}_{id_count[base_id]}" if id_count[base_id] > 1 else base_id
            
            element.set('data-internal-id', final_id)

        if not patches:
            return etree.tostring(svg_tree, pretty_print=True, encoding='unicode', xml_declaration=False)

        # 2. Sort patches to apply 'id' attributes FIRST.
        # This ensures that if we are renaming an element, subsequent patches
        # targeting the NEW ID will find it.
        patches_sorted = sorted(
            patches, 
            key=lambda x: 0 if x.get('attribute') == 'id' else 1
        )

        applied_count = 0
        matched_elements = set()  # Track which DOM elements have already been claimed by an 'id' patch

        for patch in patches_sorted:
            element_id = patch.get('id')
            attribute = patch.get('attribute')
            value = patch.get('value')
            
            if not all([element_id, attribute]):
                continue

            # Find the element using multiple possible attributes
            base_id = element_id.split('.')[0] if '.' in element_id else element_id
            
            queries = [
                f".//*[@data-internal-id='{element_id}']",
                f".//*[@id='{element_id}']",
                f".//*[@name='{element_id}']",
                f".//*[@data-name='{element_id}']"
            ]
            
            if '.' in element_id:
                queries.extend([
                    f".//*[@id='{base_id}']",
                    f".//*[@name='{base_id}']",
                    f".//*[@data-name='{base_id}']"
                ])
            
            target_element = None
            for query in queries:
                try:
                    candidates = svg_tree.xpath(query, namespaces=namespaces)
                    # Filter out candidates already matched by a previous 'id' patch
                    unmatched = [c for c in candidates if c not in matched_elements]
                    if unmatched:
                        target_element = unmatched[0]
                        break
                except Exception as xpath_err:
                    logger.warning("[SVG-Patcher] XPath query failed, skipping: %s", xpath_err)
                    continue
            
            if target_element is not None:
                if attribute == 'id':
                    # Special handling for 'id' attribute to ensure uniqueness tracking
                    target_element.set('id', str(value))
                    matched_elements.add(target_element)
                else:
                    # For other attributes, use the general setter
                    set_element_attribute(target_element, attribute, value, namespaces, svg_tree)
                applied_count += 1
            else:
                # Silently skip missing elements during re-upload bakes
                pass

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
