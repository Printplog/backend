from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAdminUser
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from django.db.models import Sum, Count, Q
from django.utils import timezone
from datetime import timedelta
from api.serializers.wallet import WalletSerializer, TransactionSerializer
from api.utils.admin_ranges import get_admin_date_range, get_range_label, parse_days_param
from wallet.models import Wallet, Transaction

class WalletStatsView(APIView):
    """Get wallet statistics for admin dashboard"""
    permission_classes = [IsAdminUser]
    
    def get(self, request):
        start_datetime, end_datetime, range_label, days = get_admin_date_range(
            days_param=request.GET.get('days'),
            date_str=request.GET.get('date')
        )

        # Total balance — regular users only (excludes admin/staff wallets)
        total_balance = Wallet.objects.filter(
            user__is_staff=False, user__is_superuser=False
        ).aggregate(total=Sum('balance'))['total'] or 0

        period_transactions = Transaction.objects.filter(
            created_at__range=(start_datetime, end_datetime),
            status=Transaction.Status.COMPLETED,
            wallet__user__is_staff=False,
            wallet__user__is_superuser=False,
        )

        total_inflow = period_transactions.filter(
            type=Transaction.Type.DEPOSIT
        ).aggregate(total=Sum('amount'))['total'] or 0

        total_outflow = period_transactions.filter(
            type=Transaction.Type.PAYMENT
        ).aggregate(total=Sum('amount'))['total'] or 0

        transaction_count = period_transactions.count()
        funded_wallets = period_transactions.filter(
            type=Transaction.Type.DEPOSIT
        ).aggregate(total=Count('wallet_id', distinct=True))['total'] or 0

        all_time_earned = Transaction.objects.filter(
            type=Transaction.Type.PAYMENT,
            status=Transaction.Status.COMPLETED,
            wallet__user__is_staff=False,
            wallet__user__is_superuser=False,
        ).exclude(
            description__startswith="Admin adjustment:"
        ).aggregate(total=Sum('amount'))['total'] or 0

        response = Response({
            'totalBalance': float(total_balance),
            'totalInflow': float(total_inflow),
            'totalOutflow': abs(float(total_outflow)),
            'allTimeEarned': abs(float(all_time_earned)),
            'transactionCount': transaction_count,
            'fundedWallets': funded_wallets,
            'rangeDays': days,
            'rangeLabel': range_label,
        })
        response["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response


class WalletListView(APIView):
    """List all user wallets"""
    permission_classes = [IsAdminUser]
    
    def get(self, request):
        # Only list wallets of regular users (excludes admin/staff)
        wallets = Wallet.objects.select_related('user').filter(
            user__is_staff=False, user__is_superuser=False
        )
        search = request.GET.get('search', '').strip()
        balance_filter = request.GET.get('balance', 'all').strip().lower()
        joined_filter = request.GET.get('joined', 'all').strip().lower()
        sort_by = request.GET.get('sort', 'balance-desc').strip().lower()
        page_size = int(request.GET.get('page_size', 10))

        if search:
            wallets = wallets.filter(
                Q(user__username__icontains=search) |
                Q(user__email__icontains=search)
            )

        if balance_filter == 'positive':
            wallets = wallets.filter(balance__gt=0)
        elif balance_filter == 'zero':
            wallets = wallets.filter(balance=0)
        elif balance_filter == '100plus':
            wallets = wallets.filter(balance__gte=100)
        elif balance_filter == '1000plus':
            wallets = wallets.filter(balance__gte=1000)

        if joined_filter in {'7', '30', '180', '365'}:
            joined_days = int(joined_filter)
            joined_cutoff = timezone.now() - timedelta(days=joined_days)
            wallets = wallets.filter(user__date_joined__gte=joined_cutoff)

        ordering_map = {
            'balance-desc': ['-balance', 'user__username'],
            'balance-asc': ['balance', 'user__username'],
            'recent': ['-user__date_joined', 'user__username'],
            'oldest': ['user__date_joined', 'user__username'],
            'name': ['user__username'],
        }
        wallets = wallets.order_by(*ordering_map.get(sort_by, ordering_map['balance-desc']))

        paginator = PageNumberPagination()
        paginator.page_size = page_size
        paginated_wallets = paginator.paginate_queryset(wallets, request)
        serializer = WalletSerializer(paginated_wallets, many=True)

        return Response({
            'results': serializer.data,
            'count': paginator.page.paginator.count,
            'next': paginator.get_next_link(),
            'previous': paginator.get_previous_link(),
            'current_page': paginator.page.number,
            'total_pages': paginator.page.paginator.num_pages,
            'filters': {
                'search': search,
                'balance': balance_filter,
                'joined': joined_filter,
                'sort': sort_by,
            },
        })


class WalletAdjustView(APIView):
    """Manually adjust user wallet balance"""
    permission_classes = [IsAdminUser]
    
    def post(self, request):
        wallet_id = request.data.get('walletId')
        adjustment_type = request.data.get('type')  # 'credit' or 'debit'
        amount = request.data.get('amount')
        reason = request.data.get('reason', '')
        
        if not all([wallet_id, adjustment_type, amount]):
            return Response(
                {'error': 'Missing required fields'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            wallet = Wallet.objects.get(id=wallet_id)
            
            if adjustment_type == 'credit':
                wallet.credit(amount, description=f"Admin adjustment: {reason}")
            elif adjustment_type == 'debit':
                wallet.debit(amount, description=f"Admin adjustment: {reason}")
            else:
                return Response(
                    {'error': 'Invalid adjustment type'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            return Response({'message': 'Balance adjusted successfully'})
        except Wallet.DoesNotExist:
            return Response(
                {'error': 'Wallet not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


class PendingRequestsView(APIView):
    """List pending funding requests"""
    permission_classes = [IsAdminUser]
    
    def get(self, request):
        # TODO: Implement when FundingRequest model exists
        # For now, return empty list
        return Response([])


class ApproveRequestView(APIView):
    """Approve a funding request"""
    permission_classes = [IsAdminUser]
    
    def post(self, request):
        # TODO: Implement when FundingRequest model exists
        return Response({'message': 'Request approved'})


class RejectRequestView(APIView):
    """Reject a funding request"""
    permission_classes = [IsAdminUser]
    
    def post(self, request):
        # TODO: Implement when FundingRequest model exists
        return Response({'message': 'Request rejected'})


class TransactionHistoryView(APIView):
    """Get all wallet transactions"""
    permission_classes = [IsAdminUser]

    def get(self, request):
        search = request.GET.get('search', '').strip()
        type_filter = request.GET.get('type', 'all').strip().lower()
        status_filter = request.GET.get('status', 'all').strip().lower()
        page_size = int(request.GET.get('page_size', 20))

        days_param = request.GET.get('days')
        date_str = request.GET.get('date')
        start_datetime, end_datetime, range_label, days = get_admin_date_range(
            days_param=days_param,
            date_str=date_str
        )

        transactions = Transaction.objects.select_related('wallet__user').filter(
            wallet__user__is_staff=False,
            wallet__user__is_superuser=False,
        )
        # Only restrict the table when a range was explicitly requested;
        # otherwise the default 1-day window silently empties the list
        # and the paginator dead-ends at "Page 1 of 1".
        if days_param or date_str:
            transactions = transactions.filter(created_at__range=(start_datetime, end_datetime))

        if search:
            transactions = transactions.filter(
                Q(wallet__user__username__icontains=search) |
                Q(wallet__user__email__icontains=search) |
                Q(description__icontains=search) |
                Q(tx_id__icontains=search)
            )

        if type_filter == 'credit':
            transactions = transactions.filter(type=Transaction.Type.DEPOSIT)
        elif type_filter == 'debit':
            transactions = transactions.filter(type=Transaction.Type.PAYMENT)

        if status_filter in {
            Transaction.Status.COMPLETED,
            Transaction.Status.PENDING,
            Transaction.Status.FAILED,
        }:
            transactions = transactions.filter(status=status_filter)

        transactions = transactions.order_by('-created_at')

        paginator = PageNumberPagination()
        paginator.page_size = page_size
        paginated_transactions = paginator.paginate_queryset(transactions, request)
        serializer = TransactionSerializer(paginated_transactions, many=True)
        
        # Calculate stats
        now = timezone.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        total_volume = Transaction.objects.filter(
            type='deposit',
            wallet__user__is_staff=False,
            wallet__user__is_superuser=False,
            created_at__range=(start_datetime, end_datetime),
        ).aggregate(total=Sum('amount'))['total'] or 0
        
        stats = {
            'total_count': transactions.count(),
            'total_volume': float(total_volume),
            'month_count': Transaction.objects.filter(
                created_at__gte=month_start,
                wallet__user__is_staff=False,
                wallet__user__is_superuser=False,
            ).count(),
            'pending_count': Transaction.objects.filter(
                status='pending',
                wallet__user__is_staff=False,
                wallet__user__is_superuser=False,
            ).count(),
        }
        
        return Response({
            'transactions': serializer.data,
            'count': paginator.page.paginator.count,
            'next': paginator.get_next_link(),
            'previous': paginator.get_previous_link(),
            'current_page': paginator.page.number,
            'total_pages': paginator.page.paginator.num_pages,
            'stats': stats,
            'filters': {
                'search': search,
                'type': type_filter,
                'status': status_filter,
            },
        })
