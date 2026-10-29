import uuid

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from analytics.models import AuditLog
from api.models import Template, Tool

User = get_user_model()


class ToolTemplatesTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", "boss@example.com", "pass-12345", is_staff=True)
        self.user = User.objects.create_user("plain", "plain@example.com", "pass-12345")
        self.flights = Tool.objects.create(name="Flights")
        self.licences = Tool.objects.create(name="Licences")
        self.boarding = Template.objects.create(name="Boarding Pass", type="tool", tool=self.flights)
        self.itinerary = Template.objects.create(name="Itinerary", type="tool", tool=self.flights)
        self.alabama = Template.objects.create(name="Alabama DL", type="tool", tool=self.licences)
        self.loose = Template.objects.create(name="Loose Template", type="tool", tool=None)

    def add(self, tool, ids):
        return self.client.post(f"/api/tools/{tool.id}/add-templates/", {"template_ids": [str(i) for i in ids]}, format="json")

    def test_tool_list_shows_template_counts(self):
        response = self.client.get("/api/tools/")
        counts = {tool["name"]: tool["templates_count"] for tool in response.data}
        self.assertEqual(counts, {"Flights": 2, "Licences": 1})

    def test_admin_moves_templates_from_another_tool_and_from_no_tool(self):
        self.client.force_authenticate(self.admin)
        response = self.add(self.licences, [self.boarding.id, self.loose.id, self.alabama.id])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"moved": 2, "already_in_tool": 1, "templates_count": 3})
        self.boarding.refresh_from_db()
        self.loose.refresh_from_db()
        self.assertEqual(self.boarding.tool, self.licences)
        self.assertEqual(self.loose.tool, self.licences)
        self.assertEqual(self.flights.templates.count(), 1)

        log = AuditLog.objects.get(action="MOVE_TEMPLATES_TO_TOOL")
        moved_from = {t["name"]: t["from_tool"] for t in log.details["templates"]}
        self.assertEqual(moved_from, {"Boarding Pass": "Flights", "Loose Template": None})

    def test_non_admin_cannot_move_templates(self):
        self.client.force_authenticate(self.user)
        self.assertEqual(self.add(self.licences, [self.boarding.id]).status_code, 403)
        self.client.force_authenticate(None)
        self.assertIn(self.add(self.licences, [self.boarding.id]).status_code, (401, 403))
        self.boarding.refresh_from_db()
        self.assertEqual(self.boarding.tool, self.flights)

    def test_unknown_or_empty_ids_are_rejected_without_changes(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.add(self.licences, [self.boarding.id, uuid.uuid4()]).status_code, 400)
        self.assertEqual(self.add(self.licences, []).status_code, 400)
        self.boarding.refresh_from_db()
        self.assertEqual(self.boarding.tool, self.flights)
