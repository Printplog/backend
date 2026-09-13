from pathlib import Path
from xml.etree import ElementTree as ET

from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import ValidationError

from api.document_compiler import compile_document_fields, generate_value
from api.svg_parser import parse_svg_to_form_fields
from api.svg_updater import update_svg_from_field_updates
from api.svg_validator import validate_svg_id


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class ReverseGenerationTests(SimpleTestCase):
    def test_values_and_legacy_rules(self):
        for source, expected in [("LOVE", "EVOL"), ("Hello world!", "!dlrow olleH"),
                                 ("", ""), (0, "0"), (False, "eslaf"), ("A😀B", "B😀A")]:
            with self.subTest(source=source):
                self.assertEqual(generate_value("AUTO:(dep_Message[reverse])", {"Message": source}), expected)
        self.assertEqual(generate_value("(dep_Missing[reverse])", {}), "")
        self.assertEqual(generate_value("(dep_Message[w1][reverse])", {"Message": "LOVE YOU"}), "EVOL")
        self.assertEqual(generate_value("(dep_Message[ch1-3][reverse])", {"Message": "LOVE"}), "VOL")
        self.assertEqual(generate_value("X_(dep_Message[reverse])", {"Message": "LOVE"}, 4), "X EV")
        self.assertEqual(generate_value("(dep_Message[ch4,3,2,1])", {"Message": "LOVE"}), "EVOL")

    def test_svg_parse_compile_and_update(self):
        svg = (Path(__file__).parent / "fixtures/reverse_text.svg").read_text()
        fields = parse_svg_to_form_fields(svg)
        for field in fields:
            valid, error = validate_svg_id(field["svgElementId"])
            self.assertTrue(valid, error)
        for source in ["LOVE", "HELLO WORLD", "", "A😀B"]:
            with self.subTest(source=source):
                compiled, _ = compile_document_fields(fields, {"Message": source})
                values = {field["id"]: field["currentValue"] for field in compiled}
                self.assertEqual(values["Reversed"], source[::-1])
                self.assertEqual(values["Chain"], source[::-1])
                updated, _ = update_svg_from_field_updates(svg, compiled, [
                    {"id": field["id"], "value": field["currentValue"]} for field in compiled
                ])
                root = ET.fromstring(updated)
                rendered = {el.get("id").split(".")[0]: "".join(el.itertext())
                            for el in root.iter() if el.get("id")}
                self.assertEqual(rendered["Reversed"], source[::-1])
                self.assertEqual(rendered["Message"], source)

    def test_circular_references(self):
        fields = [{"id": "A", "type": "gen", "generationRule": "AUTO:(dep_B[reverse])"},
                  {"id": "B", "type": "gen", "generationRule": "AUTO:(dep_A[reverse])"}]
        with self.assertRaisesMessage(ValidationError, "Circular generation reference"):
            compile_document_fields(fields, {})
