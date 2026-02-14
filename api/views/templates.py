from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from django.db.models import Prefetch
from django.http import HttpResponse
import os

from ..models import Template, Tool
from ..serializers import TemplateSerializer, AdminTemplateSerializer
from ..permissions import IsAdminOrReadOnly, IsAdminOnly
from ..cache_utils import (
    cache_template_list,
    cache_template_detail,
    cache_template_svg,
    invalidate_template_cache
)

class TemplateViewSet(viewsets.ModelViewSet):
    queryset = Template.objects.all().order_by('-created_at')
    serializer_class = TemplateSerializer
    permission_classes = [IsAdminOrReadOnly]
    pagination_class = None

    def get_queryset(self):
        queryset = Template.objects.select_related('tool', 'tutorial').prefetch_related('fonts')
        hot_param = self.request.query_params.get("hot")
        tool_param = self.request.query_params.get("tool")

        if hot_param is not None:
            if hot_param.lower() == "true":
                queryset = queryset.filter(hot=True)
            elif hot_param.lower() == "false":
                queryset = queryset.filter(hot=False)
        
        if tool_param:
            queryset = queryset.filter(tool__id=tool_param)
        
        if self.action == 'list':
            queryset = queryset.defer('form_fields', 'svg_file')
        elif self.action == 'retrieve':
            pass
        
        return queryset.order_by('-created_at')

    @cache_template_list()
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @cache_template_detail()
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        invalidate_template_cache()
        
        # Log action
        from analytics.utils import log_action
        log_action(
            actor=request.user,
            action="ADD_TEMPLATE",
            target=f"Template {response.data.get('id', '?')}",
            ip_address=request.META.get('REMOTE_ADDR')
        )
        return response

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        response = super().update(request, *args, **kwargs)
        invalidate_template_cache(template_id=instance.id)
        
        from analytics.utils import log_action
        log_action(
            actor=request.user,
            action="UPDATE_TEMPLATE",
            target=f"{instance.name} ({instance.id})",
            ip_address=request.META.get('REMOTE_ADDR')
        )
        return response

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        template_id = instance.id
        template_name = instance.name
        response = super().destroy(request, *args, **kwargs)
        invalidate_template_cache(template_id=template_id)
        
        from analytics.utils import log_action
        log_action(
            actor=request.user,
            action="DELETE_TEMPLATE",
            target=f"{template_name} ({template_id})",
            ip_address=request.META.get('REMOTE_ADDR')
        )
        return response

    # Removed get_svg action in favor of direct svg_url in serializer


class AdminTemplateViewSet(viewsets.ModelViewSet):
    """Admin-only viewset for templates without watermarks"""
    queryset = Template.objects.all().order_by('-created_at')
    serializer_class = AdminTemplateSerializer
    permission_classes = [IsAdminOnly]
    pagination_class = None
    
    def get_queryset(self):
        queryset = Template.objects.select_related('tool', 'tutorial').prefetch_related('fonts')
        hot_param = self.request.query_params.get("hot")
        tool_param = self.request.query_params.get("tool")

        if hot_param is not None:
            if hot_param.lower() == "true":
                queryset = queryset.filter(hot=True)
            elif hot_param.lower() == "false":
                queryset = queryset.filter(hot=False)
        
        if tool_param:
            queryset = queryset.filter(tool__id=tool_param)
        
        if self.action == 'list':
            # Only defer in list view to keep the response small
            queryset = queryset.defer('form_fields', 'svg_file')
        
        return queryset.order_by('-created_at')
    
    # Removed get_svg action in favor of direct svg_url in serializer
    
    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        invalidate_template_cache()
        return response
    
    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        response = super().update(request, *args, **kwargs)
        invalidate_template_cache(template_id=instance.id)
        return response
    
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        template_id = instance.id
        response = super().destroy(request, *args, **kwargs)
        invalidate_template_cache(template_id=template_id)
        return response

    @action(detail=True, methods=['get'], url_path='svg')
    def get_svg(self, request, pk=None):
        """
        Proxy to fetch SVG content directly to avoid CORS issues in Admin Editor.
        This provides a fallback mechanism when direct CDN access is blocked (e.g., localhost).
        """
        template = self.get_object()
        if not template.svg_file:
             return Response({"error": "No SVG file found"}, status=404)
        
        try:
            # Read from storage
            # Note: For S3/B2, this might stream or download to memory
            template.svg_file.open()
            content = template.svg_file.read()
            return HttpResponse(content, content_type="image/svg+xml")
        except Exception as e:
            return Response({"error": f"Failed to read SVG: {str(e)}"}, status=500)


    @action(detail=True, methods=['post'], url_path='reparse')
    def reparse(self, request, pk=None):
        """Manually force re-parsing of the template SVG to update form_fields."""
        template = self.get_object()
        
        # Trigger the manual re-parse logic in model.save()
        template._force_reparse = True
        template.save()
        
        # Invalidate cache
        invalidate_template_cache(template_id=template.id)
        
        return Response({'status': 'success', 'message': 'Template re-parsed and form fields synced.'})


from rest_framework.views import APIView
class PublicTemplateTrackingView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    
    def get(self, request, tracking_id):
        from ..models import PurchasedTemplate
        from ..serializers import PurchasedTemplateSerializer
        try:
            purchase = PurchasedTemplate.objects.get(tracking_id=tracking_id)
            serializer = PurchasedTemplateSerializer(purchase)
            return Response(serializer.data)
        except PurchasedTemplate.DoesNotExist:
            return Response({"error": "Template not found"}, status=status.HTTP_404_NOT_FOUND)
