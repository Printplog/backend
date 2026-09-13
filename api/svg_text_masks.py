"""Disposable SVG text masks. Keep parity with frontend svgTextMasks.ts."""
import copy
import json
import math
import re
from lxml import etree

NS = 'http://www.w3.org/2000/svg'
GENERATED = 'data-st-mask-generated'
HIDDEN = 'data-st-mask-source-style'


def inverse_transform(value):
    operations = re.findall(r'([a-zA-Z]+)\s*\(([^)]*)\)', value)
    if re.sub(r'[\s,]', '', re.sub(r'([a-zA-Z]+)\s*\(([^)]*)\)', '', value)):
        raise ValueError('Text masks require SVG transform attributes.')
    result = []
    for name, raw in reversed(operations):
        n = [float(v) for v in re.split(r'[\s,]+', raw.strip()) if v]
        if not all(math.isfinite(v) for v in n):
            raise ValueError('Invalid mask transform.')
        if name == 'translate' and len(n) in (1, 2):
            result.append(f'translate({-n[0]} {-n[1] if len(n) == 2 else 0})')
        elif name == 'scale' and len(n) in (1, 2) and all(n):
            result.append(f'scale({1/n[0]} {1/n[-1]})')
        elif name == 'rotate' and len(n) in (1, 3):
            result.append(f'rotate({-n[0]}' + (f' {n[1]} {n[2]}' if len(n) == 3 else '') + ')')
        elif name in ('skewX', 'skewY') and len(n) == 1:
            result.append(f'{name}({-n[0]})')
        elif name == 'matrix' and len(n) == 6:
            a,b,c,d,e,f = n
            det = a*d-b*c
            if not det:
                raise ValueError('Singular mask transform.')
            result.append('matrix(' + ' '.join(map(str, [d/det,-b/det,-c/det,a/det,(c*f-d*e)/det,(b*e-a*f)/det])) + ')')
        else:
            raise ValueError('Unsupported or singular mask transform.')
    return ' '.join(result)


def clear_text_masks(root):
    for el in root.xpath(f'.//*[@{HIDDEN}]'):
        style = json.loads(el.attrib.pop(HIDDEN))
        if style is None:
            el.attrib.pop('style', None)
        else:
            el.set('style', style)
    for el in root.xpath(f'.//*[@{GENERATED}="wrapper"]'):
        parent = el.getparent()
        for child in list(el):
            parent.insert(parent.index(el), child)
        parent.remove(el)
    for el in root.xpath(f'.//*[@{GENERATED}="definition"]'):
        el.getparent().remove(el)


def apply_text_masks(root):
    clear_text_masks(root)
    layers = [el for el in root.iter() if el.get('id') and not any(
        etree.QName(p).localname in ('defs', 'mask', 'clipPath') for p in el.iterancestors())]
    sources = set()
    for index, image in enumerate(layers):
        part = next((p for p in re.split(r'\.(?![^(]*\))', image.get('id', '')) if p.startswith('mask_')), None)
        if not part:
            continue
        if etree.QName(image).localname != 'image':
            raise ValueError('Text masks can only be applied to image layers.')
        source_id = part[5:]
        matches = [el for el in layers if el.get('id').split('.')[0] == source_id]
        if len(matches) != 1 or etree.QName(matches[0]).localname != 'text':
            raise ValueError(f'Mask source "{source_id}" must identify one text layer.')
        source = matches[0]
        inverse = ' '.join(inverse_transform(p.get('transform', '')) for p in image.iterancestors() if p is not root)
        clone = copy.deepcopy(source)
        for p in source.iterancestors():
            if p is root:
                break
            if etree.QName(p).localname != 'g':
                raise ValueError('Text mask sources must be in SVG groups, not nested viewports.')
            group = etree.Element(p.tag, dict(p.attrib))
            group.append(clone)
            clone = group
        for el in clone.iter():
            el.attrib.pop('id', None)
            el.attrib.pop('data-internal-id', None)
            el.attrib.pop('data-name', None)
        mask_id = f'st-text-mask-{index}'
        existing = {el.get('id') for el in root.iter()}
        while mask_id in existing:
            mask_id += '-'
        mask = etree.SubElement(root, f'{{{NS}}}mask', id=mask_id, style='mask-type:alpha', maskContentUnits='userSpaceOnUse')
        mask.set(GENERATED, 'definition')
        projection = etree.SubElement(mask, f'{{{NS}}}g', transform=inverse)
        projection.append(clone)
        wrapper = etree.Element(f'{{{NS}}}g', mask=f'url(#{mask_id})')
        wrapper.set(GENERATED, 'wrapper')
        image.getparent().replace(image, wrapper)
        wrapper.append(image)
        sources.add(source)
    for source in sources:
        source.set(HIDDEN, json.dumps(source.get('style')))
        source.set('style', source.get('style', '') + ';display:none!important')
