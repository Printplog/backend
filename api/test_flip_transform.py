from django.test import SimpleTestCase, override_settings

from api.svg_parser import parse_svg_to_form_fields
from api.svg_updater import update_svg_from_field_updates
from api.svg_validator import validate_svg_id


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class FlipTransformTests(SimpleTestCase):
    """Flip is a Transform-panel feature stored in the transform attribute,
    exactly like rotate. It is NOT part of the ID syntax."""

    def test_validator_rejects_flip_ids(self):
        for element_id in [
            "Title.text.flip_h",
            "Title.text.flip_v",
            "Title.text.flip_h.flip_v",
            "Photo.upload.flip_h",
        ]:
            with self.subTest(element_id=element_id):
                valid, _ = validate_svg_id(element_id)
                self.assertFalse(valid)

    def test_parser_sets_no_flip_flags(self):
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<text id="Title.text" x="10" y="20">Hi</text>'
            "</svg>"
        )
        fields = {field["id"]: field for field in parse_svg_to_form_fields(svg)}
        self.assertIsNone(fields["Title"].get("flipHorizontal"))
        self.assertIsNone(fields["Title"].get("flipVertical"))

    def test_updater_preserves_baked_in_flip_transform(self):
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<text id="Title.text" x="10" y="20" transform="translate(20, 0) scale(-1, 1)">Hi</text>'
            '<image id="Photo.upload" x="0" y="0" width="100" height="50" '
            'transform="translate(0, 50) scale(1, -1)" />'
            "</svg>"
        )
        fields = parse_svg_to_form_fields(svg)
        updated, _ = update_svg_from_field_updates(svg, fields, [{"id": "Title", "value": "Hi"}])
        self.assertIn("translate(20, 0) scale(-1, 1)", updated)
        self.assertIn("translate(0, 50) scale(1, -1)", updated)
