import uuid

from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination
from django.db.models import Q
from ..document_naming import default_document_name
from ..models import PurchasedTemplate, Template
from ..serializers import PurchasedTemplateSerializer
from ..permissions import IsOwnerOrAdmin


class DocumentPagination(PageNumberPagination):
    page_size = 12
    page_size_query_param = 'page_size'
    max_page_size = 50


class PurchasedTemplateViewSet(viewsets.ModelViewSet):
    serializer_class = PurchasedTemplateSerializer
    permission_classes = [IsOwnerOrAdmin]
    pagination_class = DocumentPagination

    @action(detail=False, methods=['post'], url_path='suggest-name', permission_classes=[IsAuthenticated])
    def suggest_name(self, request):
        """The default name a new document would get, e.g. "Jane Doe - USA Passport",
        so the user can confirm or change it before creating."""
        try:
            template_id = uuid.UUID(str(request.data.get('template')))
        except ValueError:
            template_id = None
        template = Template.objects.filter(id=template_id).only('id', 'name').first() if template_id else None
        if template is None:
            return Response({'error': 'Template not found.'}, status=status.HTTP_404_NOT_FOUND)
        fields = request.data.get('fields')
        fields = [f for f in fields if isinstance(f, dict)][:500] if isinstance(fields, list) else []
        return Response({'name': default_document_name(template, fields)})


    def get_queryset(self):
        user = self.request.user
        if not user or not user.is_authenticated:
            return PurchasedTemplate.objects.none()

        queryset = PurchasedTemplate.objects.select_related('buyer', 'template', 'template__tool').prefetch_related('fonts')
        
        # Determine filtering based on action
        # If action is None, we default to strict filtering
        action = getattr(self, 'action', None)

        if action == 'list' or action is None:
            # Strictly show ONLY the user's own documents in the list
            queryset = queryset.filter(buyer=user)
        else:
            # For detail views (retrieve/update/delete), allow admins to see any doc
            # Regular users are always limited to their own
            if not user.is_staff:
                queryset = queryset.filter(buyer=user)

        search = self.request.query_params.get('search', '').strip()
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) |
                Q(tracking_id__icontains=search) |
                Q(template__name__icontains=search)
            )
            
        return queryset.order_by('-created_at')




    # Removed get_svg action in favor of direct svg_url in serializer

    def perform_create(self, serializer):
        serializer.save(buyer=self.request.user)
