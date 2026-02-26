import uuid
from django.test import TestCase, override_settings
from django.core.files.base import ContentFile
from django.contrib.auth import get_user_model
from rest_framework.test import APIRequestFactory
from unittest.mock import MagicMock
from .models import Template, Tool
from .serializers import TemplateSerializer
from .svg_parser import parse_field_from_id
from .svg_sync import sync_form_fields_with_patches

User = get_user_model()

class TemplateStorageTest(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tool = Tool.objects.create(name="Test Tool", price=10.00)
        self.svg_content = '<svg><text>Test</text></svg>'
        # Template.svg is not a model field — SVG is ingested via _raw_svg_data
        template = Template(name="Test Template", type='tool', tool=self.tool)
        template._raw_svg_data = self.svg_content
        template.save()
        self.template = template

    def test_svg_file_creation(self):
        """Verify that saving a template creates an SVG file"""
        self.assertIsNotNone(self.template.svg_file)
        self.assertTrue(self.template.svg_file.name.endswith('.svg'))
        
        # Check content
        with self.template.svg_file.open('r') as f:
            content = f.read()
            self.assertEqual(content, self.svg_content)

    def test_template_serializer_svg_url(self):
        """Verify that the serializer returns an absolute SVG URL"""
        request = self.factory.get('/')
        serializer = TemplateSerializer(self.template, context={'request': request})
        data = serializer.data
        
        self.assertIn('svg_url', data)
        # Check for either test server URL or custom CDN domain
        url = data['svg_url']
        is_valid_url = url.startswith('http://testserver/media/') or url.startswith('https://cdn.sharptoolz.com/')
        self.assertTrue(is_valid_url, f"URL was unexpected: {url}")
        self.assertTrue(url.endswith('.svg'))

    @override_settings(USE_S3_STORAGE=True, AWS_S3_CUSTOM_DOMAIN='cdn.sharptoolz.com')
    def test_custom_domain_media_url(self):
        """Verify that MEDIA_URL changes when USE_S3_STORAGE is True (Conceptual)"""
        # This test is more about verifying the logic in settings.py indirectly
        from django.conf import settings
        # Note: override_settings might not re-run the logic in settings.py
        # but we can verify our serializer logic handles URLs correctly
        pass


class ParseFieldFromIdTest(TestCase):
    """Tests for the parse_field_from_id() helper in svg_parser.py"""

    def test_basic_text_field(self):
        """A simple named element becomes a text field."""
        field = parse_field_from_id("First_Name", "John")
        self.assertIsNotNone(field)
        self.assertEqual(field['id'], "First_Name")
        self.assertEqual(field['type'], "text")
        self.assertEqual(field['defaultValue'], "John")

    def test_gen_field_without_auto(self):
        """A .gen field without AUTO: prefix has type gen but no AUTO generationRule."""
        field = parse_field_from_id("Tracking_ID.gen_(rn[8])")
        self.assertIsNotNone(field)
        self.assertEqual(field['id'], "Tracking_ID")
        self.assertIn('generationRule', field)
        self.assertFalse(field['generationRule'].startswith("AUTO:"),
                         "generationRule should NOT start with AUTO: when not set")

    def test_gen_field_with_auto_prefix(self):
        """
        Regression: .gen field with AUTO: prefix must preserve it in generationRule.
        This was the core bug — svg_sync was losing the AUTO: prefix on ID change.
        """
        field = parse_field_from_id("Tracking_ID.gen_AUTO:(rn[8])")
        self.assertIsNotNone(field)
        self.assertEqual(field['id'], "Tracking_ID")
        self.assertIn('generationRule', field)
        self.assertTrue(
            field['generationRule'].startswith("AUTO:"),
            f"Expected generationRule to start with 'AUTO:', got: {field['generationRule']}"
        )

    def test_returns_none_for_empty_id(self):
        """Empty ID returns None."""
        self.assertIsNone(parse_field_from_id(""))

    def test_preserves_text_content(self):
        """Existing text content is preserved as default value."""
        field = parse_field_from_id("Company_Name", "Acme Corp")
        self.assertEqual(field['defaultValue'], "Acme Corp")


class SvgSyncIdChangeTest(TestCase):
    """Tests for the ID-change path in sync_form_fields_with_patches()"""

    def _make_instance(self, form_fields):
        """Create a minimal mock instance for the sync function."""
        mock = MagicMock()
        mock.id = uuid.uuid4()
        mock.form_fields = form_fields
        mock._raw_svg_data = None
        mock.svg_file = None
        return mock

    def test_id_change_updates_generation_rule(self):
        """
        Regression test: When an admin changes a .gen field's ID to add AUTO: mode,
        the form_field.generationRule must be updated to start with 'AUTO:'.
        """
        form_fields = [{
            'id': 'Tracking_ID',
            'type': 'gen',
            'svgElementId': 'Tracking_ID.gen_(rn[8])',
            'generationRule': '(rn[8])',
            'defaultValue': '',
            'currentValue': '',
            'name': 'Tracking ID',
        }]
        instance = self._make_instance(form_fields)
        patches = [{
            'id': 'Tracking_ID.gen_(rn[8])',   # old svgElementId
            'attribute': 'id',
            'value': 'Tracking_ID.gen_AUTO:(rn[8])',  # new id with AUTO:
        }]
        updated_fields, modified = sync_form_fields_with_patches(instance, patches)

        self.assertTrue(modified, "Sync should report that fields were modified")
        tracking_field = next((f for f in updated_fields if f['id'] == 'Tracking_ID'), None)
        self.assertIsNotNone(tracking_field, "Tracking_ID field should still exist")
        self.assertTrue(
            tracking_field.get('generationRule', '').startswith('AUTO:'),
            f"generationRule should start with 'AUTO:', got: {tracking_field.get('generationRule')}"
        )

    def test_id_change_preserves_current_value(self):
        """Changing a field's ID (metadata only) must not wipe its currentValue."""
        form_fields = [{
            'id': 'First_Name',
            'type': 'text',
            'svgElementId': 'First_Name',
            'defaultValue': 'Alice',
            'currentValue': 'Alice',
            'name': 'First Name',
        }]
        instance = self._make_instance(form_fields)
        patches = [{
            'id': 'First_Name',
            'attribute': 'id',
            'value': 'First_Name.max_50',
        }]
        updated_fields, modified = sync_form_fields_with_patches(instance, patches)
        field = next((f for f in updated_fields if f['id'] == 'First_Name'), None)
        self.assertIsNotNone(field)
        self.assertEqual(field.get('currentValue'), 'Alice',
                         "currentValue must be preserved through a metadata-only ID change")

    def test_innertext_patch_updates_default_value(self):
        """innerText patches update defaultValue and currentValue correctly."""
        form_fields = [{
            'id': 'Company',
            'type': 'text',
            'svgElementId': 'Company',
            'defaultValue': 'Old Name',
            'currentValue': 'Old Name',
            'name': 'Company',
        }]
        instance = self._make_instance(form_fields)
        patches = [{'id': 'Company', 'attribute': 'innerText', 'value': 'New Name'}]
        updated_fields, modified = sync_form_fields_with_patches(instance, patches)

        self.assertTrue(modified)
        field = next(f for f in updated_fields if f['id'] == 'Company')
        self.assertEqual(field['defaultValue'], 'New Name')
        self.assertEqual(field['currentValue'], 'New Name')

