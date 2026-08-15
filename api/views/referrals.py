from rest_framework import viewsets, permissions, status
from rest_framework.response import Response
from rest_framework.decorators import action
from api.models import Referral, SiteSettings
from api.serializers.referral import ReferralSerializer
from django.db.models import Count, Sum
from django.contrib.auth import get_user_model
from django.conf import settings as django_settings

User = get_user_model()

class ReferralViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for users to view their referrals and rewards.
    """
    serializer_class = ReferralSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Referral.objects.filter(referrer=self.request.user)

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """
        Get referral statistics for the current user.
        """
        user = request.user
        wallet = user.wallet
        
        # total_referrals should be the count of users who signed up with this user's link
        total_referrals = User.objects.filter(referred_by=user).count()
        
        # rewarded_referrals are those where a Referral record exists (created on deposit)
        rewarded_referrals = self.get_queryset().filter(is_rewarded=True).count()

        # pending_referrals are signups who haven't made a qualifying deposit yet
        pending_referrals = max(0, total_referrals - rewarded_referrals)
        
        total_earned = self.get_queryset().filter(is_rewarded=True).aggregate(
            total=Sum('reward_amount')
        )['total'] or 0
        
        site_settings = SiteSettings.get_settings()
        
        return Response({
            'total_referrals': total_referrals,
            'rewarded_referrals': rewarded_referrals,
            'pending_referrals': pending_referrals,
            'total_earned': total_earned,
            'withdrawable_balance': wallet.referral_balance,
            'min_withdrawal': site_settings.min_withdrawal_threshold,
            'referral_code': user.username,
            'referral_link': f"{django_settings.FRONTEND_URL}/auth/register?ref={user.username}",
            'reward_percentage': site_settings.referral_percentage,
            'min_deposit_threshold': site_settings.min_referral_deposit,
            'enable_referrals': site_settings.enable_referrals
        })

    @action(detail=False, methods=['post'])
    def request_withdrawal(self, request):
        """
        Submit a withdrawal request for referral earnings.
        """
        from wallet.models import WithdrawalRequest
        from django.db import transaction
        from decimal import Decimal

        amount = request.data.get("amount")
        usdt_address = request.data.get("usdt_address")

        if not amount or not usdt_address:
            return Response({"detail": "Amount and USDT BEP20 address are required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            amount = Decimal(amount)
        except Exception:
            return Response({"detail": "Invalid amount format."}, status=status.HTTP_400_BAD_REQUEST)

        settings = SiteSettings.get_settings()
        if not settings.enable_referrals:
            return Response({"detail": "The referral program is currently disabled."}, status=status.HTTP_403_FORBIDDEN)
            
        if amount < settings.min_withdrawal_threshold:
            return Response({"detail": f"Minimum withdrawal amount is ${settings.min_withdrawal_threshold}."}, status=status.HTTP_400_BAD_REQUEST)

        wallet = request.user.wallet
        if wallet.referral_balance < amount:
            return Response({"detail": "Insufficient referral balance."}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            wallet.referral_balance -= amount
            wallet.save(update_fields=['referral_balance'])
            
            WithdrawalRequest.objects.create(
                user=request.user,
                amount=amount,
                usdt_address=usdt_address,
                status=WithdrawalRequest.Status.PENDING
            )

        return Response({"detail": "Withdrawal request submitted successfully."}, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'])
    def remind_friends(self, request):
        """
        Send a reminder email to all pending referrals.
        """
        site_settings = SiteSettings.get_settings()
        if not site_settings.enable_referrals:
            return Response({"detail": "The referral program is currently disabled."}, status=status.HTTP_403_FORBIDDEN)

        from api.utils.email_service import EmailService
        user = request.user
        
        # Find pending referrals (Users referred by me who haven't been rewarded yet)
        # Note: In our system, a Referral record with is_rewarded=True is created on first deposit.
        # So pending referrals are Users who have referred_by=user BUT no registration_referral where is_rewarded=True
        pending_users = User.objects.filter(
            referred_by=user
        ).exclude(
            registration_referral__is_rewarded=True
        )

        if not pending_users.exists():
            return Response({"detail": "You don't have any pending referrals to remind."}, status=status.HTTP_400_BAD_REQUEST)

        # Anti-spam: Basic rate limiting could be added here (e.g., once per day)
        # For now, we'll just send to all pending users
        # Each of these is queued, so N reminders no longer means N sequential
        # SMTP round-trips held open by this request.
        count = 0
        for friend in pending_users:
            if EmailService.send_referral_reminder(friend.email, friend.username, user.username):
                count += 1

        return Response({"detail": f"Reminders are on their way to {count} friends!"}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], permission_classes=[permissions.AllowAny])
    def leaderboard(self, request):
        """
        Get the top referrers leaderboard based on total earnings.
        """
        from django.db.models import Sum, Q
        # Top 10 users by total reward amount
        top_referrers = User.objects.annotate(
            total_earned=Sum('referral_records__reward_amount', filter=Q(referral_records__is_rewarded=True))
        ).filter(total_earned__gt=0).order_by('-total_earned')[:10]
        
        data = [
            {
                'username': user.username[:3] + '***' if len(user.username) > 3 else user.username,
                'amount': user.total_earned
            }
            for user in top_referrers
        ]
        
        return Response(data)
