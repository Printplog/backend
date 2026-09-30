from lxml import etree
from django.test import SimpleTestCase

from api.svg_updater import update_svg_from_field_updates


class SvgTextLayoutTests(SimpleTestCase):
    def test_mixed_direct_text_and_tspans_keep_the_authored_baseline(self):
        svg = """<svg xmlns="http://www.w3.org/2000/svg">
          <style>.address { font-size: 64px; }</style>
          <text id="Address.textarea" class="address" transform="matrix(.339 -.02 .02 .339 370 500)">Old line one<tspan x="0" dy="78">Old line two</tspan><tspan x="0" dy="78">Old line three</tspan></text>
        </svg>"""
        value = "19 Washington Square\nNew York, NY 10011\nUSA"
        fields = [{
            "id": "Address",
            "type": "textarea",
            "svgElementId": "Address.textarea",
            "currentValue": value,
        }]

        output, _ = update_svg_from_field_updates(
            svg, fields, [{"id": "Address", "value": value}]
        )
        root = etree.fromstring(output.encode())
        text = root.xpath('//*[@id="Address.textarea"]')[0]
        spans = text.xpath('./*[local-name()="tspan"]')

        self.assertEqual(text.get("transform"), "matrix(.339 -.02 .02 .339 370 500)")
        self.assertEqual(text.text, "19 Washington Square")
        self.assertEqual([span.text for span in spans], ["New York, NY 10011", "USA"])
        self.assertEqual([span.get("dy") for span in spans], ["78", "78"])

    def test_tspan_first_text_keeps_its_coordinates(self):
        svg = """<svg xmlns="http://www.w3.org/2000/svg">
          <text id="Name.text"><tspan x="40" y="90" class="line">Old</tspan></text>
        </svg>"""
        fields = [{"id": "Name", "type": "text", "svgElementId": "Name.text"}]

        output, _ = update_svg_from_field_updates(
            svg, fields, [{"id": "Name", "value": "Updated"}]
        )
        span = etree.fromstring(output.encode()).xpath('//*[local-name()="tspan"]')[0]

        self.assertEqual(span.text, "Updated")
        self.assertEqual(span.get("x"), "40")
        self.assertEqual(span.get("y"), "90")
        self.assertEqual(span.get("class"), "line")

    def test_tspan_first_text_repairs_an_overlapping_authored_dy(self):
        svg = """<svg xmlns="http://www.w3.org/2000/svg">
          <style>.cls-6 { fill: black; } .cls-6 { font-size: 62.864px; font-family: Arial; }</style>
          <text id="Address.textarea" class="cls-6" transform="matrix(.345 -.02 .02 .345 400 596)">
            <tspan x="0">Old line one</tspan><tspan x="0" dy="18.2">Old line two</tspan>
          </text>
        </svg>"""
        value = "0000 IDGOD.PH\nIDGOD.PH, WY 00000"
        fields = [{"id": "Address", "type": "textarea", "svgElementId": "Address.textarea"}]

        output, _ = update_svg_from_field_updates(
            svg, fields, [{"id": "Address", "value": value}]
        )
        text = etree.fromstring(output.encode()).xpath('//*[@id="Address.textarea"]')[0]
        spans = text.xpath('./*[local-name()="tspan"]')

        self.assertEqual(text.get("transform"), "matrix(.345 -.02 .02 .345 400 596)")
        self.assertIsNone(spans[0].get("dy"))
        self.assertAlmostEqual(float(spans[1].get("dy")), 62.864 * 1.2, places=4)
