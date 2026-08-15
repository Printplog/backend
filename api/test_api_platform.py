from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User
from api.api_security import DEFAULT_API_KEY_SCOPES, generate_api_key
from api.models import (
    ApiEntitlement,
    ApiKey,
    DocumentRenderJob,
    EmbedSession,
    PurchasedTemplate,
    SiteSettings,
    Template,
    Tool,
)
from api.rendering import RenderInputError, sanitize_svg_for_render, svg_dimensions
from api.serializers.base import SiteSettingsSerializer
from wallet.models import Transaction, Wallet


class ApiPlatformSecurityTests(APITestCase):
    def setUp(self):
        self.site = SiteSettings.get_settings()
        self.site.enable_api_access = True
        self.site.require_api_upgrade = True
        self.site.api_upgrade_price = Decimal("20.00")
        self.site.api_session_ttl_minutes = 30
        self.site.save()

        self.customer = User.objects.create_user("customer", "customer@example.com", "password")
        self.other = User.objects.create_user("other", "other@example.com", "password")
        self.wallet = Wallet.objects.get(user=self.customer)
        self.wallet.balance = Decimal("100.00")
        self.wallet.save(update_fields=["balance"])
        self.other_wallet = Wallet.objects.get(user=self.other)
        self.other_wallet.balance = Decimal("100.00")
        self.other_wallet.save(update_fields=["balance"])

        self.tool = Tool.objects.create(name="API Tool", price=Decimal("5.00"))
        self.template = Template.objects.create(
            name="API Template",
            type="tool",
            tool=self.tool,
            form_fields=[
                {"id": "Name", "name": "Name", "type": "text", "max": 50, "editable": True},
                {"id": "Country", "name": "Country", "type": "select", "options": [
                    {"value": "ng", "label": "Nigeria", "displayText": "Nigeria", "svgElementId": "Country.select_ng"},
                ]},
                {"id": "Tracking", "name": "Tracking", "type": "gen", "isTrackingId": True, "generationRule": "AUTO:(ru[2])(rn[6])"},
                {"id": "Copy", "name": "Copy", "type": "text", "dependsOn": "Name[w1]"},
            ],
        )

    def activate(self, user=None):
        user = user or self.customer
        self.client.force_authenticate(user=user)
        response = self.client.post("/api/api-access/activate/", {}, format="json")
        self.client.force_authenticate(user=None)
        return response

    def issue_key(self, user=None, origins=None, scopes=None):
        user = user or self.customer
        ApiEntitlement.objects.get_or_create(user=user, defaults={"status": ApiEntitlement.Status.ACTIVE})
        token, prefix, token_hash = generate_api_key()
        key = ApiKey.objects.create(
            user=user,
            name="Test key",
            prefix=prefix,
            secret_hash=token_hash,
            scopes=scopes or DEFAULT_API_KEY_SCOPES,
            allowed_origins=origins or ["https://customer.example"],
        )
        return token, key

    def create_embed_session(self, token, **overrides):
        self.api_credentials(token)
        payload = {
            "template_id": str(self.template.id),
            "external_user_id": "end-user-1",
            "origin": "https://customer.example",
            "mode": "test",
            **overrides,
        }
        return self.client.post("/api/v1/embed-sessions", payload, format="json")

    def api_credentials(self, token):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_paid_activation_debits_wallet_once_and_records_price(self):
        first = self.activate()
        second = self.activate()
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("80.00"))
        entitlement = ApiEntitlement.objects.get(user=self.customer)
        self.assertEqual(entitlement.paid_amount, Decimal("20.00"))
        self.assertIsNotNone(entitlement.payment_transaction_id)
        self.assertEqual(
            Transaction.objects.filter(wallet=self.wallet, description="SharpToolz API account upgrade").count(),
            1,
        )

    def test_free_activation_does_not_debit_wallet(self):
        self.site.require_api_upgrade = False
        self.site.save(update_fields=["require_api_upgrade"])
        response = self.activate()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("100.00"))
        self.assertEqual(ApiEntitlement.objects.get(user=self.customer).paid_amount, Decimal("0.00"))

    def test_disabled_api_cannot_be_activated(self):
        self.site.enable_api_access = False
        self.site.save(update_fields=["enable_api_access"])
        response = self.activate()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(ApiEntitlement.objects.filter(user=self.customer).exists())

    def test_key_secret_is_returned_once_and_never_stored(self):
        ApiEntitlement.objects.create(user=self.customer)
        self.client.force_authenticate(user=self.customer)
        response = self.client.post(
            "/api/api-access/keys/",
            {"name": "Production", "allowed_origins": ["https://CUSTOMER.example/"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        secret = response.data["secret"]
        key = ApiKey.objects.get(pk=response.data["id"])
        self.assertNotEqual(key.secret_hash, secret)
        self.assertNotIn(secret, repr(key.__dict__))
        listed = self.client.get("/api/api-access/keys/")
        self.assertNotIn("secret", listed.data[0])
        self.assertEqual(key.allowed_origins, ["https://customer.example"])

    def test_revoked_key_is_rejected(self):
        token, key = self.issue_key()
        key.revoked_at = timezone.now()
        key.save(update_fields=["revoked_at"])
        self.api_credentials(token)
        response = self.client.get("/api/v1/templates")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_missing_scope_is_rejected(self):
        token, _ = self.issue_key(scopes=["templates:read"])
        self.api_credentials(token)
        response = self.client.get("/api/v1/documents")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_template_list_suppresses_legacy_svg_banner(self):
        self.template.banner.name = "templates/banners/legacy-source.svg"
        self.template.save(update_fields=["banner"])
        token, _ = self.issue_key()
        self.api_credentials(token)
        response = self.client.get("/api/v1/templates")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        listed = next(item for item in response.data["results"] if str(item["id"]) == str(self.template.id))
        self.assertIsNone(listed["banner_url"])

    def test_api_tool_discount_is_listed_and_charged_for_paid_embed(self):
        self.site.api_tool_discount_percentage = Decimal("20.00")
        self.site.save(update_fields=["api_tool_discount_percentage"])
        token, _ = self.issue_key()
        self.api_credentials(token)

        templates = self.client.get("/api/v1/templates")
        listed = next(
            item for item in templates.data["results"]
            if str(item["id"]) == str(self.template.id)
        )
        self.assertEqual(listed["price"], "4.00")

        created = self.create_embed_session(token, mode="paid")
        embed_token = created.data["embed_url"].split("#", 1)[1]
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Embed {embed_token}",
            HTTP_X_EMBED_ORIGIN="https://customer.example",
        )
        finalized = self.client.post(
            "/api/v1/embed/finalize",
            {"values": {"Name": "Discounted User", "Country": "ng"}},
            format="json",
        )

        self.assertEqual(finalized.status_code, status.HTTP_201_CREATED)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("96.00"))

    def test_api_tool_discount_applies_when_upgrading_test_document(self):
        self.site.api_tool_discount_percentage = Decimal("25.00")
        self.site.save(update_fields=["api_tool_discount_percentage"])
        token, _ = self.issue_key()
        document = PurchasedTemplate.objects.create(
            buyer=self.customer,
            template=self.template,
            external_user_id="discount-upgrade",
            name="Discount upgrade",
            test=True,
            form_fields=self.template.form_fields,
        )
        self.api_credentials(token)

        upgraded = self.client.post(
            f"/api/v1/documents/{document.id}/upgrade",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="discount-upgrade-test",
        )

        self.assertEqual(upgraded.status_code, status.HTTP_200_OK)
        self.wallet.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("96.25"))
        self.assertFalse(document.test)

    def test_full_api_tool_discount_does_not_create_zero_value_debit(self):
        self.site.api_tool_discount_percentage = Decimal("100.00")
        self.site.save(update_fields=["api_tool_discount_percentage"])
        token, _ = self.issue_key()
        created = self.create_embed_session(token, mode="paid")
        embed_token = created.data["embed_url"].split("#", 1)[1]
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Embed {embed_token}",
            HTTP_X_EMBED_ORIGIN="https://customer.example",
        )

        finalized = self.client.post(
            "/api/v1/embed/finalize",
            {"values": {"Name": "Free API User", "Country": "ng"}},
            format="json",
        )

        self.assertEqual(finalized.status_code, status.HTTP_201_CREATED)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("100.00"))
        self.assertFalse(PurchasedTemplate.objects.get(pk=finalized.data["document_id"]).test)

    def test_api_tool_discount_percentage_is_validated(self):
        too_high = SiteSettingsSerializer(
            self.site,
            data={"api_tool_discount_percentage": "100.01"},
            partial=True,
        )
        below_zero = SiteSettingsSerializer(
            self.site,
            data={"api_tool_discount_percentage": "-0.01"},
            partial=True,
        )

        self.assertFalse(too_high.is_valid())
        self.assertFalse(below_zero.is_valid())

    def test_template_schema_is_not_public(self):
        token, _ = self.issue_key(scopes=["templates:read"])
        self.api_credentials(token)
        response = self.client.get(f"/api/v1/templates/{self.template.id}/schema")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_suspended_entitlement_invalidates_api_key(self):
        token, _ = self.issue_key()
        entitlement = ApiEntitlement.objects.get(user=self.customer)
        entitlement.status = ApiEntitlement.Status.SUSPENDED
        entitlement.save(update_fields=["status"])
        self.api_credentials(token)
        response = self.client.get("/api/v1/templates")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_cross_tenant_document_read_is_not_found(self):
        own_token, _ = self.issue_key()
        foreign_document = PurchasedTemplate.objects.create(
            buyer=self.other,
            template=self.template,
            external_user_id="same-label",
            form_fields=self.template.form_fields,
        )
        self.api_credentials(own_token)
        response = self.client.get(f"/api/v1/documents/{foreign_document.id}")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_direct_document_creation_is_not_allowed(self):
        token, _ = self.issue_key()
        self.api_credentials(token)
        response = self.client.post(
            "/api/v1/documents",
            {
                "template_id": str(self.template.id),
                "external_user_id": "42",
                "mode": "test",
                "values": {"Name": "Jane Doe", "Tracking": "ATTACKER"},
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="generated-field-test",
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertEqual(PurchasedTemplate.objects.filter(buyer=self.customer).count(), 0)

    def test_malformed_template_identifiers_are_client_errors(self):
        token, _ = self.issue_key()
        self.api_credentials(token)
        session = self.client.post(
            "/api/v1/embed-sessions",
            {
                "template_id": "not-a-uuid",
                "external_user_id": "42",
                "origin": "https://customer.example",
            },
            format="json",
        )
        self.assertEqual(session.status_code, status.HTTP_400_BAD_REQUEST)

    def test_direct_document_field_updates_are_not_allowed(self):
        token, _ = self.issue_key()
        document = PurchasedTemplate.objects.create(
            buyer=self.customer,
            template=self.template,
            external_user_id="42",
            form_fields=[
                {**self.template.form_fields[0], "currentValue": "Jane"},
                {**self.template.form_fields[1], "currentValue": "ng"},
            ],
        )
        self.api_credentials(token)
        response = self.client.patch(
            f"/api/v1/documents/{document.id}", {"values": {"Name": "Janet"}}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_document_retrieve_returns_metadata_without_internal_fields(self):
        token, _ = self.issue_key()
        document = PurchasedTemplate.objects.create(
            buyer=self.customer,
            template=self.template,
            external_user_id="42",
            form_fields=self.template.form_fields,
        )
        self.api_credentials(token)
        response = self.client.get(f"/api/v1/documents/{document.id}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("form_fields", response.data)

    def test_embed_token_is_origin_bound_and_finalize_is_replay_safe(self):
        token, _ = self.issue_key()
        self.api_credentials(token)
        created = self.client.post(
            "/api/v1/embed-sessions",
            {
                "template_id": str(self.template.id),
                "external_user_id": "end-user-1",
                "origin": "https://customer.example",
                "mode": "paid",
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertEqual(created.data["operation"], "create")
        self.assertIsNone(created.data["document_id"])
        embed_token = created.data["embed_url"].split("#", 1)[1]

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Embed {embed_token}",
            HTTP_X_EMBED_ORIGIN="https://attacker.example",
        )
        denied = self.client.get("/api/v1/embed/session")
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Embed {embed_token}",
            HTTP_X_EMBED_ORIGIN="https://customer.example",
        )
        session_response = self.client.get("/api/v1/embed/session")
        self.assertEqual(session_response.status_code, status.HTTP_200_OK)
        self.assertEqual(session_response.data["template"]["fonts"], [])

        first = self.client.post(
            "/api/v1/embed/finalize",
            {"values": {"Name": "Jane Doe", "Country": "ng"}},
            format="json",
        )
        second = self.client.post(
            "/api/v1/embed/finalize",
            {"values": {"Name": "Changed", "Country": "ng"}},
            format="json",
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data["document_id"], second.data["document_id"])
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("95.00"))
        self.assertEqual(EmbedSession.objects.get(pk=created.data["id"]).status, EmbedSession.Status.COMPLETED)

    def test_edit_session_updates_only_editable_fields_without_charging_wallet(self):
        token, _ = self.issue_key()
        document = PurchasedTemplate.objects.create(
            buyer=self.customer,
            template=self.template,
            external_user_id="end-user-1",
            name="Original name",
            test=False,
            tracking_id="TRACK-1",
            form_fields=[
                {**self.template.form_fields[0], "currentValue": "Jane"},
                {**self.template.form_fields[1], "currentValue": "ng"},
                {**self.template.form_fields[2], "currentValue": "TRACK-1"},
                {**self.template.form_fields[3], "currentValue": "Jane"},
            ],
        )
        self.api_credentials(token)
        created = self.client.post(
            f"/api/v1/documents/{document.id}/session",
            {
                "origin": "https://customer.example",
                "preview_mode": "standard",
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertEqual(created.data["operation"], "edit")
        self.assertEqual(str(created.data["document_id"]), str(document.id))
        embed_token = created.data["embed_url"].split("#", 1)[1]
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Embed {embed_token}",
            HTTP_X_EMBED_ORIGIN="https://customer.example",
        )
        loaded = self.client.get("/api/v1/embed/session")
        self.assertEqual(loaded.status_code, status.HTTP_200_OK)
        self.assertEqual(loaded.data["operation"], "edit")
        self.assertEqual(loaded.data["document_name"], "Original name")
        self.assertEqual(loaded.data["prefill"]["Name"], "Jane")

        first = self.client.post(
            "/api/v1/embed/finalize",
            {"values": {"Name": "Janet"}, "name": "Updated name"},
            format="json",
        )
        replay = self.client.post(
            "/api/v1/embed/finalize",
            {"values": {"Name": "Attacker replay"}},
            format="json",
        )
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(replay.status_code, status.HTTP_200_OK)
        document.refresh_from_db()
        values = {field["id"]: field.get("currentValue") for field in document.form_fields}
        self.assertEqual(values["Name"], "Janet")
        self.assertEqual(values["Country"], "ng")
        self.assertEqual(values["Tracking"], "TRACK-1")
        self.assertEqual(document.name, "Updated name")
        self.assertEqual(document.tracking_id, "TRACK-1")
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("100.00"))
        self.assertEqual(PurchasedTemplate.objects.filter(buyer=self.customer).count(), 1)

    def test_edit_session_rejects_non_editable_fields_and_foreign_documents(self):
        token, _ = self.issue_key()
        own_document = PurchasedTemplate.objects.create(
            buyer=self.customer,
            template=self.template,
            external_user_id="42",
            form_fields=[
                {**self.template.form_fields[0], "currentValue": "Jane"},
                {**self.template.form_fields[1], "currentValue": "ng"},
            ],
        )
        foreign_document = PurchasedTemplate.objects.create(
            buyer=self.other,
            template=self.template,
            external_user_id="42",
            form_fields=self.template.form_fields,
        )
        self.api_credentials(token)
        foreign = self.client.post(
            f"/api/v1/documents/{foreign_document.id}/session",
            {"origin": "https://customer.example"},
            format="json",
        )
        self.assertEqual(foreign.status_code, status.HTTP_404_NOT_FOUND)

        created = self.client.post(
            f"/api/v1/documents/{own_document.id}/session",
            {"origin": "https://customer.example"},
            format="json",
        )
        embed_token = created.data["embed_url"].split("#", 1)[1]
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Embed {embed_token}",
            HTTP_X_EMBED_ORIGIN="https://customer.example",
        )
        denied = self.client.post(
            "/api/v1/embed/finalize",
            {"values": {"Country": "ng"}},
            format="json",
        )
        self.assertEqual(denied.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(EmbedSession.objects.get(pk=created.data["id"]).status, EmbedSession.Status.PENDING)

    @patch("api.views.api_platform.public_protected_manifest")
    @patch("api.views.api_platform.build_protected_preview")
    def test_protected_embed_never_returns_svg_source(self, build_preview, public_manifest):
        build_preview.return_value = {"version": 1}
        public_manifest.return_value = {
            "version": 1,
            "width": 800,
            "height": 600,
            "base_url": "https://testserver/api/v1/embed/preview-assets/base",
            "layers": [],
        }
        token, _ = self.issue_key()
        created = self.create_embed_session(token, preview_mode="protected")
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            EmbedSession.objects.get(pk=created.data["id"]).preview_mode,
            EmbedSession.PreviewMode.PROTECTED,
        )

        embed_token = created.data["embed_url"].split("#", 1)[1]
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Embed {embed_token}",
            HTTP_X_EMBED_ORIGIN="https://customer.example",
        )
        response = self.client.get("/api/v1/embed/session")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["preview_mode"], "protected")
        self.assertIsNone(response.data["template"]["svg_url"])
        self.assertEqual(response.data["template"]["svg_patches"], [])
        self.assertEqual(response.data["template"]["fonts"], [])
        self.assertEqual(response.data["template"]["protected_preview"]["version"], 1)
        build_preview.assert_called_once_with(self.template)

    def test_embed_origin_wildcards_and_paths_are_rejected(self):
        token, _ = self.issue_key()
        wildcard = self.create_embed_session(token, origin="https://*.customer.example")
        path = self.create_embed_session(token, origin="https://customer.example/form")
        self.assertEqual(wildcard.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(path.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(EmbedSession.objects.count(), 0)

    def test_create_session_rejects_programmatic_field_prefill(self):
        token, _ = self.issue_key()
        response = self.create_embed_session(token, prefill={"Name": "Bypass iframe"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(EmbedSession.objects.count(), 0)

    def test_revoking_key_immediately_revokes_pending_embed_sessions(self):
        token, key = self.issue_key()
        created = self.create_embed_session(token)
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        embed_token = created.data["embed_url"].split("#", 1)[1]

        self.client.force_authenticate(user=self.customer)
        revoked = self.client.delete(f"/api/api-access/keys/{key.id}/")
        self.client.force_authenticate(user=None)
        self.assertEqual(revoked.status_code, status.HTTP_204_NO_CONTENT)

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Embed {embed_token}",
            HTTP_X_EMBED_ORIGIN="https://customer.example",
        )
        response = self.client.get("/api/v1/embed/session")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(EmbedSession.objects.get(pk=created.data["id"]).status, EmbedSession.Status.REVOKED)

    def test_global_kill_switch_invalidates_existing_embed_session(self):
        token, _ = self.issue_key()
        created = self.create_embed_session(token)
        embed_token = created.data["embed_url"].split("#", 1)[1]
        self.site.enable_api_access = False
        self.site.save(update_fields=["enable_api_access"])
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Embed {embed_token}",
            HTTP_X_EMBED_ORIGIN="https://customer.example",
        )
        response = self.client.get("/api/v1/embed/session")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_api_request_body_limit_rejects_before_parsing(self):
        token, _ = self.issue_key()
        self.api_credentials(token)
        response = self.client.post(
            "/api/v1/documents",
            {},
            format="json",
            CONTENT_LENGTH=str(26 * 1024 * 1024),
            HTTP_IDEMPOTENCY_KEY="oversized",
        )
        self.assertEqual(response.status_code, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

    def test_cookie_authenticated_writes_require_csrf(self):
        self.site.require_api_upgrade = False
        self.site.save(update_fields=["require_api_upgrade"])
        browser = APIClient(enforce_csrf_checks=True)
        access_token = str(RefreshToken.for_user(self.customer).access_token)
        browser.cookies["access_token"] = access_token

        denied = browser.post("/api/api-access/activate/", {}, format="json")
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(ApiEntitlement.objects.filter(user=self.customer).exists())

        csrf_response = browser.get("/api/accounts/csrf/")
        allowed = browser.post(
            "/api/api-access/activate/",
            {},
            format="json",
            HTTP_X_CSRFTOKEN=csrf_response.data["csrfToken"],
        )
        self.assertEqual(allowed.status_code, status.HTTP_201_CREATED)

    def test_refresh_cookie_cannot_mint_access_without_csrf(self):
        browser = APIClient(enforce_csrf_checks=True)
        browser.cookies["refresh_token"] = str(RefreshToken.for_user(self.customer))
        denied = browser.post("/api/accounts/refresh-token/", {}, format="json")
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)

        csrf_response = browser.get("/api/accounts/csrf/")
        allowed = browser.post(
            "/api/accounts/refresh-token/",
            {},
            format="json",
            HTTP_X_CSRFTOKEN=csrf_response.data["csrfToken"],
        )
        self.assertEqual(allowed.status_code, status.HTTP_200_OK)
        self.assertIn("access_token", allowed.cookies)

    def test_login_bootstraps_csrf_before_validating_credentials(self):
        browser = APIClient(enforce_csrf_checks=True)
        credentials = {"username": "not-a-user", "password": "not-a-password"}

        denied = browser.post("/api/accounts/login/", credentials, format="json")
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)

        csrf_response = browser.get("/api/accounts/csrf/")
        self.assertEqual(csrf_response.status_code, status.HTTP_200_OK)
        self.assertIn("csrftoken", csrf_response.cookies)

        validated = browser.post(
            "/api/accounts/login/",
            credentials,
            format="json",
            HTTP_X_CSRFTOKEN=csrf_response.data["csrfToken"],
        )
        self.assertEqual(validated.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(validated.data["error"], ["Invalid credentials"])

    def test_render_creation_is_idempotent_and_cross_tenant_polling_is_hidden(self):
        token, _ = self.issue_key()
        document = PurchasedTemplate.objects.create(
            buyer=self.customer,
            template=self.template,
            external_user_id="42",
            form_fields=self.template.form_fields,
        )
        self.api_credentials(token)
        with patch("api.tasks.render_document.delay") as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                first = self.client.post(
                    f"/api/v1/documents/{document.id}/render",
                    {"format": "png"},
                    format="json",
                    HTTP_IDEMPOTENCY_KEY="render-42",
                )
            second = self.client.post(
                f"/api/v1/documents/{document.id}/render",
                {"format": "png"},
                format="json",
                HTTP_IDEMPOTENCY_KEY="render-42",
            )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data["id"], second.data["id"])
        enqueue.assert_called_once_with(first.data["id"])

        other_token, _ = self.issue_key(user=self.other)
        self.api_credentials(other_token)
        hidden = self.client.get(f"/api/v1/renders/{first.data['id']}")
        self.assertEqual(hidden.status_code, status.HTTP_404_NOT_FOUND)

    def test_cross_tenant_document_cannot_be_rendered(self):
        token, _ = self.issue_key()
        foreign_document = PurchasedTemplate.objects.create(
            buyer=self.other,
            template=self.template,
            external_user_id="42",
            form_fields=self.template.form_fields,
        )
        self.api_credentials(token)
        response = self.client.post(
            f"/api/v1/documents/{foreign_document.id}/render",
            {"format": "pdf"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="foreign-render",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(DocumentRenderJob.objects.exists())

    def test_render_watch_ticket_is_short_lived_job_scoped_and_buyer_scoped(self):
        from api.render_events import read_render_watch_ticket

        token, key = self.issue_key()
        document = PurchasedTemplate.objects.create(
            buyer=self.customer,
            template=self.template,
            external_user_id="42",
            form_fields=self.template.form_fields,
        )
        job = DocumentRenderJob.objects.create(
            user=self.customer,
            document=document,
            requested_by_key=key,
            format=DocumentRenderJob.Format.PDF,
            expires_at=timezone.now() + timedelta(hours=1),
        )

        self.api_credentials(token)
        response = self.client.post(f"/api/v1/renders/{job.id}/watch", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["expires_in"], 60)
        parsed = urlsplit(response.data["websocket_url"])
        self.assertEqual(parsed.scheme, "ws")
        self.assertEqual(parsed.path, f"/ws/api/v1/renders/{job.id}/")
        self.assertNotIn(token, response.data["websocket_url"])
        ticket = parse_qs(parsed.query)["ticket"][0]
        payload = read_render_watch_ticket(ticket)
        self.assertEqual(payload["job_id"], str(job.id))
        self.assertEqual(payload["user_id"], str(self.customer.id))
        self.assertEqual(payload["api_key_id"], str(key.id))

        other_token, _ = self.issue_key(user=self.other)
        self.api_credentials(other_token)
        hidden = self.client.post(f"/api/v1/renders/{job.id}/watch", {}, format="json")
        self.assertEqual(hidden.status_code, status.HTTP_404_NOT_FOUND)

    @override_settings(API_RENDER_MAX_ACTIVE_PER_KEY=1)
    def test_render_active_job_cap_limits_resource_abuse(self):
        token, key = self.issue_key()
        document = PurchasedTemplate.objects.create(
            buyer=self.customer,
            template=self.template,
            external_user_id="42",
            form_fields=self.template.form_fields,
        )
        DocumentRenderJob.objects.create(
            user=self.customer,
            document=document,
            requested_by_key=key,
            format=DocumentRenderJob.Format.PNG,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.api_credentials(token)
        response = self.client.post(
            f"/api/v1/documents/{document.id}/render",
            {"format": "png"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="over-cap",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(DocumentRenderJob.objects.count(), 1)

    @override_settings(API_EMBED_MAX_PENDING_PER_KEY=1)
    def test_pending_embed_session_cap_limits_token_flooding(self):
        token, _ = self.issue_key()
        first = self.create_embed_session(token)
        second = self.create_embed_session(token, external_user_id="end-user-2")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(EmbedSession.objects.count(), 1)

    @override_settings(
        STORAGES={
            "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        }
    )
    def test_render_download_signature_is_short_lived_and_job_bound(self):
        token, key = self.issue_key()
        document = PurchasedTemplate.objects.create(
            buyer=self.customer,
            template=self.template,
            external_user_id="42",
            form_fields=self.template.form_fields,
        )
        job = DocumentRenderJob.objects.create(
            user=self.customer,
            document=document,
            requested_by_key=key,
            format=DocumentRenderJob.Format.PDF,
            status=DocumentRenderJob.Status.COMPLETED,
            expires_at=timezone.now() + timedelta(hours=1),
            completed_at=timezone.now(),
        )
        job.output_file.save(f"{job.id}.pdf", ContentFile(b"%PDF-1.7\n%%EOF"))
        job.output_size = job.output_file.size
        job.save(update_fields=["output_size", "updated_at"])

        self.api_credentials(token)
        polled = self.client.get(f"/api/v1/renders/{job.id}")
        self.assertEqual(polled.status_code, status.HTTP_200_OK)
        signed = urlsplit(polled.data["download_url"])

        self.client.credentials()
        downloaded = self.client.get(f"{signed.path}?{signed.query}")
        self.assertEqual(downloaded.status_code, status.HTTP_200_OK)
        self.assertEqual(b"".join(downloaded.streaming_content), b"%PDF-1.7\n%%EOF")
        self.assertEqual(downloaded["Cache-Control"], "private, no-store")

        tampered = self.client.get(f"{signed.path}?{signed.query}x")
        self.assertEqual(tampered.status_code, status.HTTP_403_FORBIDDEN)
        different_job_id = DocumentRenderJob.objects.create(
            user=self.customer,
            document=document,
            format=DocumentRenderJob.Format.PDF,
            expires_at=timezone.now() + timedelta(hours=1),
        ).id
        rebound = self.client.get(f"/api/v1/render-download/{different_job_id}?{signed.query}")
        self.assertEqual(rebound.status_code, status.HTTP_403_FORBIDDEN)

        key.revoked_at = timezone.now()
        key.save(update_fields=["revoked_at"])
        revoked = self.client.get(f"{signed.path}?{signed.query}")
        self.assertEqual(revoked.status_code, status.HTTP_403_FORBIDDEN)

    def test_render_svg_sanitizer_blocks_active_content_and_resource_fetches(self):
        attack = """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"
            onload="alert(1)"><script>alert(1)</script><foreignObject><div>bad</div></foreignObject>
            <image href="https://attacker.example/pixel" src="//attacker.example/pixel"/>
            <style>rect{fill:url(/* bypass */ https://attacker.example/a)}</style>
            <rect style="background-image:url(javascript:alert(1))"/></svg>"""
        cleaned = sanitize_svg_for_render(attack)
        for forbidden in ("script", "foreignObject", "onload", "attacker.example", "javascript:"):
            self.assertNotIn(forbidden, cleaned)

        legacy_raster = sanitize_svg_for_render(
            '<svg xmlns="http://www.w3.org/2000/svg"><image href="data:img/png;base64,iVBORw0KGgo="/></svg>'
        )
        self.assertIn("data:image/png;base64,iVBORw0KGgo=", legacy_raster)
        self.assertNotIn("data:img/png", legacy_raster)

        malformed_raster = sanitize_svg_for_render(
            '<svg xmlns="http://www.w3.org/2000/svg"><image href="data:image/png;base64,AAAAhttps://attacker.example"/></svg>'
        )
        self.assertNotIn("href=", malformed_raster)
        self.assertNotIn("attacker.example", malformed_raster)

        metadata_id = 'Tracking.gen.link_&quot;https://customer.example&quot;'
        metadata = sanitize_svg_for_render(
            f'<svg xmlns="http://www.w3.org/2000/svg"><text id="{metadata_id}">Safe ID</text></svg>'
        )
        self.assertIn("https://customer.example", metadata)

        xxe = """<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
            <svg xmlns="http://www.w3.org/2000/svg"><text>&xxe;</text></svg>"""
        with self.assertRaises(RenderInputError):
            sanitize_svg_for_render(xxe)
        with self.assertRaises(RenderInputError):
            svg_dimensions('<svg xmlns="http://www.w3.org/2000/svg" width="999999" height="2"/>')

    @override_settings(
        STORAGES={
            "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        }
    )
    def test_render_task_stores_only_verified_output(self):
        from api.tasks import render_document

        _, key = self.issue_key()
        document = PurchasedTemplate.objects.create(
            buyer=self.customer,
            template=self.template,
            external_user_id="42",
            form_fields=[],
        )
        job = DocumentRenderJob.objects.create(
            user=self.customer,
            document=document,
            requested_by_key=key,
            format=DocumentRenderJob.Format.PDF,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        with (
            patch("api.tasks.assemble_document_svg", return_value='<svg width="1" height="1"/>'),
            patch("api.tasks.render_svg_with_chromium", return_value=b"%PDF-1.7\n%%EOF"),
            patch("api.tasks.broadcast_render_job") as broadcast,
        ):
            render_document.run(str(job.id))
        job.refresh_from_db()
        self.assertEqual(job.status, DocumentRenderJob.Status.COMPLETED)
        self.assertEqual(job.output_size, len(b"%PDF-1.7\n%%EOF"))
        self.assertTrue(job.output_file.name.endswith(".pdf"))
        self.assertEqual(broadcast.call_count, 2)
        broadcast.assert_called_with(str(job.id))
