from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from django.core.cache import cache
from django.db.models import Q, Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from datetime import timedelta
from django.shortcuts import get_object_or_404
from django.contrib.auth import get_user_model

from ..models import PurchasedTemplate
from ..serializers import AdminOverviewSerializer
from ..permissions import IsAdminOrReadOnly, IsSuperUser
from ..utils.admin_ranges import get_admin_date_range, parse_days_param
from accounts.serializers import CustomUserDetailsSerializer

User = get_user_model()

class AdminOverview(APIView):
    permission_classes = [IsAdminOrReadOnly]
    
    def get(self, request):
        """
        Get admin overview statistics with optimized queries.
        No caching to ensure real-time data.
        Accepts optional ?days= query param (default 1, max 365).
        """
        serializer = AdminOverviewSerializer()
        start_datetime, end_datetime, range_label, days = get_admin_date_range(
            days_param=request.GET.get('days'),
            date_str=request.GET.get('date')
        )
        start_date = start_datetime.date()

        # 1. Get documents chart data - optimized with single query
        documents_data = (
            PurchasedTemplate.objects
            .filter(created_at__date__gte=start_date)
            .annotate(date=TruncDate('created_at'))
            .values('date', 'test')
            .annotate(
                total=Count('id'),
                paid=Count('id', filter=Q(test=False))
            )
            .order_by('date')
        )

        documents_chart = [
            {
                'date': item['date'].isoformat(),
                'total': item['total'],
                'paid': item['paid'],
                'test': item['total'] if item['test'] else 0
            }
            for item in documents_data
        ]

        # 2. Get user growth data - optimized (no loop)
        user_growth_data = (
            User.objects
            .filter(date_joined__date__gte=start_date)
            .annotate(date=TruncDate('date_joined'))
            .values('date')
            .annotate(count=Count('id'))
            .order_by('date')
        )

        # Calculate cumulative users
        total_users_before_range = User.objects.filter(date_joined__date__lt=start_date).count()
        current_cumulative = total_users_before_range

        growth_lookup = {item['date']: item['count'] for item in user_growth_data}
        revenue_chart = []

        total_downloads = serializer.get_total_downloads()

        for i in range(days):
            date = start_date + timedelta(days=i)
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
            'regular_users': serializer.get_regular_users(),
            'staff_users': serializer.get_staff_users(),
            'total_purchased_docs': serializer.get_total_purchased_docs(),
            'total_wallet_balance': serializer.get_total_wallet_balance() if request.user.is_superuser else None,
            'external_users': serializer.get_external_users(),
            'active_external_users': serializer.get_active_external_users(start_datetime),
            'documents_chart': documents_chart,
            'revenue_chart': revenue_chart,
        }
        
        response = Response(data, status=status.HTTP_200_OK)
        # Prevent caching of admin stats in browser/CDN
        response["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response


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
            role = request.GET.get('role', 'all').strip().lower()
            source = request.GET.get('source', '').strip()
            
            # Base queryset for users list (pagination)
            users_queryset = User.objects.all()
            if source:
                users_queryset = users_queryset.filter(source=source)
            if search:
                users_queryset = users_queryset.filter(
                    Q(username__icontains=search) | 
                    Q(email__icontains=search)
                )

            if role == 'admin':
                users_queryset = users_queryset.filter(is_superuser=True)
            elif role == 'staff':
                users_queryset = users_queryset.filter(is_staff=True, is_superuser=False)
            elif role == 'user':
                users_queryset = users_queryset.filter(is_staff=False, is_superuser=False)

            days_param = request.GET.get('days')
            date_str = request.GET.get('date')
            start_datetime, end_datetime, range_label, days = get_admin_date_range(
                days_param=days_param,
                date_str=date_str
            )

            # Only restrict the table when a range was explicitly requested.
            # get_admin_date_range defaults to a 1-day window, so applying it
            # unconditionally would hide every user who didn't join today.
            range_selected = bool(days_param or date_str)
            if range_selected:
                users_queryset = users_queryset.filter(
                    date_joined__range=(start_datetime, end_datetime)
                )

            # Statistics (recalculated on every request)
            today = timezone.localdate()
            intervals = {
                'today': today,
                'range_start': start_datetime.date(),
            }
            
            # Optimized stats aggregation
            new_users = User.objects.aggregate(
                today=Count('id', filter=Q(date_joined__date=intervals['today'])),
                period=Count('id', filter=Q(date_joined__range=(start_datetime, end_datetime))),
            )
            
            # Fetch purchase stats - combined query
            purchases_stats = PurchasedTemplate.objects.filter(test=False).aggregate(
                today=Count('buyer_id', filter=Q(created_at__date=intervals['today']), distinct=True),
                period=Count('buyer_id', filter=Q(created_at__range=(start_datetime, end_datetime)), distinct=True),
            )
            
            regular_users_count = User.objects.filter(is_staff=False, is_superuser=False).count()
            staff_users_count = User.objects.filter(is_staff=True).count() + User.objects.filter(is_superuser=True, is_staff=False).count()

            stats_data = {
                'all_users': users_queryset.count() if (search or role != 'all' or range_selected) else User.objects.count(),
                'regular_users': regular_users_count,
                'staff_users': staff_users_count,
                'new_users': new_users,
                'total_purchases_users': purchases_stats,
                'range_label': range_label,
                'range_days': days,
            }

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
            
            response = Response({
                **stats_data,
                'users': users_list_data,
                'search_term': search,
                'role_filter': role,
            }, status=status.HTTP_200_OK)
            
            # Prevent caching of admin stats in browser/CDN
            response["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"
            return response
            
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error in AdminUsers.get: {str(e)}")
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
            purchases = user.purchased_templates.select_related('template').order_by('-created_at')
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
            purchase_counts = user.purchased_templates.aggregate(
                total=Count('id'),
                paid=Count('id', filter=Q(test=False)),
                test_count=Count('id', filter=Q(test=True)),
            )
            stats = {
                'total_purchases': purchase_counts['total'],
                'paid_purchases': purchase_counts['paid'],
                'test_purchases': purchase_counts['test_count'],
                'total_downloads': getattr(user, 'downloads', 0),
                'days_since_joined': (timezone.now() - user.date_joined).days,
            }
            
            response = Response({
                'user': user_serializer.data,
                'wallet': wallet_data,
                'purchase_history': purchase_history,
                'transaction_history': transaction_history,
                'stats': stats,
            }, status=status.HTTP_200_OK)
            # Ensure no caching
            response["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"
            return response
            
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
            doc_type = request.GET.get('type', 'all').strip().lower()
            
            days_param = request.GET.get('days')
            date_str = request.GET.get('date')
            start_datetime, end_datetime, range_label, days = get_admin_date_range(
                days_param=days_param,
                date_str=date_str
            )

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

            if doc_type == 'paid':
                queryset = queryset.filter(test=False)
            elif doc_type == 'test':
                queryset = queryset.filter(test=True)

            # Only restrict the table when a range was explicitly requested;
            # otherwise the default 1-day window silently empties the list
            # and the paginator dead-ends at "Page 1 of 1".
            if days_param or date_str:
                queryset = queryset.filter(created_at__range=(start_datetime, end_datetime))

            stats = cache.get(f'admin_documents_stats_{range_label}')
            if stats is None:
                now = timezone.now()
                seven_days_ago = now - timedelta(days=7)

                total_revenue_data = (
                    PurchasedTemplate.objects
                    .filter(test=False, template__isnull=False, created_at__range=(start_datetime, end_datetime))
                    .select_related('template__tool')
                    .aggregate(total=Sum('template__tool__price'))['total'] or 0
                )

                popular_template_data = (
                    PurchasedTemplate.objects
                    .filter(template__isnull=False)
                    .values('template__name')
                    .annotate(count=Count('id'))
                    .order_by('-count')
                    .first()
                )
                popular_template = popular_template_data['template__name'] if popular_template_data else 'N/A'

                stats = {
                    'total_purchases': PurchasedTemplate.objects.count(),
                    'total_revenue': float(total_revenue_data),
                    'popular_template': popular_template,
                    'recent_count': PurchasedTemplate.objects.filter(created_at__gte=seven_days_ago).count(),
                }
                cache.set('admin_documents_stats_v1', stats, 60)

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

            response = Response({
                'results': results,
                'count': paginator.page.paginator.count,
                'total_pages': paginator.page.paginator.num_pages,
                'current_page': paginator.page.number,
                'next': paginator.get_next_link(),
                'previous': paginator.get_previous_link(),
                'stats': stats,
                'type_filter': doc_type,
            }, status=status.HTTP_200_OK)
            # Prevent caching
            response["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"
            return response

        except Exception as e:
            return Response(
                {'error': 'Internal server error', 'details': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
