from django.db.models import Count
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from ..cache_utils import invalidate_template_cache
from ..models import Country
from ..serializers import CountrySerializer
from ..permissions import IsAdminOnly, IsAdminOrReadOnly
from .tools import move_templates


class CountryViewSet(viewsets.ModelViewSet):
    """Countries templates are grouped under. Anyone can list them; only admins change them."""
    queryset = Country.objects.annotate(templates_count=Count('templates')).order_by('name')
    serializer_class = CountrySerializer
    permission_classes = [IsAdminOrReadOnly]
    pagination_class = None

    def perform_destroy(self, instance):
        # Its templates stay, just without a country
        super().perform_destroy(instance)
        invalidate_template_cache()

    @action(detail=True, methods=['post'], url_path='add-templates', permission_classes=[IsAdminOnly])
    def add_templates(self, request, pk=None):
        """Put templates under this country, from another country or from none."""
        country = self.get_object()
        result = move_templates(request, 'country', country, "MOVE_TEMPLATES_TO_COUNTRY")
        if result is None:
            return Response({'error': 'Some templates were not found.'}, status=status.HTTP_400_BAD_REQUEST)
        moved, templates = result
        return Response({
            'moved': len(moved),
            'already_in_country': len(templates) - len(moved),
            'templates_count': country.templates.count(),
        })

    @action(detail=True, methods=['post'], url_path='remove-templates', permission_classes=[IsAdminOnly])
    def remove_templates(self, request, pk=None):
        """Take templates out of this country (they're left with no country)."""
        country = self.get_object()
        result = move_templates(
            request, 'country', None, "REMOVE_TEMPLATES_FROM_COUNTRY", only_from=country,
        )
        if result is None:
            return Response({'error': 'Some templates were not found.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'removed': len(result[0]), 'templates_count': country.templates.count()})
