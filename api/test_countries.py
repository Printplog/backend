import uuid

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from analytics.models import AuditLog
from api.models import Country, Template, Tool

User = get_user_model()


class CountryTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", "boss@example.com", "pass-12345", is_staff=True)
        self.user = User.objects.create_user("plain", "plain@example.com", "pass-12345")
        self.usa = Country.objects.create(code="US", name="United States")
        self.uk = Country.objects.create(code="GB", name="United Kingdom")
        passports = Tool.objects.create(name="Passports")
        self.us_passport = Template.objects.create(name="USA Passport", type="tool", tool=passports, country=self.usa)
        self.uk_passport = Template.objects.create(name="UK Passport", type="tool", tool=passports, country=self.uk)
        self.loose = Template.objects.create(name="Boarding Pass", type="tool", tool=passports)

    def add(self, country, ids, path="add-templates"):
        return self.client.post(
            f"/api/countries/{country.id}/{path}/", {"template_ids": [str(i) for i in ids]}, format="json"
        )

    def test_anyone_can_list_countries_with_counts(self):
        response = self.client.get("/api/countries/")
        self.assertEqual(response.status_code, 200)
        rows = {c["code"]: (c["name"], c["templates_count"]) for c in response.data}
        self.assertEqual(rows, {"US": ("United States", 1), "GB": ("United Kingdom", 1)})

    def test_admin_adds_country_and_code_is_normalised(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post("/api/countries/", {"code": "ng", "name": "Nigeria"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["code"], "NG")
        self.assertEqual(response.data["templates_count"], 0)
        self.assertEqual(self.client.post("/api/countries/", {"code": "NG", "name": "Nigeria"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/countries/", {"code": "NGA", "name": "Nigeria"}, format="json").status_code, 400)

    def test_non_admin_cannot_change_countries(self):
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.post("/api/countries/", {"code": "NG", "name": "Nigeria"}, format="json").status_code, 403)
        self.assertEqual(self.add(self.usa, [self.loose.id]).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/countries/{self.usa.id}/").status_code, 403)

    def test_admin_moves_templates_into_country_from_another_or_none(self):
        self.client.force_authenticate(self.admin)
        response = self.add(self.usa, [self.uk_passport.id, self.loose.id, self.us_passport.id])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"moved": 2, "already_in_country": 1, "templates_count": 3})
        self.uk_passport.refresh_from_db()
        self.assertEqual(self.uk_passport.country, self.usa)
        # The tool is untouched: country and tool are separate groupings
        self.assertEqual(self.uk_passport.tool.name, "Passports")
        log = AuditLog.objects.get(action="MOVE_TEMPLATES_TO_COUNTRY")
        moved_from = {t["name"]: t["from_country"] for t in log.details["templates"]}
        self.assertEqual(moved_from, {"UK Passport": "United Kingdom", "Boarding Pass": None})

    def test_remove_only_takes_out_templates_of_that_country(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.add(self.usa, [self.uk_passport.id], path="remove-templates").status_code, 400)
        self.uk_passport.refresh_from_db()
        self.assertEqual(self.uk_passport.country, self.uk)

        response = self.add(self.usa, [self.us_passport.id], path="remove-templates")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"removed": 1, "templates_count": 0})
        self.us_passport.refresh_from_db()
        self.assertIsNone(self.us_passport.country)

    def test_unknown_ids_are_rejected(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.add(self.usa, [self.loose.id, uuid.uuid4()]).status_code, 400)
        self.loose.refresh_from_db()
        self.assertIsNone(self.loose.country)

    def test_deleting_country_keeps_its_templates(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.delete(f"/api/countries/{self.usa.id}/").status_code, 204)
        self.us_passport.refresh_from_db()
        self.assertIsNone(self.us_passport.country)

    def test_templates_can_be_filtered_by_country(self):
        ids = lambda r: {t["id"] for t in r.data["results"]}
        response = self.client.get(f"/api/templates/?country={self.usa.id}")
        self.assertEqual(ids(response), {str(self.us_passport.id)})
        self.assertEqual(response.data["results"][0]["country"], self.usa.id)
        self.assertEqual(ids(self.client.get("/api/templates/?country=none")), {str(self.loose.id)})

        self.client.force_authenticate(self.admin)
        self.assertEqual(ids(self.client.get(f"/api/admin/templates/?country={self.uk.id}")), {str(self.uk_passport.id)})
