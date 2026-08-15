from drf_spectacular.extensions import OpenApiAuthenticationExtension


class ApiKeyAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "api.api_security.ApiKeyAuthentication"
    name = "SharpToolzApiKey"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "stz_live_...",
            "description": "Secret server-side SharpToolz API key.",
        }


def filter_public_v1_endpoints(endpoints):
    """Keep the generated contract limited to the supported customer API."""
    excluded = {"/api/v1/schema", "/api/v1/docs"}
    return [
        endpoint
        for endpoint in endpoints
        if endpoint[0].startswith("/api/v1/") and endpoint[0] not in excluded
    ]
