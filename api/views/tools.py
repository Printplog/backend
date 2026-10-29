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


def move_templates(request, field, group, log_action_name, only_from=None):
    """Set `Template.<field>` to `group` (a Tool, a Country, or None to take them out).

    `only_from` limits the ids to templates currently in that group. Returns None if
    any id isn't found (within that limit), else (moved, templates).
    """
    payload = AddTemplatesSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    ids = payload.validated_data['template_ids']

    templates = Template.objects.filter(id__in=ids).select_related(field)
    if only_from is not None:
        templates = templates.filter(**{field: only_from})
    templates = list(templates)
    if len(templates) != len(set(ids)):
        return None

    group_id = group.id if group else None
    moved = [t for t in templates if getattr(t, f'{field}_id') != group_id]
    # Plain update: only the group changes, so skip Template.save()'s SVG work
    Template.objects.filter(id__in=[t.id for t in moved]).update(**{field: group})
    invalidate_template_cache()

    if moved:
        from analytics.utils import log_action
        log_action(
            actor=request.user,
            action=log_action_name,
            target=f"{group.name} ({group.id})" if group else f"No {field}",
            ip_address=request.META.get('REMOTE_ADDR'),
            details={
                'templates': [
                    {
                        'id': str(t.id),
                        'name': t.name,
                        f'from_{field}': getattr(t, field).name if getattr(t, field) else None,
                    }
                    for t in moved
                ],
            },
        )
    return moved, templates


class ToolViewSet(viewsets.ModelViewSet):
    queryset = Tool.objects.select_related('tutorial').annotate(templates_count=Count('templates')).order_by('name')
    serializer_class = ToolSerializer
    permission_classes = [IsAdminOrReadOnly]

    @action(detail=True, methods=['post'], url_path='add-templates', permission_classes=[IsAdminOnly])
    def add_templates(self, request, pk=None):
        """Move templates into this tool, from another tool or from no tool."""
        tool = self.get_object()
        result = move_templates(request, 'tool', tool, "MOVE_TEMPLATES_TO_TOOL")
        if result is None:
            return Response({'error': 'Some templates were not found.'}, status=status.HTTP_400_BAD_REQUEST)
        moved, templates = result
        return Response({
            'moved': len(moved),
            'already_in_tool': len(templates) - len(moved),
            'templates_count': tool.templates.count(),
        })
