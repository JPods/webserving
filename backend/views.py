"""
WebServing API views — local inventory routing.

Public endpoints (no auth required — this is a search engine):
    GET  /wcapi/webserving/search/     — search local inventory
    POST /wcapi/webserving/register/   — register a WebClerk instance
    POST /wcapi/webserving/heartbeat/  — instance heartbeat
    GET  /wcapi/webserving/stats/      — network statistics

The search endpoint is public. Registration requires an Athena token
from the registering instance.
"""
import logging
import time

from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView

from common.api_responses import api_response
from .services import search_local_inventory, haversine_miles

logger = logging.getLogger(__name__)


class SearchView(APIView):
    """Search local inventory across registered WebClerk instances.

    GET /wcapi/webserving/search/?q=<query>&lat=<lat>&lng=<lng>&radius=<miles>

    Public — no authentication required. This is a search engine.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        query = request.GET.get('q', '').strip()
        if not query:
            return api_response(error='Missing search query (q=)', status=400)

        try:
            lat = float(request.GET.get('lat', ''))
            lng = float(request.GET.get('lng', ''))
        except (ValueError, TypeError):
            return api_response(
                error='Missing or invalid location (lat=, lng=)',
                status=400,
            )

        try:
            radius = float(request.GET.get('radius', '5'))
        except (ValueError, TypeError):
            radius = 5.0

        # Clamp radius to 1-10 miles
        radius = max(1.0, min(radius, 10.0))

        result = search_local_inventory(query, lat, lng, radius)
        return api_response(data=result)


class RegisterView(APIView):
    """Register a WebClerk instance in the routing network.

    POST /wcapi/webserving/register/
    {
        "instance_uuid": "...",
        "business_name": "Bob's Hardware",
        "api_url": "https://bobs-hardware.com/wcapi/",
        "latitude": 41.8240,
        "longitude": -71.4128,
        "city": "Providence",
        "state": "RI",
        "zip_code": "02903",
        "athena_token": "..."
    }

    Upserts by instance_uuid. The registering instance sends its own
    Athena token so WebServing can query its inventory later.
    """
    permission_classes = [AllowAny]  # Instance self-registers

    def post(self, request):
        data = request.data or {}
        instance_uuid = data.get('instance_uuid')
        if not instance_uuid:
            return api_response(error='instance_uuid required', status=400)

        business_name = data.get('business_name', '').strip()
        api_url = data.get('api_url', '').strip()
        if not business_name or not api_url:
            return api_response(
                error='business_name and api_url required', status=400,
            )

        try:
            lat = float(data.get('latitude', ''))
            lng = float(data.get('longitude', ''))
        except (ValueError, TypeError):
            return api_response(error='Valid latitude and longitude required', status=400)

        from .models import RegisteredInstance

        now_ms = int(time.time() * 1000)
        inst, created = RegisteredInstance.objects.update_or_create(
            instance_uuid=instance_uuid,
            defaults={
                'business_name': business_name,
                'api_url': api_url,
                'latitude': lat,
                'longitude': lng,
                'city': data.get('city', ''),
                'state': data.get('state', ''),
                'zip_code': data.get('zip_code', ''),
                'athena_token': data.get('athena_token', ''),
                'tier': data.get('tier', 'free'),
                'dt_last_heartbeat': now_ms,
                'is_online': True,
                'consecutive_failures': 0,
            },
        )

        action = 'registered' if created else 'updated'
        logger.info("WebServing: %s instance %s (%s)", action, business_name, instance_uuid)

        return api_response(data={
            'status': action,
            'instance_uuid': str(instance_uuid),
            'business_name': business_name,
        })


class HeartbeatView(APIView):
    """Instance heartbeat — confirms the instance is still online.

    POST /wcapi/webserving/heartbeat/
    {"instance_uuid": "..."}
    """
    permission_classes = [AllowAny]

    def post(self, request):
        instance_uuid = (request.data or {}).get('instance_uuid')
        if not instance_uuid:
            return api_response(error='instance_uuid required', status=400)

        from .models import RegisteredInstance

        try:
            inst = RegisteredInstance.objects.get(
                instance_uuid=instance_uuid, is_active=True,
            )
        except RegisteredInstance.DoesNotExist:
            return api_response(error='Instance not registered', status=404)

        inst.dt_last_heartbeat = int(time.time() * 1000)
        inst.is_online = True
        inst.consecutive_failures = 0
        inst.save(update_fields=[
            'dt_last_heartbeat', 'is_online', 'consecutive_failures',
        ])

        return api_response(data={'status': 'ok', 'instance_uuid': str(instance_uuid)})


class StatsView(APIView):
    """Network statistics — how many instances, searches, coverage.

    GET /wcapi/webserving/stats/
    """
    permission_classes = [AllowAny]

    def get(self, request):
        from .models import RegisteredInstance, SearchLog
        from django.db.models import Count, Avg

        total = RegisteredInstance.objects.filter(is_active=True).count()
        online = RegisteredInstance.objects.filter(is_active=True, is_online=True).count()

        by_tier = dict(
            RegisteredInstance.objects.filter(is_active=True)
            .values_list('tier')
            .annotate(n=Count('id'))
        )

        # Search stats (last 7 days)
        seven_days_ms = int(time.time() * 1000) - (7 * 86400 * 1000)
        recent_searches = SearchLog.objects.filter(
            dt_created__gte=seven_days_ms,
        )
        search_count = recent_searches.count()
        avg_results = recent_searches.aggregate(
            avg=Avg('results_count'),
        )['avg'] or 0

        # Coverage — unique cities/states
        coverage = RegisteredInstance.objects.filter(
            is_active=True, is_online=True,
        ).values('state').annotate(
            cities=Count('city', distinct=True),
            stores=Count('id'),
        ).order_by('-stores')[:10]

        return api_response(data={
            'instances': {'total': total, 'online': online},
            'by_tier': by_tier,
            'searches_7d': search_count,
            'avg_results_per_search': round(avg_results, 1),
            'coverage': list(coverage),
        })
