from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from api.management.commands.seed_api_demo import DEMO_TEMPLATES
from api.svg_parser import parse_svg_to_form_fields


class ApiDemoTemplateAssetTests(SimpleTestCase):
    def test_real_travel_templates_are_machine_readable(self):
        template_root = Path(settings.BASE_DIR).parent / "templates"
        for specification in DEMO_TEMPLATES:
            with self.subTest(template=specification["name"]):
                svg = (template_root / specification["file"]).read_text(encoding="utf-8")
                fields = parse_svg_to_form_fields(svg)

                self.assertGreaterEqual(len(fields), specification["minimum_fields"])
                self.assertNotIn("<script", svg.lower())
                self.assertTrue(all(field.get("id") and field.get("type") for field in fields))
