from collections import defaultdict
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count, Max, Q
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from api.models import (
    ApiEntitlement,
    ApiKey,
    ApiUsageEvent,
    DocumentRenderJob,
    EmbedSession,
    PurchasedTemplate,
)
from api.permissions import IsSuperUser
from api.utils.admin_ranges import get_admin_date_range, parse_days_param


User = get_user_model()


def _no_cache(response):
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


def _distinct_external_users(user_ids, since=None):
    values = defaultdict(set)
    documents = PurchasedTemplate.objects.filter(
        buyer_id__in=user_ids,
    ).exclude(external_user_id="")
    sessions = EmbedSession.objects.filter(
        user_id__in=user_ids,
    ).exclude(external_user_id="")
    if since:
        documents = documents.filter(created_at__gte=since)
        sessions = sessions.filter(created_at__gte=since)
    for user_id, external_user_id in documents.values_list("buyer_id", "external_user_id"):
        values[user_id].add(external_user_id)
    for user_id, external_user_id in sessions.values_list("user_id", "external_user_id"):
        values[user_id].add(external_user_id)
    return values


def _external_user_count(user_ids, since=None):
    documents = PurchasedTemplate.objects.filter(
        buyer_id__in=user_ids,
    ).exclude(external_user_id="")
    sessions = EmbedSession.objects.filter(
        user_id__in=user_ids,
    ).exclude(external_user_id="")
    if since:
        documents = documents.filter(created_at__gte=since)
        sessions = sessions.filter(created_at__gte=since)
    pairs = documents.order_by().values_list("buyer_id", "external_user_id").union(
        sessions.order_by().values_list("user_id", "external_user_id")
    )
    return pairs.count()


def _counts_by_user(queryset, user_field="user_id", **aggregates):
    return {
        row[user_field]: row
        for row in queryset.values(user_field).annotate(**aggregates)
    }


class AdminApiCustomersView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        days = parse_days_param(request.GET.get("days"))
        now = timezone.now()
        since, _, _, days = get_admin_date_range(days_param=days)
        search = request.GET.get("search", "").strip()
        entitlement_status = request.GET.get("status", "all").strip().lower()
        try:
            page = max(1, int(request.GET.get("page", 1)))
            page_size = max(1, min(50, int(request.GET.get("page_size", 20))))
        except (TypeError, ValueError):
            return Response({"detail": "page and page_size must be integers."}, status=400)

        entitlements = ApiEntitlement.objects.select_related(
            "user", "user__api_customer_settings"
        ).annotate(
            total_keys=Count("user__api_keys", distinct=True),
            active_keys=Count(
                "user__api_keys",
                filter=Q(
                    user__api_keys__revoked_at__isnull=True,
                ) & (Q(user__api_keys__expires_at__isnull=True) | Q(user__api_keys__expires_at__gt=now)),
                distinct=True,
            ),
        )
        if search:
            entitlements = entitlements.filter(
                Q(user__username__icontains=search)
                | Q(user__email__icontains=search)
                | Q(user__api_keys__prefix__icontains=search)
            ).distinct()
        if entitlement_status in ApiEntitlement.Status.values:
            entitlements = entitlements.filter(status=entitlement_status)

        total_matches = entitlements.count()
        total_pages = max(1, (total_matches + page_size - 1) // page_size)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        page_entitlements = list(entitlements.order_by("-activated_at")[offset:offset + page_size])
        user_ids = [item.user_id for item in page_entitlements]

        all_external = _distinct_external_users(user_ids)
        range_external = _distinct_external_users(user_ids, since)
        request_stats = _counts_by_user(
            ApiUsageEvent.objects.filter(user_id__in=user_ids, created_at__gte=since),
            requests=Count("id"),
            errors=Count("id", filter=Q(status_code__gte=400)),
            last_request_at=Max("created_at"),
        )
        session_stats = _counts_by_user(
            EmbedSession.objects.filter(user_id__in=user_ids, created_at__gte=since),
            sessions=Count("id"),
            completed_sessions=Count("id", filter=Q(status=EmbedSession.Status.COMPLETED)),
            paid_sessions=Count("id", filter=Q(mode=EmbedSession.Mode.PAID)),
            last_session_at=Max("created_at"),
        )
        document_stats = _counts_by_user(
            PurchasedTemplate.objects.filter(buyer_id__in=user_ids, created_at__gte=since),
            user_field="buyer_id",
            documents=Count("id"),
            paid_documents=Count("id", filter=Q(test=False)),
            last_document_at=Max("created_at"),
        )
        render_stats = _counts_by_user(
            DocumentRenderJob.objects.filter(user_id__in=user_ids, created_at__gte=since),
            renders=Count("id"),
            failed_renders=Count("id", filter=Q(status=DocumentRenderJob.Status.FAILED)),
            completed_renders=Count("id", filter=Q(status=DocumentRenderJob.Status.COMPLETED)),
            last_render_at=Max("created_at"),
        )

        keys_by_user = defaultdict(list)
        for key in ApiKey.objects.filter(user_id__in=user_ids).order_by("-created_at"):
            keys_by_user[key.user_id].append({
                "id": str(key.id),
                "name": key.name,
                "prefix": key.prefix,
                "scopes": key.scopes,
                "allowed_origins": key.allowed_origins,
                "is_active": key.is_active,
                "last_used_at": key.last_used_at,
                "expires_at": key.expires_at,
                "revoked_at": key.revoked_at,
                "created_at": key.created_at,
            })

        customers = []
        for entitlement in page_entitlements:
            user = entitlement.user
            requests = request_stats.get(user.id, {})
            sessions = session_stats.get(user.id, {})
            documents = document_stats.get(user.id, {})
            renders = render_stats.get(user.id, {})
            request_count = requests.get("requests", 0)
            error_count = requests.get("errors", 0)
            timestamps = [
                requests.get("last_request_at"),
                sessions.get("last_session_at"),
                documents.get("last_document_at"),
                renders.get("last_render_at"),
            ]
            timestamps.extend(key["last_used_at"] for key in keys_by_user[user.id])
            last_activity_at = max((item for item in timestamps if item), default=None)
            settings = getattr(user, "api_customer_settings", None)
            customers.append({
                "user": {
                    "id": user.id,
                    "username": user.username,
                    "email": user.email,
                    "name": user.username,
                },
                "status": entitlement.status,
                "activated_at": entitlement.activated_at,
                "paid_amount": str(entitlement.paid_amount),
                "allowed_origins": settings.allowed_origins if settings else [],
                "total_keys": entitlement.total_keys,
                "active_keys": entitlement.active_keys,
                "external_users": len(all_external[user.id]),
                "active_external_users": len(range_external[user.id]),
                "requests": request_count,
                "errors": error_count,
                "success_rate": round(((request_count - error_count) / request_count) * 100, 1) if request_count else None,
                "sessions": sessions.get("sessions", 0),
                "completed_sessions": sessions.get("completed_sessions", 0),
                "paid_sessions": sessions.get("paid_sessions", 0),
                "documents": documents.get("documents", 0),
                "paid_documents": documents.get("paid_documents", 0),
                "renders": renders.get("renders", 0),
                "completed_renders": renders.get("completed_renders", 0),
                "failed_renders": renders.get("failed_renders", 0),
                "last_activity_at": last_activity_at,
                "keys": keys_by_user[user.id],
            })

        all_customer_ids = list(ApiEntitlement.objects.values_list("user_id", flat=True))
        request_summary = ApiUsageEvent.objects.filter(created_at__gte=since).aggregate(
            requests=Count("id"),
            errors=Count("id", filter=Q(status_code__gte=400)),
        )
        summary = {
            "customers": ApiEntitlement.objects.count(),
            "active_customers": ApiEntitlement.objects.filter(status=ApiEntitlement.Status.ACTIVE).count(),
            "active_keys": ApiKey.objects.filter(revoked_at__isnull=True).filter(
                user__api_entitlement__status=ApiEntitlement.Status.ACTIVE,
            ).filter(
                Q(expires_at__isnull=True) | Q(expires_at__gt=now)
            ).count(),
            "external_users": _external_user_count(all_customer_ids),
            "active_external_users": _external_user_count(all_customer_ids, since),
            "requests": request_summary["requests"],
            "errors": request_summary["errors"],
            "success_rate": round(
                ((request_summary["requests"] - request_summary["errors"]) / request_summary["requests"]) * 100,
                1,
            ) if request_summary["requests"] else None,
        }

        request_trend = {
            row["date"]: row
            for row in ApiUsageEvent.objects.filter(created_at__gte=since)
            .annotate(date=TruncDate("created_at"))
            .values("date")
            .annotate(
                requests=Count("id"),
                errors=Count("id", filter=Q(status_code__gte=400)),
            )
        }
        trend = []
        start_date = timezone.localdate(since)
        for index in range(days):
            date = start_date + timedelta(days=index)
            row = request_trend.get(date, {})
            trend.append({
                "date": date.isoformat(),
                "requests": row.get("requests", 0),
                "errors": row.get("errors", 0),
            })

        operations = list(
            ApiUsageEvent.objects.filter(created_at__gte=since)
            .values("operation", "method")
            .annotate(
                requests=Count("id"),
                errors=Count("id", filter=Q(status_code__gte=400)),
            )
            .order_by("-requests")[:8]
        )

        return _no_cache(Response({
            "range_days": days,
            "summary": summary,
            "trend": trend,
            "operations": operations,
            "customers": {
                "results": customers,
                "count": total_matches,
                "current_page": page,
                "total_pages": total_pages,
            },
        }))


class AdminApiCustomerStatusView(APIView):
    permission_classes = [IsSuperUser]

    def patch(self, request, user_id):
        entitlement = ApiEntitlement.objects.filter(user_id=user_id).first()
        if not entitlement:
            return Response({"detail": "API customer not found."}, status=404)
        next_status = request.data.get("status")
        if next_status not in ApiEntitlement.Status.values:
            return Response({"detail": "Choose active, suspended, or revoked."}, status=400)
        entitlement.status = next_status
        entitlement.save(update_fields=["status", "updated_at"])
        if next_status != ApiEntitlement.Status.ACTIVE:
            EmbedSession.objects.filter(
                user_id=user_id,
                status=EmbedSession.Status.PENDING,
            ).update(status=EmbedSession.Status.REVOKED, revoked_at=timezone.now())
        return Response({"user_id": user_id, "status": entitlement.status})


class AdminApiKeyRevokeView(APIView):
    permission_classes = [IsSuperUser]

    def delete(self, request, user_id, key_id):
        key = ApiKey.objects.filter(id=key_id, user_id=user_id).first()
        if not key:
            return Response({"detail": "API key not found."}, status=404)
        if not key.revoked_at:
            key.revoked_at = timezone.now()
            key.save(update_fields=["revoked_at"])
            EmbedSession.objects.filter(
                api_key=key,
                status=EmbedSession.Status.PENDING,
            ).update(status=EmbedSession.Status.REVOKED, revoked_at=key.revoked_at)
        return Response(status=status.HTTP_204_NO_CONTENT)
