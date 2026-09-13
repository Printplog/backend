from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

from django.test import SimpleTestCase
from api.views.templates import AdminTemplateViewSet


class TemplateSvgStorageTests(SimpleTestCase):
    def view_for(self, file):
        view = AdminTemplateViewSet()
        view.get_object = Mock(return_value=SimpleNamespace(svg_file=file))
        return view

    def test_missing_local_file_returns_actionable_404(self):
        file = Mock()
        file.open.side_effect = FileNotFoundError('/private/local/path.svg')
        response = self.view_for(file).get_svg(None)
        self.assertEqual(response.status_code, 404)
        self.assertIn('missing from storage', response.data['error'])
        self.assertNotIn('/private/local', response.data['error'])

    def test_available_svg_is_served_and_closed(self):
        stream = BytesIO(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        file = Mock()
        file.open.return_value = stream
        response = self.view_for(file).get_svg(None)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        self.assertEqual(response['Content-Type'], 'image/svg+xml')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertTrue(stream.closed)

    def test_no_file_attached(self):
        response = self.view_for(None).get_svg(None)
        self.assertEqual(response.status_code, 404)
