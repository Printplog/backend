from rest_framework import serializers
from ..models import Template, Font, Tutorial
from .base import FontSerializer
from api.watermark import WaterMark
from api.utils import get_signed_url
import os
from lxml import etree
import json
from django.core.files.base import ContentFile

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
    banner = serializers.SerializerMethodField()
    tool_price = serializers.SerializerMethodField()
    
    class Meta:
        model = Template
        fields = '__all__'
    
    def get_svg_url(self, obj):
        if obj.svg_file:
            return get_signed_url(obj.svg_file)
        return None

    def get_banner(self, obj):
        if obj.banner:
            return get_signed_url(obj.banner)
        return None

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
        view = self.context.get('view')
        
        if view and view.action == 'list':
            # For list view: remove SVG and form_fields, keep banner
            representation.pop('svg', None)
            representation.pop('form_fields', None)
        elif view and view.action == 'retrieve':
            # For detail view: provide SVG URL, keep form_fields
            # User wants to fetch SVG from URL, so remove content
            if instance.svg_file:
                representation.pop('svg', None)
        else:
            # For other actions (create, update): include SVG if present
            if 'svg' in representation and representation['svg']:
                representation['svg'] = WaterMark().add_watermark(representation['svg'])
        
        return representation


class AdminTemplateSerializer(serializers.ModelSerializer):
    """Admin-only serializer that never adds watermarks and handles SVG patching."""
    fonts = FontSerializer(many=True, read_only=True)
    font_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Font.objects.all(),
        source='fonts',
        write_only=True,
        required=False
    )
    svg_url = serializers.SerializerMethodField()
    banner = serializers.SerializerMethodField()
    tool_price = serializers.SerializerMethodField()
    
    # Use ListField for structured data. For FormData, this will need parsing in `update`.
    svg_patch = serializers.ListField(
        child=serializers.DictField(),
        write_only=True,
        required=False
    )

    class Meta:
        model = Template
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at', 'form_fields')

    def get_svg_url(self, obj):
        if obj.svg_file:
            return get_signed_url(obj.svg_file)
        return None

    def get_banner(self, obj):
        if obj.banner:
            return get_signed_url(obj.banner)
        return None
    
    def get_tool_price(self, obj):
        return obj.tool.price if obj.tool else None

    def create(self, validated_data):
        fonts_data = validated_data.pop('fonts', None)
        validated_data.pop('svg_patch', None) # Don't use patch on create
        template = Template.objects.create(**validated_data)
        if fonts_data:
            template.fonts.set(fonts_data)
        return template
    
    def update(self, instance, validated_data):
        if 'form_fields' in validated_data:
            validated_data.pop('form_fields', None)
        
        fonts_data = validated_data.pop('fonts', None)
        
        # --- SVG Patch Logic ---
        svg_patch_data = validated_data.pop('svg_patch', None)
        
        # Handle case where patch comes as a JSON string from FormData
        request = self.context.get('request')
        if not svg_patch_data and request and 'svg_patch' in request.data:
            try:
                svg_patch_data = json.loads(request.data.get('svg_patch'))
            except (json.JSONDecodeError, TypeError):
                raise serializers.ValidationError("Invalid JSON format for svg_patch.")

        if svg_patch_data:
            try:
                # 1. Read existing SVG content
                if not instance.svg:
                    raise serializers.ValidationError("Cannot apply patch: No existing SVG found.")
                
                instance.svg.open('r')
                svg_content = instance.svg.read()
                instance.svg.close()

                if not svg_content:
                    raise serializers.ValidationError("Cannot apply patch: Existing SVG is empty.")

                # 2. Parse SVG using lxml
                parser = etree.XMLParser(recover=True, remove_blank_text=True)
                svg_tree = etree.fromstring(svg_content.encode('utf-8'), parser=parser)
                
                # Register namespaces to find elements correctly
                namespaces = {k if k is not None else 'svg': v for k, v in svg_tree.nsmap.items()}
                if 'svg' not in namespaces:
                    namespaces['svg'] = 'http://www.w3.org/2000/svg'

                # 3. Apply patches
                for patch in svg_patch_data:
                    element_id = patch.get('id')
                    attribute = patch.get('attribute')
                    value = patch.get('value')
                    
                    if not all([element_id, attribute]):
                        continue

                    # Find element by ID. The `.` ensures it searches the whole tree from the current node.
                    elements = svg_tree.findall(f".//*[@id='{element_id}']", namespaces=namespaces)
                    
                    for element in elements:
                        if attribute == 'innerText':
                            element.text = str(value)
                        else:
                            # Handle namespaced attributes like xlink:href
                            attr_parts = attribute.split(':')
                            if len(attr_parts) == 2 and attr_parts[0] in namespaces:
                                ns_key = namespaces[attr_parts[0]]
                                element.set(f"{{{ns_key}}}{attr_parts[1]}", str(value))
                            else:
                                element.set(attribute, str(value))
                
                # 4. Serialize back to string
                new_svg_content = etree.tostring(svg_tree, pretty_print=True).decode('utf-8')
                
                # 5. Overwrite the existing file content
                validated_data['svg'] = ContentFile(new_svg_content.encode('utf-8'), name=os.path.basename(instance.svg.name))

            except json.JSONDecodeError:
                raise serializers.ValidationError("Invalid JSON in svg_patch.")
            except Exception as e:
                # Use logging in a real app: logging.error(f"SVG Patch failed: {e}")
                raise serializers.ValidationError(f"Failed to apply SVG patch: {str(e)}")


        # Continue with the normal update process for all other fields
        instance = super().update(instance, validated_data)
        
        if fonts_data is not None:
            instance.fonts.set(fonts_data)
        
        return instance
    
    def to_representation(self, instance):
        representation = super().to_representation(instance)
        view = self.context.get('view')
        
        if view and view.action == 'list':
            representation.pop('svg', None)
            representation.pop('form_fields', None)
        elif view and view.action == 'retrieve':
            # For detail view: provide SVG URL, keep form_fields
            # Frontend wants to fetch SVG from URL for better loading speed
            if instance.svg_file:
                representation.pop('svg', None)
        else:
            pass
        
        return representation
