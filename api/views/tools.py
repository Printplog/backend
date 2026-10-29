from django.db.models import Count
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from ..cache_utils import invalidate_template_cache
from ..models import Template, Tool
from ..serializers import ToolSerializer
from ..permissions import IsAdminOnly, IsAdminOrReadOnly


class AddTemplatesSerializer(serializers.Serializer):
    template_ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=False, max_length=500)


class ToolViewSet(viewsets.ModelViewSet):
    queryset = Tool.objects.select_related('tutorial').annotate(templates_count=Count('templates')).order_by('name')
    serializer_class = ToolSerializer
    permission_classes = [IsAdminOrReadOnly]

    @action(detail=True, methods=['post'], url_path='add-templates', permission_classes=[IsAdminOnly])
    def add_templates(self, request, pk=None):
        """Move templates into this tool, from another tool or from no tool."""
        tool = self.get_object()
        payload = AddTemplatesSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        ids = payload.validated_data['template_ids']

        templates = list(Template.objects.filter(id__in=ids).select_related('tool'))
        if len(templates) != len(set(ids)):
            return Response({'error': 'Some templates were not found.'}, status=status.HTTP_400_BAD_REQUEST)

        moved = [t for t in templates if t.tool_id != tool.id]
        # Plain update: only the tool changes, so skip Template.save()'s SVG work
        Template.objects.filter(id__in=[t.id for t in moved]).update(tool=tool)
        invalidate_template_cache()

        if moved:
            from analytics.utils import log_action
            log_action(
                actor=request.user,
                action="MOVE_TEMPLATES_TO_TOOL",
                target=f"{tool.name} ({tool.id})",
                ip_address=request.META.get('REMOTE_ADDR'),
                details={
                    'templates': [
                        {'id': str(t.id), 'name': t.name, 'from_tool': t.tool.name if t.tool else None}
                        for t in moved
                    ],
                },
            )

        return Response({
            'moved': len(moved),
            'already_in_tool': len(templates) - len(moved),
            'templates_count': tool.templates.count(),
        })
