from pathlib import Path
from lxml import etree
from django.test import SimpleTestCase, override_settings
from api.document_compiler import compile_document_fields
from api.svg_parser import parse_svg_to_form_fields
from api.svg_validator import validate_svg_id
from api.svg_updater import update_svg_from_field_updates
from api.svg_text_masks import apply_text_masks, clear_text_masks
from api.rendering import sanitize_svg_for_render

SVG = (Path(__file__).parent / 'fixtures/text_mask.svg').read_text()
NS = {'s':'http://www.w3.org/2000/svg'}


@override_settings(CACHES={'default':{'BACKEND':'django.core.cache.backends.locmem.LocMemCache'}})
class TextMaskTests(SimpleTestCase):
    def test_parse_and_validate(self):
        fields = parse_svg_to_form_fields(SVG)
        self.assertEqual(next(f for f in fields if f['id']=='Photo')['maskSource'], 'Title')
        self.assertTrue(validate_svg_id('Photo.upload.mask_Title')[0])
        self.assertTrue(validate_svg_id('Photo.upload.mask_Title.editable')[0])
        self.assertFalse(validate_svg_id('Photo.text.mask_Title')[0])
        self.assertFalse(validate_svg_id('Photo.upload.mask_')[0])

    def test_mask_source_updates_export_and_removal(self):
        fields = parse_svg_to_form_fields(SVG)
        for text in ('LOVE','HELLO',''):
            output, _ = update_svg_from_field_updates(SVG, fields, [{'id':'Title','value':text}])
            root = etree.fromstring(sanitize_svg_for_render(output).encode())
            self.assertEqual(root.xpath('string(.//s:mask//s:text)',namespaces=NS), text)
            self.assertEqual(len(root.xpath('.//s:image',namespaces=NS)),1)
            apply_text_masks(root)
            self.assertEqual(len(root.xpath('.//s:mask',namespaces=NS)),1)
            root.xpath('.//s:image',namespaces=NS)[0].set('id','Photo.upload')
            apply_text_masks(root)
            self.assertFalse(root.xpath('.//s:mask',namespaces=NS))
            source = root.xpath('.//s:text',namespaces=NS)[0]
            self.assertNotIn('display:none',source.get('style',''))

    def test_shared_source_and_transforms(self):
        root=etree.fromstring(SVG.encode())
        image=root.xpath('.//s:image',namespaces=NS)[0]
        import copy
        extra=copy.deepcopy(image);extra.set('id','Second.upload.mask_Title');root.append(extra)
        apply_text_masks(root)
        self.assertEqual(len(root.xpath('.//s:mask',namespaces=NS)),2)
        self.assertIn('translate(-40.0 ',root.xpath('.//s:mask/s:g',namespaces=NS)[0].get('transform'))
        for text in root.xpath('.//s:mask//s:text',namespaces=NS):
            self.assertNotIn('display:none',text.get('style',''))
        clear_text_masks(root)
        self.assertFalse(root.xpath('.//*[@data-st-mask-source-style]'))

    def test_generated_text_masks(self):
        svg=SVG.replace('Title.text','Title.gen_AUTO:(dep_Message[w1])').replace('</svg>','<text id="Message.text">LOVE YOU</text></svg>')
        fields,_=compile_document_fields(parse_svg_to_form_fields(svg),{})
        output,_=update_svg_from_field_updates(svg,fields,[{'id':f['id'],'value':f['currentValue']} for f in fields])
        root=etree.fromstring(output.encode())
        self.assertEqual(root.xpath('string(.//s:mask//s:text)',namespaces=NS),'LOVE')

    def test_invalid_source(self):
        for source in ('Missing','Photo'):
            with self.assertRaisesRegex(ValueError,'one text layer'):
                apply_text_masks(etree.fromstring(SVG.replace('mask_Title','mask_'+source).encode()))
