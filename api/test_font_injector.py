from dataclasses import dataclass

from django.test import SimpleTestCase

from api.font_injector import inject_fonts_into_svg


@dataclass
class FakeFontFile:
    name: str
    url: str

    def __bool__(self):
        return True


@dataclass
class FakeFont:
    id: str
    name: str
    family: str
    weight: str
    style: str
    font_file: FakeFontFile

    def get_font_format(self):
        return "truetype"


def make_font(
    font_id="inter-400",
    name="Inter Regular",
    family="Inter",
    weight="400",
    style="normal",
    url="/fonts/inter-regular.ttf",
):
    return FakeFont(
        id=font_id,
        name=name,
        family=family,
        weight=weight,
        style=style,
        font_file=FakeFontFile(name=url, url=url),
    )


class FontInjectorTests(SimpleTestCase):
    def test_injects_same_family_variants_with_distinct_weight_and_style(self):
        svg = '<svg><text style="font-family: Inter; font-weight:700;">Hello</text></svg>'
        result = inject_fonts_into_svg(
            svg,
            [
                make_font(),
                make_font("inter-700", "Inter Bold", "Inter", "700", "normal", "/fonts/inter-bold.ttf"),
                make_font("inter-italic", "Inter Italic", "Inter", "400", "italic", "/fonts/inter-italic.ttf"),
            ],
            base_url="https://cdn.test",
            embed_base64=False,
        )

        self.assertEqual(result.count('font-family: "Inter";'), 3)
        self.assertIn("font-weight: 400;", result)
        self.assertIn("font-weight: 700;", result)
        self.assertIn("font-style: italic;", result)
        self.assertIn('url("https://cdn.test/fonts/inter-bold.ttf")', result)

    def test_sibling_faces_sharing_family_and_weight_stay_separate(self):
        svg = (
            '<svg>'
            '<text style="font-family: Arial;">Regular</text>'
            "<text style=\"font-family: 'Arial Black';\">Black</text>"
            "<text style=\"font-family: 'Arial Bold';\">Bold</text>"
            "</svg>"
        )
        result = inject_fonts_into_svg(
            svg,
            [
                make_font("arial", "Arial", "Arial", "normal", "normal", "/fonts/arial.ttf"),
                make_font("arial-black", "Arial Black", "Arial", "normal", "normal", "/fonts/arial-black.ttf"),
                make_font("arial-bold", "Arial Bold", "Arial", "normal", "normal", "/fonts/arial-bold.ttf"),
            ],
            base_url="https://cdn.test",
            embed_base64=False,
        )

        self.assertIn('font-family: "Arial";', result)
        self.assertIn('font-family: "Arial Black";', result)
        self.assertIn('font-family: "Arial Bold";', result)
        self.assertIn('url("https://cdn.test/fonts/arial.ttf")', result)
        self.assertIn('url("https://cdn.test/fonts/arial-black.ttf")', result)
        self.assertIn('url("https://cdn.test/fonts/arial-bold.ttf")', result)

    def test_font_without_family_falls_back_to_name_without_crashing(self):
        svg = "<svg><text>Hello</text></svg>"
        result = inject_fonts_into_svg(
            svg,
            [make_font(name="Fancy Bold", family="", weight="700", url="/fonts/fancy-bold.ttf")],
            base_url="https://cdn.test",
            embed_base64=False,
        )

        self.assertIn('font-family: "Fancy Bold";', result)
        self.assertIn("font-weight: 700;", result)
