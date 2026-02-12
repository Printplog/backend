from rest_framework import serializers
from ..models import PurchasedTemplate, Template
from .base import FieldUpdateSerializer, FontSerializer
from ..svg_updater import update_svg_from_field_updates
from api.watermark import WaterMark
from api.utils import get_signed_url
from decimal import Decimal

class PurchasedTemplateSerializer(serializers.ModelSerializer):
    field_updates = FieldUpdateSerializer(many=True, write_only=True, required=False)
    fonts = FontSerializer(many=True, read_only=True)
    banner = serializers.SerializerMethodField()
    tool_price = serializers.SerializerMethodField()
    svg_url = serializers.SerializerMethodField()
    
    class Meta:
        model = PurchasedTemplate
        fields = '__all__'
        read_only_fields = ('buyer',)
        extra_kwargs = {
            'svg': {'required': False},
        }
        
    def charge_if_test_false(self, instance, validated_data, is_update=False):
        old_test = instance.test if is_update else True
        new_test = validated_data.get("test", old_test)

        if old_test is True and new_test is False:
            user = instance.buyer

            if not hasattr(user, "wallet"):
                raise serializers.ValidationError("User does not have a wallet.")

            charge_amount = Decimal('5.00')
            if instance.template and instance.template.tool:
                charge_amount = instance.template.tool.price or Decimal('5.00')

            if user.wallet.balance < charge_amount:
                raise serializers.ValidationError(f"Insufficient funds to remove watermark. Required: {charge_amount}")

            user.wallet.debit(charge_amount, description=f"Document purchase: {instance.name}")

            svg = validated_data.get("svg")
            if svg:
                validated_data["svg"] = WaterMark().remove_watermark(svg)

    def update(self, instance, validated_data):
        field_updates = validated_data.pop("field_updates", None)
        if field_updates:
            base_svg = validated_data.get("svg", instance.svg)
            form_fields = instance.form_fields or []
            updated_svg, updated_fields = update_svg_from_field_updates(base_svg, form_fields, field_updates)
            validated_data["svg"] = updated_svg
            validated_data.pop("form_fields", None)
        self.charge_if_test_false(instance, validated_data, is_update=True)
        return super().update(instance, validated_data)

    def create(self, validated_data):
        field_updates = validated_data.pop("field_updates", None)
        template = validated_data.get("template")
        if field_updates:
            if not template:
                raise serializers.ValidationError(
                    {"field_updates": "Template is required when submitting field updates."}
                )
            template = Template.objects.only('svg', 'form_fields').get(pk=template.pk)
            base_svg = template.svg
            form_fields = template.form_fields or []
            updated_svg, updated_fields = update_svg_from_field_updates(base_svg, form_fields, field_updates)
            validated_data["svg"] = updated_svg
            validated_data.pop("form_fields", None)
        elif template and "svg" not in validated_data:
            template = Template.objects.only('svg').get(pk=template.pk)
            validated_data["svg"] = template.svg
        
        temp_instance = self.Meta.model(**validated_data)
        self.charge_if_test_false(temp_instance, validated_data, is_update=False)
        
        template = validated_data.get('template')
        if template and 'keywords' not in validated_data:
            validated_data['keywords'] = list(template.keywords) if template.keywords else []
        
        return super().create(validated_data)

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        view = self.context.get('view')
        
        if view and view.action == 'list':
            representation.pop('form_fields', None)
            representation.pop('svg', None)
        elif view and view.action == 'retrieve':
            # User-side needs raw SVG to avoid CORS issues with CDN
            # So we do NOT pop 'svg' here anymore
            if instance.svg_file:
                representation.pop('svg', None)
        else:
            if instance.test and 'svg' in representation and representation['svg']:
                representation['svg'] = WaterMark().add_watermark(representation['svg'])
        
        return representation
    
    def get_tool_price(self, obj):
        template = obj.template
        if template and template.tool:
            return template.tool.price
        return None

    def get_banner(self, obj):
        template = obj.template
        if not template or not template.banner:
            return None
        request = self.context.get('request')
        banner_url = template.banner.url
        if request and hasattr(request, 'build_absolute_uri'):
            return request.build_absolute_uri(banner_url)
        return banner_url

    def get_svg_url(self, obj):
        if obj.svg_file:
            return get_signed_url(obj.svg_file)
        return None
