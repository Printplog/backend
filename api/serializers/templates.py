from rest_framework import serializers
from ..models import Template, Font, Tutorial
from .base import FontSerializer
from api.watermark import WaterMark
import os

class TutorialSerializer(serializers.ModelSerializer):
    template_name = serializers.CharField(source='template.name', read_only=True)
    template_tool = serializers.CharField(source='template.tool.id', read_only=True, allow_null=True)
    template_tool_name = serializers.CharField(source='template.tool.name', read_only=True, allow_null=True)
    
    class Meta:
        model = Tutorial
        fields = ['id', 'template', 'template_name', 'template_tool', 'template_tool_name', 'url', 'title', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class TemplateSerializer(serializers.ModelSerializer):
    tutorial = TutorialSerializer(read_only=True)
    fonts = FontSerializer(many=True, read_only=True)
    font_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Font.objects.all(),
        source='fonts',
        write_only=True,
        required=False
    )
    svg_url = serializers.SerializerMethodField()
    tool_price = serializers.SerializerMethodField()
    
    class Meta:
        model = Template
        fields = '__all__'
    
    def get_svg_url(self, obj):
        request = self.context.get('request')
        if obj.svg_file and request:
            return request.build_absolute_uri(obj.svg_file.url)
        return obj.svg_file.url if obj.svg_file else None

    def get_tool_price(self, obj):
        return obj.tool.price if obj.tool else None

    def create(self, validated_data):
        # Extract tutorial data from request data
        request = self.context.get('request')
        tutorial_url = request.data.get('tutorial_url') if request else None
        tutorial_title = request.data.get('tutorial_title') if request else None
        fonts_data = validated_data.pop('fonts', None)
        
        # Create the template
        template = Template.objects.create(**validated_data)
        
        if fonts_data:
            template.fonts.set(fonts_data)
        
        # Create tutorial if URL is provided
        if tutorial_url:
            Tutorial.objects.create(
                template=template,
                url=tutorial_url,
                title=tutorial_title or ''
            )
        
        return template
    
    def update(self, instance, validated_data):
        # Extract tutorial data from request data
        request = self.context.get('request')
        tutorial_url = request.data.get('tutorial_url') if request else None
        tutorial_title = request.data.get('tutorial_title') if request else None
        fonts_data = validated_data.pop('fonts', None)
        
        # Update the template
        instance = super().update(instance, validated_data)
        
        if fonts_data is not None:
            instance.fonts.set(fonts_data)
        
        # Update or create tutorial
        if tutorial_url is not None:  # Allow clearing tutorial by sending empty string
            tutorial, created = Tutorial.objects.get_or_create(
                template=instance,
                defaults={'url': tutorial_url, 'title': tutorial_title or ''}
            )
            if not created:
                tutorial.url = tutorial_url
                tutorial.title = tutorial_title or ''
                tutorial.save()
        
        return instance
        
    def to_representation(self, instance):
        # Get the base representation
        representation = super().to_representation(instance)
        request = self.context.get('request')
        view = self.context.get('view')
        
        # Handle banner URL for production
        if 'banner' in representation and representation['banner']:
            if request and hasattr(request, 'build_absolute_uri'):
                # Use request to build absolute URL
                representation['banner'] = request.build_absolute_uri(representation['banner'])
            else:
                # Fallback: Just return the URL as is from the field
                pass
        
        if view and view.action == 'list':
            # For list view: remove SVG and form_fields, keep banner
            representation.pop('svg', None)
            representation.pop('form_fields', None)
        elif view and view.action == 'retrieve':
            # For detail view: provide SVG URL, keep form_fields
            representation.pop('svg', None)
        else:
            # For other actions (create, update): include SVG if present
            if 'svg' in representation and representation['svg']:
                representation['svg'] = WaterMark().add_watermark(representation['svg'])
        
        return representation


class AdminTemplateSerializer(serializers.ModelSerializer):
    """Admin-only serializer that never adds watermarks to templates"""
    fonts = FontSerializer(many=True, read_only=True)
    font_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Font.objects.all(),
        source='fonts',
        write_only=True,
        required=False
    )
    svg_url = serializers.SerializerMethodField()
    tool_price = serializers.SerializerMethodField()
    
    class Meta:
        model = Template
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at', 'form_fields')

    def get_svg_url(self, obj):
        request = self.context.get('request')
        if obj.svg_file and request:
            return request.build_absolute_uri(obj.svg_file.url)
        return obj.svg_file.url if obj.svg_file else None
    
    def get_tool_price(self, obj):
        return obj.tool.price if obj.tool else None

    def create(self, validated_data):
        fonts_data = validated_data.pop('fonts', None)
        template = Template.objects.create(**validated_data)
        if fonts_data:
            template.fonts.set(fonts_data)
        return template
    
    def update(self, instance, validated_data):
        if 'form_fields' in validated_data:
            validated_data.pop('form_fields', None)
        
        fonts_data = validated_data.pop('fonts', None)
        
        if 'svg' in validated_data:
            instance.skip_svg_parse = True
        
        instance = super().update(instance, validated_data)
        
        if fonts_data is not None:
            instance.fonts.set(fonts_data)
        
        return instance
    
    def to_representation(self, instance):
        representation = super().to_representation(instance)
        request = self.context.get('request')
        view = self.context.get('view')
        
        if 'banner' in representation and representation['banner']:
            if request and hasattr(request, 'build_absolute_uri'):
                representation['banner'] = request.build_absolute_uri(representation['banner'])
            else:
                pass
        
        if view and view.action == 'list':
            representation.pop('svg', None)
            representation.pop('form_fields', None)
        elif view and view.action == 'retrieve':
            pass
        else:
            pass
        
        return representation
