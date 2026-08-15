from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from ..utils.email_service import EmailService

class ContactView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        name = request.data.get("name")
        email = request.data.get("email")
        subject = request.data.get("subject")
        message = request.data.get("message")

        # Simple validation
        if not all([name, email, subject, message]):
            return Response(
                {"error": "All fields are required."}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        # Queued, not delivered — `accepted` says we took ownership of it, and
        # the worker retries on failure rather than making the sender re-type.
        accepted = EmailService.send_contact_form(name, email, subject, message)

        if accepted:
            return Response(
                {"message": "Your message has been sent successfully."},
                status=status.HTTP_200_OK
            )
        else:
            return Response(
                {"error": "Failed to send message. Please try again later."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
