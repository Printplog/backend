from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from django.db.models import Q, Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from datetime import timedelta
from django.shortcuts import get_object_or_404
from django.contrib.auth import get_user_model

from django.core.cache import cache
from ..models import PurchasedTemplate
from wallet.models import Wallet
from ..serializers import AdminOverviewSerializer
from ..permissions import IsAdminOrReadOnly, IsSuperUser
from accounts.serializers import CustomUserDetailsSerializer

User = get_user_model()

class AdminOverview(APIView):
    permission_classes = [IsAdminOrReadOnly]
    
    def get(self, request):
        """
        Get admin overview statistics with optimized queries and caching.
        """
        # Try to get from cache first
        cache_key = "admin_overview_stats"
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data, status=status.HTTP_200_OK)
            
        serializer = AdminOverviewSerializer()
        now = timezone.now()
        thirty_days_ago = now.date() - timedelta(days=30)
        
        # 1. Get documents chart data - optimized with single query
        documents_data = (
            PurchasedTemplate.objects
            .filter(created_at__date__gte=thirty_days_ago)
            .annotate(date=TruncDate('created_at'))
            .values('date')
            .annotate(
                total=Count('id'),
                paid=Count('id', filter=Q(test=False)),
                test=Count('id', filter=Q(test=True))
            )
            .order_by('date')
        )
        
        documents_chart = [
            {
                'date': item['date'].isoformat(),
                'total': item['total'],
                'paid': item['paid'],
                'test': item['test']
            }
            for item in documents_data
        ]
        
        # 2. Get user growth data - optimized (no loop)
        user_growth_data = (
            User.objects
            .filter(date_joined__date__gte=thirty_days_ago)
            .annotate(date=TruncDate('date_joined'))
            .values('date')
            .annotate(count=Count('id'))
            .order_by('date')
        )
        
        # Calculate cumulative users
        total_users_before_thirty_days = User.objects.filter(date_joined__date__lt=thirty_days_ago).count()
        current_cumulative = total_users_before_thirty_days
        
        growth_lookup = {item['date']: item['count'] for item in user_growth_data}
        revenue_chart = []
        
        total_downloads = serializer.get_total_downloads()
        
        for i in range(30):
            date = thirty_days_ago + timedelta(days=i+1)
            count_on_day = growth_lookup.get(date, 0)
            current_cumulative += count_on_day
            revenue_chart.append({
                'date': date.isoformat(),
                'users': current_cumulative,
                'downloads': total_downloads
            })
        
        data = {
            'total_downloads': total_downloads,
            'total_users': serializer.get_total_users(),
            'total_purchased_docs': serializer.get_total_purchased_docs(),
            'total_wallet_balance': serializer.get_total_wallet_balance() if request.user.is_superuser else None,
            'documents_chart': documents_chart,
            'revenue_chart': revenue_chart,
        }
        
        # Cache for 5 minutes
        cache.set(cache_key, data, 300)
        
        return Response(data, status=status.HTTP_200_OK)


class AdminUsers(APIView):
    permission_classes = [IsSuperUser]
    
    def get(self, request):
        """
        Get users data with optimized statistics aggregation.
        """
        try:
            # Get query parameters
            page = int(request.GET.get('page', 1))
            page_size = int(request.GET.get('page_size', 10))
            search = request.GET.get('search', '').strip()
            
            # Base queryset for users list (pagination)
            users_queryset = User.objects.all()
            if search:
                users_queryset = users_queryset.filter(
                    Q(username__icontains=search) | 
                    Q(email__icontains=search)
                )
            
            # Statistics Caching (stats don't need to be recalculated on every page change)
            stats_cache_key = f"admin_user_stats_{hash(search)}"
            stats_data = cache.get(stats_cache_key)
            
            if not stats_data:
                now = timezone.now()
                today = now.date()
                intervals = {
                    'today': today,
                    'past_7_days': today - timedelta(days=7),
                    'past_14_days': today - timedelta(days=14),
                    'past_30_days': today - timedelta(days=30),
                }
                
                # Optimized stats aggregation
                new_users = User.objects.aggregate(
                    today=Count('id', filter=Q(date_joined__date=intervals['today'])),
                    past_7_days=Count('id', filter=Q(date_joined__date__gte=intervals['past_7_days'])),
                    past_14_days=Count('id', filter=Q(date_joined__date__gte=intervals['past_14_days'])),
                    past_30_days=Count('id', filter=Q(date_joined__date__gte=intervals['past_30_days'])),
                )
                
                # Fetch purchase stats - combined query
                from ..models import PurchasedTemplate
                purchases_stats = PurchasedTemplate.objects.filter(test=False).aggregate(
                    today=Count('buyer_id', filter=Q(created_at__date=intervals['today']), distinct=True),
                    past_7_days=Count('buyer_id', filter=Q(created_at__date__gte=intervals['past_7_days']), distinct=True),
                    past_14_days=Count('buyer_id', filter=Q(created_at__date__gte=intervals['past_14_days']), distinct=True),
                    past_30_days=Count('buyer_id', filter=Q(created_at__date__gte=intervals['past_30_days']), distinct=True),
                )
                
                stats_data = {
                    'all_users': User.objects.count() if not search else users_queryset.count(),
                    'new_users': new_users,
                    'total_purchases_users': purchases_stats,
                }
                cache.set(stats_cache_key, stats_data, 300) # Cache for 5 mins

            # Pagination
            paginator = PageNumberPagination()
            paginator.page_size = page_size
            
            users_queryset = users_queryset.order_by('-date_joined')
            paginated_users = paginator.paginate_queryset(users_queryset, request)
            
            user_serializer = CustomUserDetailsSerializer(paginated_users, many=True)
            
            users_list_data = {
                'results': user_serializer.data,
                'count': paginator.page.paginator.count,
                'next': paginator.get_next_link(),
                'previous': paginator.get_previous_link(),
                'current_page': page,
                'total_pages': paginator.page.paginator.num_pages,
            }
            
            return Response({
                **stats_data,
                'users': users_list_data,
                'search_term': search,
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            return Response(
                {'error': 'Internal server error', 'details': str(e)}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class AdminUserDetails(APIView):
    permission_classes = [IsSuperUser]
    
    def get(self, request, user_id):
        try:
            user = get_object_or_404(User, id=user_id)
            user_serializer = CustomUserDetailsSerializer(user)
            
            # Wallet data
            wallet_data = {
                'id': None,
                'balance': 0.0,
                'created_at': user.date_joined.isoformat(),
            }
            if hasattr(user, 'wallet'):
                wallet = user.wallet
                wallet_data = {
                    'id': str(wallet.id),
                    'balance': float(wallet.balance),
                    'created_at': wallet.created_at.isoformat() if hasattr(wallet, 'created_at') else user.date_joined.isoformat(),
                }
            
            # Purchase history
            purchases = user.purchased_templates.all().order_by('-created_at')
            purchase_history = [{
                'id': str(p.id),
                'template_name': p.template.name if p.template else "Deleted Template",
                'name': p.name,
                'test': p.test,
                'tracking_id': p.tracking_id,
                'created_at': p.created_at.isoformat(),
                'updated_at': p.updated_at.isoformat(),
            } for p in purchases]
            
            # Transaction history
            transaction_history = []
            if hasattr(user, 'wallet'):
                transactions = user.wallet.transactions.all().order_by('-created_at')
                transaction_history = [{
                    'id': str(t.id),
                    'type': t.type,
                    'amount': float(t.amount),
                    'status': t.status,
                    'description': t.description,
                    'tx_id': t.tx_id,
                    'address': t.address,
                    'created_at': t.created_at.isoformat(),
                } for t in transactions]
            
            # Stats
            stats = {
                'total_purchases': user.purchased_templates.count(),
                'paid_purchases': user.purchased_templates.filter(test=False).count(),
                'test_purchases': user.purchased_templates.filter(test=True).count(),
                'total_downloads': getattr(user, 'downloads', 0),
                'days_since_joined': (timezone.now() - user.date_joined).days,
            }
            
            return Response({
                'user': user_serializer.data,
                'wallet': wallet_data,
                'purchase_history': purchase_history,
                'transaction_history': transaction_history,
                'stats': stats,
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            return Response(
                {'error': 'Internal server error', 'details': str(e)}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def patch(self, request, user_id):
        try:
            user = get_object_or_404(User, id=user_id)
            role = request.data.get('role')
            
            if role:
                from accounts.serializers import ROLE_CODES
                if role == ROLE_CODES["staff"]:
                    user.is_superuser = False
                    user.is_staff = True
                elif role == ROLE_CODES["user"]:
                    user.is_superuser = False
                    user.is_staff = False
                else:
                    return Response({'error': 'Invalid role code or promotion not allowed'}, status=status.HTTP_400_BAD_REQUEST)
                
            # Also allow toggling is_active if needed
            is_active = request.data.get('is_active')
            if is_active is not None:
                user.is_active = bool(is_active)
                
            user.save()
            
            # Log action
            from analytics.utils import log_action
            log_action(
                actor=request.user,
                action="UPDATE_USER",
                target=f"{user.username} ({user.id})",
                ip_address=request.META.get('REMOTE_ADDR'),
                details=request.data
            )
            
            # Return updated user details
            user_serializer = CustomUserDetailsSerializer(user)
            return Response({
                'message': 'User updated successfully',
                'user': user_serializer.data
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            return Response(
                {'error': 'Internal server error', 'details': str(e)}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def delete(self, request, user_id):
        try:
            user = get_object_or_404(User, id=user_id)
            if user.is_superuser:
                return Response({'error': 'Cannot delete superuser'}, status=status.HTTP_400_BAD_REQUEST)
            
            user_info = {'id': user.id, 'username': user.username, 'email': user.email}
            
            # Log action before delete
            from analytics.utils import log_action
            log_action(
                actor=request.user,
                action="DELETE_USER",
                target=f"{user.username} ({user.id})",
                ip_address=request.META.get('REMOTE_ADDR')
            )
            
            user.delete()
            return Response({'message': 'User deleted successfully', 'deleted_user': user_info}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': 'Internal server error', 'details': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AdminDocuments(APIView):
    """Admin-only paginated view of all purchased templates with search."""
    permission_classes = [IsAdminOrReadOnly]

    def get(self, request):
        try:
            page_size = int(request.GET.get('page_size', 20))
            search = request.GET.get('search', '').strip()

            queryset = (
                PurchasedTemplate.objects
                .select_related('buyer', 'template', 'template__tool')
                .defer('form_fields', 'svg_file')
                .order_by('-created_at')
            )

            if search:
                queryset = queryset.filter(
                    Q(name__icontains=search) |
                    Q(buyer__username__icontains=search) |
                    Q(buyer__email__icontains=search) |
                    Q(tracking_id__icontains=search) |
                    Q(template__name__icontains=search)
                )

            paginator = PageNumberPagination()
            paginator.page_size = page_size
            paginated_qs = paginator.paginate_queryset(queryset, request)

            results = [
                {
                    'id': str(doc.id),
                    'name': doc.name,
                    'test': doc.test,
                    'tracking_id': doc.tracking_id,
                    'created_at': doc.created_at.isoformat(),
                    'updated_at': doc.updated_at.isoformat(),
                    'buyer': {
                        'id': doc.buyer.id,
                        'username': doc.buyer.username,
                        'email': doc.buyer.email,
                    } if doc.buyer else None,
                    'template': {
                        'id': str(doc.template.id),
                        'name': doc.template.name,
                    } if doc.template else None,
                }
                for doc in paginated_qs
            ]

            return Response({
                'results': results,
                'count': paginator.page.paginator.count,
                'total_pages': paginator.page.paginator.num_pages,
                'current_page': paginator.page.number,
                'next': paginator.get_next_link(),
                'previous': paginator.get_previous_link(),
            }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {'error': 'Internal server error', 'details': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
