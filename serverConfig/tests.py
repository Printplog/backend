from django.test import SimpleTestCase, override_settings


class ApiDocsRoutingTests(SimpleTestCase):
    @override_settings(FRONTEND_URL="https://app.sharptoolz.test/")
    def test_generated_docs_route_redirects_to_custom_frontend_docs(self):
        response = self.client.get("/api/v1/docs")

        self.assertRedirects(
            response,
            "https://app.sharptoolz.test/api-docs",
            fetch_redirect_response=False,
        )

    def test_openapi_schema_exposes_only_the_supported_v1_contract(self):
        response = self.client.get("/api/v1/schema")

        self.assertEqual(response.status_code, 200)
        contract = response.content.decode("utf-8")
        self.assertIn("/templates", contract)
        self.assertIn("/documents", contract)
        self.assertNotIn("/admin/", contract)
