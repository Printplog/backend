from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from ..models import TrackingSupportMessage
from ..serializers.support import (
    PublicTrackingSupportSerializer,
    TrackingSupportMessageSerializer,
    TrackingSupportStatusSerializer,
)


class PublicTrackingSupportView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "tracking_support"

    def post(self, request):
        serializer = PublicTrackingSupportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        support_message = serializer.save()
        return Response(
            {
                "id": str(support_message.id),
                "message": "Your support request has been received.",
            },
            status=status.HTTP_201_CREATED,
        )


class TrackingSupportMessageListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        messages = TrackingSupportMessage.objects.filter(
            document__buyer=request.user,
        ).select_related("document")
        source = request.query_params.get("source", "").strip()
        message_status = request.query_params.get("status", "").strip()
        if source:
            messages = messages.filter(source=source)
        if message_status:
            messages = messages.filter(status=message_status)
        return Response({
            "results": TrackingSupportMessageSerializer(messages, many=True).data,
            "unread_count": messages.filter(status=TrackingSupportMessage.Status.NEW).count(),
        })


class TrackingSupportMessageDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, message_id):
        support_message = get_object_or_404(
            TrackingSupportMessage.objects.select_related("document"),
            id=message_id,
            document__buyer=request.user,
        )
        serializer = TrackingSupportStatusSerializer(
            support_message,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(TrackingSupportMessageSerializer(support_message).data)
