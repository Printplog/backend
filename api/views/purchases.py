from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from ..models import PurchasedTemplate
from ..serializers import PurchasedTemplateSerializer
from ..permissions import IsOwnerOrAdmin

class PurchasedTemplateViewSet(viewsets.ModelViewSet):
    serializer_class = PurchasedTemplateSerializer
    permission_classes = [IsOwnerOrAdmin]
    pagination_class = None

    def get_queryset(self):
        user = self.request.user
        queryset = PurchasedTemplate.objects.select_related('buyer', 'template', 'template__tool').prefetch_related('fonts')
        
        if not user.is_staff:
            queryset = queryset.filter(buyer=user)
            
        if self.action == 'list':
            queryset = queryset.defer('form_fields', 'svg_file')
        elif self.action == 'retrieve':
            pass
            
        return queryset.order_by('-created_at')

    # Removed get_svg action in favor of direct svg_url in serializer

    def perform_create(self, serializer):
        serializer.save(buyer=self.request.user)
