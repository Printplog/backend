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
            queryset = queryset.defer('svg', 'form_fields')
        elif self.action == 'retrieve':
            queryset = queryset.defer('svg')
            
        return queryset.order_by('-created_at')

    @action(detail=True, methods=['get'], url_path='svg')
    def get_svg(self, request, pk=None):
        """Separate endpoint to load SVG content for purchased templates"""
        try:
            instance = self.get_object()
            if instance.svg_file:
                 return Response({"url": request.build_absolute_uri(instance.svg_file.url), "svg": None}, status=status.HTTP_200_OK)
            return Response({"svg": instance.svg}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_404_NOT_FOUND)

    def perform_create(self, serializer):
        serializer.save(buyer=self.request.user)
