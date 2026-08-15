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

    def test_openapi_schema_is_not_publicly_exposed(self):
        response = self.client.get("/api/v1/schema")

        self.assertEqual(response.status_code, 404)
