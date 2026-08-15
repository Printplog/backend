from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.db.models import Sum
from wallet.models import Wallet
from .purchases import PurchasedTemplate  # Import from local if needed or use string
from django.utils import timezone
from datetime import timedelta
from accounts.serializers import CustomUserDetailsSerializer
from rest_framework.pagination import PageNumberPagination

User = get_user_model()

class AdminOverviewSerializer(serializers.Serializer):
    total_downloads = serializers.IntegerField()
    total_users = serializers.IntegerField()
    regular_users = serializers.IntegerField()
    staff_users = serializers.IntegerField()
    total_purchased_docs = serializers.IntegerField()
    total_wallet_balance = serializers.DecimalField(max_digits=12, decimal_places=2)
    external_users = serializers.IntegerField()
    active_external_users = serializers.IntegerField()

    def get_total_downloads(self):
        """Get total downloads across all users"""
        return User.objects.aggregate(
            total=Sum('downloads')
        )['total'] or 0

    def get_total_users(self):
        """Get total number of all users (including staff/admin)"""
        return User.objects.count()

    def get_regular_users(self):
        """Get count of regular users only (not staff or superuser)"""
        return User.objects.filter(is_staff=False, is_superuser=False).count()

    def get_staff_users(self):
        """Get count of staff and admin users"""
        return User.objects.filter(is_staff=True).count() + User.objects.filter(is_superuser=True, is_staff=False).count()

    def get_total_purchased_docs(self):
        """Get total number of paid documents (excluding test documents)"""
        from ..models import PurchasedTemplate
        return PurchasedTemplate.objects.filter(
            test=False
        ).count()

    def _external_user_pairs(self, since=None):
        """
        End users belong to the API customer that owns them — `external_user_id`
        is only opaque and unique within one customer, so "user_42" from two
        customers is two different people. Identity is therefore the
        (customer, external_user_id) pair, never the id on its own.

        Counted across both documents and embed sessions, so an end user who
        opened a hosted form without finishing a document still counts.
        """
        from ..models import EmbedSession, PurchasedTemplate

        documents = (
            PurchasedTemplate.objects
            .exclude(external_user_id="")
            .values_list("buyer_id", "external_user_id")
            .order_by()
        )
        sessions = (
            EmbedSession.objects
            .exclude(external_user_id="")
            .values_list("user_id", "external_user_id")
            .order_by()
        )

        if since is not None:
            documents = documents.filter(created_at__gte=since)
            sessions = sessions.filter(created_at__gte=since)

        # union() is DISTINCT by default, so de-duplication happens in the
        # database rather than by pulling every pair into memory.
        return documents.union(sessions)

    def get_external_users(self):
        """Distinct end users API customers have brought in, all time."""
        return self._external_user_pairs().count()

    def get_active_external_users(self, since):
        """Distinct end users seen since `since`."""
        return self._external_user_pairs(since=since).count()

    def get_total_wallet_balance(self):
        """Get total wallet balance for regular users only (excludes admin/staff)"""
        return Wallet.objects.filter(
            user__is_staff=False,
            user__is_superuser=False
        ).aggregate(total=Sum('balance'))['total'] or 0


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
