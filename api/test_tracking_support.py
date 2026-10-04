import base64
import hashlib
import hmac
import json
import time
from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from api.models import PurchasedTemplate, Template, Tool, TrackingSupportMessage, TrackingSupportReply
from api.utils.support_email import process_inbound_email


class TrackingSupportTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", "owner@example.com", "password")
        self.other = User.objects.create_user("other", "other@example.com", "password")
        tool = Tool.objects.create(name="Tracking Tool", price="5.00")
        template = Template.objects.create(name="Tracking Template", type="tool", tool=tool)
        self.document = PurchasedTemplate.objects.create(
            buyer=self.owner,
            template=template,
            name="Customer parcel",
            tracking_id="PF-2048",
        )

    def submit(self, **overrides):
        payload = {
            "tracking_id": "PF-2048",
            "source": "parcel_finda",
            "customer_name": "Ada Okafor",
            "customer_email": "ada@example.com",
            "subject": "Delivery address",
            "message": "Please confirm the delivery address on this parcel.",
            **overrides,
        }
        return self.client.post(reverse("tracking-support-create"), payload, format="json")

    def test_public_submission_is_attached_to_tracking_owner(self):
        response = self.submit()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        support_message = TrackingSupportMessage.objects.get()
        self.assertEqual(support_message.document, self.document)
        self.assertEqual(support_message.document.buyer, self.owner)
        self.assertEqual(support_message.status, TrackingSupportMessage.Status.NEW)

    def test_public_submission_rejects_unknown_tracking_id(self):
        response = self.submit(tracking_id="UNKNOWN")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(TrackingSupportMessage.objects.count(), 0)

    def test_owner_list_isolated_and_reports_unread_count(self):
        self.submit()
        self.client.force_authenticate(user=self.owner)

        response = self.client.get(reverse("tracking-support-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["unread_count"], 1)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["document_id"], str(self.document.id))
        self.assertEqual(response.data["results"][0]["tracking_id"], "PF-2048")

        self.client.force_authenticate(user=self.other)
        other_response = self.client.get(reverse("tracking-support-list"))
        self.assertEqual(other_response.data["results"], [])

    def test_owner_can_filter_messages_by_document(self):
        self.submit()
        other_document = PurchasedTemplate.objects.create(
            buyer=self.owner,
            template=self.document.template,
            name="Other parcel",
            tracking_id="PF-4096",
        )
        self.submit(tracking_id="PF-4096", subject="Other delivery")
        self.client.force_authenticate(user=self.owner)

        response = self.client.get(
            reverse("tracking-support-list"),
            {"document_id": str(self.document.id)},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["document_id"], str(self.document.id))
        self.assertNotEqual(response.data["results"][0]["document_id"], str(other_document.id))

    def test_owner_can_update_status_but_other_user_cannot(self):
        self.submit()
        support_message = TrackingSupportMessage.objects.get()
        detail_url = reverse("tracking-support-detail", args=[support_message.id])

        self.client.force_authenticate(user=self.other)
        self.assertEqual(
            self.client.patch(detail_url, {"status": "read"}, format="json").status_code,
            status.HTTP_404_NOT_FOUND,
        )

        self.client.force_authenticate(user=self.owner)
        response = self.client.patch(detail_url, {"status": "read"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        support_message.refresh_from_db()
        self.assertEqual(support_message.status, TrackingSupportMessage.Status.READ)

    @patch("api.views.support.send_owner_reply", return_value="resend-email-1")
    def test_owner_can_reply_and_conversation_is_returned(self, send_owner_reply):
        self.submit()
        support_message = TrackingSupportMessage.objects.get()
        self.client.force_authenticate(user=self.owner)

        response = self.client.post(
            reverse("tracking-support-reply", args=[support_message.id]),
            {"body": "The address is correct."},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        reply = TrackingSupportReply.objects.get()
        self.assertEqual(reply.direction, TrackingSupportReply.Direction.OWNER)
        self.assertEqual(reply.resend_email_id, "resend-email-1")
        send_owner_reply.assert_called_once()

        list_response = self.client.get(reverse("tracking-support-list"))
        conversation = list_response.data["results"][0]["conversation"]
        self.assertEqual([entry["direction"] for entry in conversation], ["customer", "owner"])
        self.assertEqual(conversation[1]["body"], "The address is correct.")

    @patch("api.views.support.send_owner_reply", return_value="resend-email-2")
    def test_other_user_cannot_reply(self, send_owner_reply):
        self.submit()
        support_message = TrackingSupportMessage.objects.get()
        self.client.force_authenticate(user=self.other)

        response = self.client.post(
            reverse("tracking-support-reply", args=[support_message.id]),
            {"body": "Not my ticket."},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        send_owner_reply.assert_not_called()

    @override_settings(RESEND_WEBHOOK_SECRET="whsec_c2VjcmV0")
    @patch("api.views.support.process_inbound_email", return_value="customer_reply")
    def test_verified_resend_webhook_is_processed(self, process_inbound_email):
        payload = json.dumps({"type": "email.received", "data": {"email_id": "email-1"}}).encode()
        message_id = "msg_123"
        timestamp = str(int(time.time()))
        signature = base64.b64encode(
            hmac.new(b"secret", f"{message_id}.{timestamp}.".encode() + payload, hashlib.sha256).digest()
        ).decode()

        response = self.client.post(
            reverse("resend-webhook"),
            data=payload,
            content_type="application/json",
            HTTP_SVIX_ID=message_id,
            HTTP_SVIX_TIMESTAMP=timestamp,
            HTTP_SVIX_SIGNATURE=f"v1,{signature}",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        process_inbound_email.assert_called_once_with({"email_id": "email-1"})

    @override_settings(RESEND_WEBHOOK_SECRET="whsec_c2VjcmV0")
    def test_invalid_resend_webhook_is_rejected(self):
        response = self.client.post(
            reverse("resend-webhook"),
            data=b'{"type":"email.received"}',
            content_type="application/json",
            HTTP_SVIX_ID="msg_123",
            HTTP_SVIX_TIMESTAMP=str(int(time.time())),
            HTTP_SVIX_SIGNATURE="v1,invalid",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("api.utils.support_email.notify_owner_of_customer_reply", return_value="owner-notification")
    @patch("api.utils.support_email._resend_request")
    def test_customer_email_reply_is_routed_to_the_ticket_owner(self, resend_request, notify_owner):
        self.submit()
        ticket = TrackingSupportMessage.objects.get()
        resend_request.return_value = {
            "from": "Ada Okafor <ada@example.com>",
            "text": "Here is the information you requested.",
        }

        result = process_inbound_email({
            "email_id": "received-customer-1",
            "to": [f"support+{ticket.id.hex}@parcelfinda.com"],
        })

        self.assertEqual(result, "customer_reply")
        reply = TrackingSupportReply.objects.get()
        self.assertEqual(reply.direction, TrackingSupportReply.Direction.CUSTOMER)
        self.assertEqual(reply.external_message_id, "received-customer-1")
        notify_owner.assert_called_once()

    @patch("api.utils.support_email.send_owner_reply", return_value="customer-notification")
    @patch("api.utils.support_email._resend_request")
    def test_owner_can_reply_from_email_without_exposing_private_address(self, resend_request, send_reply):
        self.submit()
        ticket = TrackingSupportMessage.objects.get()
        resend_request.return_value = {
            "from": "Document owner <owner@example.com>",
            "text": "Your delivery address is confirmed.",
        }

        result = process_inbound_email({
            "email_id": "received-owner-1",
            "to": [f"support+{ticket.id.hex}@parcelfinda.com"],
        })

        self.assertEqual(result, "owner_reply")
        reply = TrackingSupportReply.objects.get()
        self.assertEqual(reply.direction, TrackingSupportReply.Direction.OWNER)
        self.assertEqual(reply.resend_email_id, "customer-notification")
        send_reply.assert_called_once()
