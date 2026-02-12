# templates/serializers.py
from rest_framework import serializers
from django.db.models import Sum, Count
from django.contrib.auth import get_user_model
from django.core.cache import cache
from api.watermark import WaterMark
from .models import Template, PurchasedTemplate, Tool, Tutorial, Font, SiteSettings, TransformVariable
from wallet.models import Wallet
from rest_framework.pagination import PageNumberPagination
from django.utils import timezone
from datetime import timedelta
from accounts.serializers import CustomUserDetailsSerializer
from .svg_updater import update_svg_from_field_updates
from decimal import Decimal
import hashlib
import json

User = get_user_model()


class FieldUpdateSerializer(serializers.Serializer):
    id = serializers.CharField()
    value = serializers.JSONField(required=False, allow_null=True)


class ToolSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tool
        fields = '__all__'


class TransformVariableSerializer(serializers.ModelSerializer):
    class Meta:
        model = TransformVariable
        fields = '__all__'


class FontSerializer(serializers.ModelSerializer):
    font_url = serializers.SerializerMethodField()
    
    class Meta:
        model = Font
        fields = ['id', 'name', 'family', 'weight', 'style', 'font_file', 'font_url', 'created_at']
        read_only_fields = ['id', 'created_at']
    
    def get_font_url(self, obj):
        """Return absolute URL for font file"""
        request = self.context.get('request')
        if obj.font_file and request:
            return request.build_absolute_uri(obj.font_file.url)
        return obj.font_file.url if obj.font_file else None


class AdminOverviewSerializer(serializers.Serializer):
    total_downloads = serializers.IntegerField()
    total_users = serializers.IntegerField()
    total_purchased_docs = serializers.IntegerField()
    total_wallet_balance = serializers.DecimalField(max_digits=12, decimal_places=2)
    
    def get_total_downloads(self):
        """Get total downloads across all users"""
        return User.objects.aggregate(
            total=Sum('downloads')
        )['total'] or 0
    
    def get_total_users(self):
        """Get total number of users"""
        return User.objects.count()
    
    def get_total_purchased_docs(self):
        """Get total number of paid documents (excluding test documents)"""
        return PurchasedTemplate.objects.filter(
            test=False  # Only count paid documents, not test ones
        ).count()
    
    def get_total_wallet_balance(self):
        """Get total wallet balance across all users"""
        return Wallet.objects.aggregate(
            total=Sum('balance')
        )['total'] or 0


class AdminUsersSerializer(serializers.Serializer):
    """Serializer specifically for Admin Users page"""
    all_users = serializers.IntegerField()
    new_users = serializers.DictField()
    total_purchases_users = serializers.DictField()
    users = serializers.ListField()
    
    def get_all_users(self):
        """Get total number of users"""
        return User.objects.count()
    
    def get_new_users_stats(self):
        """Get new users statistics for different time periods"""
        now = timezone.now()
        
        # Calculate date ranges
        today = now.date()
        seven_days_ago = today - timedelta(days=7)
        fourteen_days_ago = today - timedelta(days=14)
        thirty_days_ago = today - timedelta(days=30)
        
        # Count new users for each period
        today_users = User.objects.filter(date_joined__date=today).count()
        seven_days_users = User.objects.filter(date_joined__date__gte=seven_days_ago).count()
        fourteen_days_users = User.objects.filter(date_joined__date__gte=fourteen_days_ago).count()
        thirty_days_users = User.objects.filter(date_joined__date__gte=thirty_days_ago).count()
        
        return {
            'today': today_users,
            'past_7_days': seven_days_users,
            'past_14_days': fourteen_days_users,
            'past_30_days': thirty_days_users,
        }
    
    def get_total_purchases_users_stats(self):
        """Get users with purchases statistics for different time periods"""
        now = timezone.now()
        
        # Calculate date ranges
        today = now.date()
        seven_days_ago = today - timedelta(days=7)
        fourteen_days_ago = today - timedelta(days=14)
        thirty_days_ago = today - timedelta(days=30)
        
        # Count users with purchases for each period
        today_purchases = User.objects.filter(
            purchased_templates__test=False,
            purchased_templates__created_at__date=today
        ).distinct().count()
        
        seven_days_purchases = User.objects.filter(
            purchased_templates__test=False,
            purchased_templates__created_at__date__gte=seven_days_ago
        ).distinct().count()
        
        fourteen_days_purchases = User.objects.filter(
            purchased_templates__test=False,
            purchased_templates__created_at__date__gte=fourteen_days_ago
        ).distinct().count()
        
        thirty_days_purchases = User.objects.filter(
            purchased_templates__test=False,
            purchased_templates__created_at__date__gte=thirty_days_ago
        ).distinct().count()
        
        return {
            'today': today_purchases,
            'past_7_days': seven_days_purchases,
            'past_14_days': fourteen_days_purchases,
            'past_30_days': thirty_days_purchases,
        }
    
    def get_paginated_users(self, page=1, page_size=10):
        """Get paginated user data"""
        paginator = PageNumberPagination()
        paginator.page_size = page_size
        
        users = User.objects.all().order_by('-date_joined')
        paginated_users = paginator.paginate_queryset(users, None)
        
        user_serializer = CustomUserDetailsSerializer(paginated_users, many=True)
        return {
            'results': user_serializer.data,
            'count': paginator.page.paginator.count,
            'next': paginator.get_next_link(),
            'previous': paginator.get_previous_link(),
            'current_page': page,
            'total_pages': paginator.page.paginator.num_pages,
        }


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
    tool_price = serializers.SerializerMethodField()
    
    class Meta:
        model = Template
        fields = '__all__'
    
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
                # Fallback: check if we're in production
                from django.conf import settings
                if getattr(settings, 'ENV', 'development') == 'production':
                    # Use production domain
                    representation['banner'] = f"https://api.sharptoolz.com{representation['banner']}"
        
        if view and view.action == 'list':
            # For list view: remove SVG and form_fields, keep banner
            representation.pop('svg', None)
            representation.pop('form_fields', None)
            # Banner will be included automatically since it's in fields
        elif view and view.action == 'retrieve':
            # For detail view: exclude SVG (will be loaded separately for better UX)
            # Keep form_fields so forms can load immediately
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
    tool_price = serializers.SerializerMethodField()
    
    class Meta:
        model = Template
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at', 'form_fields')
    
    def get_tool_price(self, obj):
        return obj.tool.price if obj.tool else None

    def create(self, validated_data):
        fonts_data = validated_data.pop('fonts', None)
        template = Template.objects.create(**validated_data)
        if fonts_data:
            template.fonts.set(fonts_data)
        return template
    
    def update(self, instance, validated_data):
        # Remove form_fields if present (should be read-only, but extra safety)
        if 'form_fields' in validated_data:
            validated_data.pop('form_fields', None)
        
        fonts_data = validated_data.pop('fonts', None)
        
        # Skip expensive SVG re-parsing for admin edits
        # The form_fields are read-only and shouldn't be regenerated on admin updates
        # This avoids: DB query + full SVG string comparison + SVG parsing + file save
        if 'svg' in validated_data:
            instance.skip_svg_parse = True
        
        instance = super().update(instance, validated_data)
        
        if fonts_data is not None:
            instance.fonts.set(fonts_data)
        
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
                # Fallback: check if we're in production
                from django.conf import settings
                if getattr(settings, 'ENV', 'development') == 'production':
                    # Use production domain
                    representation['banner'] = f"https://api.sharptoolz.com{representation['banner']}"
        
        # For list view: remove SVG and form_fields, keep banner
        if view and view.action == 'list':
            representation.pop('svg', None)
            representation.pop('form_fields', None)
            # Banner will be included automatically since it's in fields
        elif view and view.action == 'retrieve':
            # For detail view: exclude SVG (will be loaded separately for better UX)
            # Keep form_fields so forms can load immediately
            representation.pop('svg', None)
        else:
            # For other actions (create, update): include SVG if present
            # No watermark processing - admin gets clean templates
            pass
        
        return representation



class PurchasedTemplateSerializer(serializers.ModelSerializer):
    field_updates = FieldUpdateSerializer(many=True, write_only=True, required=False)
    fonts = FontSerializer(many=True, read_only=True)
    banner = serializers.SerializerMethodField()
    tool_price = serializers.SerializerMethodField()
    
    class Meta:
        model = PurchasedTemplate
        fields = '__all__'
        read_only_fields = ('buyer',)
        extra_kwargs = {
            'svg': {'required': False},
        }
        
    def charge_if_test_false(self, instance, validated_data, is_update=False):
        old_test = instance.test if is_update else True  # Assume default True for new records
        new_test = validated_data.get("test", old_test)

        # Charge only if test changes from True to False
        if old_test is True and new_test is False:
            user = instance.buyer

            if not hasattr(user, "wallet"):
                raise serializers.ValidationError("User does not have a wallet.")

            # Get charge amount from tool price, default to 5.00
            charge_amount = Decimal('5.00')
            if instance.template and instance.template.tool:
                charge_amount = instance.template.tool.price or Decimal('5.00')

            if user.wallet.balance < charge_amount:
                raise serializers.ValidationError(f"Insufficient funds to remove watermark. Required: {charge_amount}")

            user.wallet.debit(charge_amount, description=f"Document purchase: {instance.name}")

            # Remove watermark from SVG only when test changes from True to False
            svg = validated_data.get("svg")
            if svg:
                validated_data["svg"] = WaterMark().remove_watermark(svg)

    def update(self, instance, validated_data):
        field_updates = validated_data.pop("field_updates", None)
        if field_updates:
            base_svg = validated_data.get("svg", instance.svg)
            form_fields = instance.form_fields or []
            updated_svg, updated_fields = update_svg_from_field_updates(base_svg, form_fields, field_updates)
            # Only save the updated SVG - let model.save() parse it to regenerate form_fields
            validated_data["svg"] = updated_svg
            # Remove form_fields from validated_data so save() will parse from the updated SVG
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
            # Optimize: Only fetch SVG and form_fields from template
            template = Template.objects.only('svg', 'form_fields').get(pk=template.pk)
            base_svg = template.svg
            form_fields = template.form_fields or []
            updated_svg, updated_fields = update_svg_from_field_updates(base_svg, form_fields, field_updates)
            # Only save the updated SVG - let model.save() parse it to regenerate form_fields
            validated_data["svg"] = updated_svg
            # Remove form_fields from validated_data so save() will parse from the updated SVG
            validated_data.pop("form_fields", None)
        elif template and "svg" not in validated_data:
            # Optimize: Only fetch SVG from template
            template = Template.objects.only('svg').get(pk=template.pk)
            validated_data["svg"] = template.svg
        
        # Create a temporary instance to simulate access to `buyer` and `test`
        temp_instance = self.Meta.model(**validated_data)
        self.charge_if_test_false(temp_instance, validated_data, is_update=False)
        
        # Copy keywords from template if not provided
        template = validated_data.get('template')
        if template and 'keywords' not in validated_data:
            validated_data['keywords'] = list(template.keywords) if template.keywords else []
        
        return super().create(validated_data)

    def to_representation(self, instance):
        # Get the base representation
        representation = super().to_representation(instance)
        request = self.context.get('request')
        view = self.context.get('view')
        
        # Remove heavy fields on list view and provide banner preview
        if view and view.action == 'list':
            representation.pop('form_fields', None)
            representation.pop('svg', None)
        elif view and view.action == 'retrieve':
            # For detail view: exclude SVG (will be loaded separately for better UX)
            # Keep form_fields so forms can load immediately
            representation.pop('svg', None)
        else:
            # For other actions (create, update): add watermark to SVG if it's a test template
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

class SiteSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = SiteSettings
        fields = ['crypto_address', 'whatsapp_number', 'manual_purchase_text', 'updated_at']
        read_only_fields = ['updated_at']
    

    