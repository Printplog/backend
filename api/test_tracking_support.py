from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from api.models import PurchasedTemplate, Template, Tool, TrackingSupportMessage


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
        self.assertEqual(response.data["results"][0]["tracking_id"], "PF-2048")

        self.client.force_authenticate(user=self.other)
        other_response = self.client.get(reverse("tracking-support-list"))
        self.assertEqual(other_response.data["results"], [])

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
