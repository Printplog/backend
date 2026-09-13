from pathlib import Path
from django.test import TestCase, override_settings
from lxml import etree
from api.models import Template
from api.serializers.templates import AdminTemplateSerializer
from api.svg_utils import apply_svg_patches, merge_svg_patches

SVG = (Path(__file__).parent / 'fixtures/text_mask_playground.svg').read_text()
NS = {'s': 'http://www.w3.org/2000/svg'}

@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'}}, CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class MaskPersistenceTests(TestCase):
    def test_save_reload_repeated_mask_changes_and_image_replacement(self):
        template = Template(name='Mask persistence', type='design')
        template._raw_svg_data = SVG
        template.save()
        template = Template.objects.get(pk=template.pk)
        current = 'Photo.upload'
        for target in ['Photo.upload.mask_Title', 'Photo.upload', 'Photo.upload.mask_Title', 'Photo.upload']:
            incoming = template.svg_patches + [
                {'id': current, 'attribute': 'id', 'value': target},
                {'id': target, 'attribute': 'href', 'value': 'new.png'},
            ]
            serializer = AdminTemplateSerializer(template, data={'svg_patch': incoming}, partial=True)
            self.assertTrue(serializer.is_valid(), serializer.errors)
            serializer.save()
            # Fresh model and file reads simulate reload; no editor/store state survives.
            template = Template.objects.get(pk=template.pk)
            with template.svg_file.open('r') as asset:
                output = apply_svg_patches(asset.read(), template.svg_patches)
            root = etree.fromstring(output.encode())
            image = root.xpath('.//s:image', namespaces=NS)[0]
            self.assertEqual(image.get('id'), target)
            self.assertEqual(image.get('href'), 'new.png')
            photo = next(f for f in template.form_fields if f['id'] == 'Photo')
            self.assertEqual(photo.get('maskSource'), 'Title' if '.mask_' in target else None)
            current = target

    def test_legacy_rename_chain_also_applies_following_attributes(self):
        patches = [
            {'id': 'Photo.upload', 'attribute': 'id', 'value': 'Photo.upload.mask_Title'},
            {'id': 'Photo.upload.mask_Title', 'attribute': 'id', 'value': 'Photo.upload'},
            {'id': 'Photo.upload.mask_Title', 'attribute': 'href', 'value': 'new.png'},
        ]
        for saved in (patches, merge_svg_patches(patches)):
            root = etree.fromstring(apply_svg_patches(SVG, saved).encode())
            image = root.xpath('.//s:image', namespaces=NS)[0]
            self.assertEqual(image.get('id'), 'Photo.upload')
            self.assertEqual(image.get('href'), 'new.png')
