"""
WebServing routing service — fan-out search to local WebClerk instances.

The core algorithm:
  1. Find all registered instances within radius of the search location
  2. Query each instance's inventory endpoint concurrently
  3. Merge results, annotate with distance, rank by distance + availability
  4. Return unified result set

Distance calculation uses the Haversine formula (accurate enough for
1-10 mile radius — no need for PostGIS).
"""
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

logger = logging.getLogger(__name__)

# Max concurrent instance queries
MAX_CONCURRENT = 20
# Timeout per instance query (seconds)
INSTANCE_TIMEOUT = 5
# Miles to degrees (approximate at mid-latitudes)
MILES_TO_DEG_LAT = 1 / 69.0
MILES_TO_DEG_LNG_AT_40 = 1 / 54.6  # varies with latitude


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distance between two points in miles (Haversine formula)."""
    R = 3958.8  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def find_companies_in_radius(lat: float, lng: float, radius_miles: float):
    """Find companies within radius using bounding box + Haversine.

    First pass: SQL bounding box (fast, uses index).
    Second pass: Haversine filter (exact, in Python).
    """
    from apps.webserving.models import Company

    # Bounding box — generous to catch edge cases
    dlat = radius_miles * MILES_TO_DEG_LAT * 1.2
    dlng = radius_miles * MILES_TO_DEG_LNG_AT_40 * 1.2

    candidates = Company.objects.filter(
        is_active=True,
        is_online=True,
        latitude__gte=lat - dlat,
        latitude__lte=lat + dlat,
        longitude__gte=lng - dlng,
        longitude__lte=lng + dlng,
    ).values(
        'id', 'instance_uuid', 'name', 'api_url',
        'latitude', 'longitude', 'city', 'state', 'tier',
        'athena_token', 'domain', 'phones',
    )

    # Haversine filter
    results = []
    for inst in candidates:
        dist = haversine_miles(lat, lng, inst['latitude'], inst['longitude'])
        if dist <= radius_miles:
            inst['distance_miles'] = round(dist, 1)
            results.append(inst)

    # Sort: subscribed first, then by distance
    tier_rank = {'professional': 0, 'standard': 1, 'free': 2}
    results.sort(key=lambda x: (tier_rank.get(x['tier'], 9), x['distance_miles']))
    return results


def _is_local_instance(instance: dict) -> bool:
    """Check if this instance is the local server (self-query)."""
    url = instance.get('api_url', '')
    return 'localhost' in url or '127.0.0.1' in url


def _get_retail_layout() -> list:
    """Load the item list.retail layout columns from the wc:model Setting.

    Returns a list of column defs: [{'field': 'ida', 'label': 'ida', ...}, ...]
    Falls back to a hardcoded default if the Setting doesn't exist.
    """
    try:
        from apps.core.models import Setting
        s = Setting.objects.filter(
            parent_model='item', purpose='wc:model',
        ).first()
        if s:
            cols = (s.config or {}).get('layout', {}).get('list', {}).get('retail', {}).get('columns', [])
            if cols:
                return cols
    except Exception:
        pass
    # Fallback — reasonable defaults
    return [
        {'field': 'ida', 'label': 'ida'},
        {'field': 'description', 'label': 'description'},
        {'field': 'price.retail', 'label': 'price', 'format': 'currency'},
        {'field': 'quantity.on_hand', 'label': 'in stock'},
    ]


def _pjpv_get(data: dict, path: str):
    """Walk a PJPV dotted path into a dict. Returns None if any step missing."""
    val = data
    for key in path.split('.'):
        if isinstance(val, dict):
            val = val.get(key)
        else:
            return None
    return val


def _query_local_inventory(instance: dict, query: str) -> dict:
    """Query this instance's own database directly — no HTTP needed.

    Uses the item Setting's list.retail layout to determine which
    PJPV paths to return. Each item in the response carries the
    layout-defined paths as keys — no extraction or flattening.
    """
    try:
        from django.apps import apps
        from django.db.models import Q

        Item = apps.get_model('products', 'Item')
        columns = _get_retail_layout()

        # Collect the root DB columns needed from the PJPV paths
        db_fields = {'id'}
        for col in columns:
            root = col.get('field', '').split('.')[0]
            if root:
                db_fields.add(root)

        qs = Item.objects.filter(
            Q(ida__icontains=query) |
            Q(name__icontains=query) |
            Q(description__icontains=query) |
            Q(sku__icontains=query),
            is_active=True,
        ).values(*db_fields).order_by('-dt_modified')[:10]

        items = []
        for row in qs:
            item = {'item_id': row.get('id')}
            for col in columns:
                path = col['field']
                item[path] = _pjpv_get(row, path)
            items.append(item)

        return {**instance, 'items': items, 'error': None}

    except Exception as e:
        logger.warning("Local inventory query failed: %s", e)
        return {**instance, 'items': [], 'error': str(e)[:100]}


def query_instance_inventory(instance: dict, query: str) -> dict:
    """Query a single WebClerk instance for matching inventory.

    Local instances query the database directly (no HTTP overhead).
    Remote instances are queried via their public API endpoint.
    Both return items keyed by PJPV paths from the list.retail layout.
    """
    # Local instance — direct database query, no auth needed
    if _is_local_instance(instance):
        return _query_local_inventory(instance, query)

    # Remote instance — HTTP query with layout-based extraction
    columns = _get_retail_layout()
    api_url = instance['api_url'].rstrip('/')
    url = f"{api_url}/get/item_variant/"

    headers = {'X-Forwarded-Proto': 'https'}
    token = instance.get('athena_token', '')
    if token:
        headers['Authorization'] = f'Athena {token}'

    try:
        with httpx.Client(timeout=INSTANCE_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url, params={'search': query, 'limit': 20}, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            results = data.get('results', data.get('data', []))
            items = []
            for record in (results[:10] if isinstance(results, list) else []):
                item = {'item_id': record.get('id')}
                for col in columns:
                    path = col['field']
                    item[path] = _pjpv_get(record, path)
                items.append(item)

            return {**instance, 'items': items, 'error': None}

    except httpx.ConnectError:
        return {**instance, 'items': [], 'error': 'unreachable'}
    except httpx.TimeoutException:
        return {**instance, 'items': [], 'error': 'timeout'}
    except Exception as e:
        return {**instance, 'items': [], 'error': str(e)[:100]}


def search_local_inventory(
    query: str,
    lat: float,
    lng: float,
    radius_miles: float = 5.0,
) -> dict:
    """Search for inventory across all local WebClerk instances.

    The main routing function:
      1. Find instances within radius
      2. Fan out query to each concurrently
      3. Merge and rank results
      4. Log the search for Alice pattern analysis

    Returns {
        'query': str,
        'location': {'lat': float, 'lng': float},
        'radius_miles': float,
        'stores': [{'business_name', 'distance_miles', 'city', 'items': [...]}],
        'total_items': int,
        'stores_queried': int,
        'stores_responded': int,
        'elapsed_ms': int,
    }
    """
    start = time.time()
    columns = _get_retail_layout()

    # Find nearby instances
    instances = find_companies_in_radius(lat, lng, radius_miles)
    if not instances:
        elapsed = int((time.time() - start) * 1000)
        _log_search(query, lat, lng, radius_miles, 0, 0, 0, elapsed)
        return {
            'query': query,
            'location': {'lat': lat, 'lng': lng},
            'radius_miles': radius_miles,
            'columns': columns,
            'stores': [],
            'total_items': 0,
            'stores_queried': 0,
            'stores_responded': 0,
            'elapsed_ms': elapsed,
        }

    # Fan out queries concurrently
    results = []
    with ThreadPoolExecutor(max_workers=min(len(instances), MAX_CONCURRENT)) as pool:
        futures = {
            pool.submit(query_instance_inventory, inst, query): inst
            for inst in instances
        }
        for future in as_completed(futures):
            results.append(future.result())

    # Build response — only stores with matching items
    stores = []
    total_items = 0
    stores_responded = 0

    for r in results:
        if r.get('error'):
            continue
        stores_responded += 1
        items = r.get('items', [])
        if not items:
            continue
        total_items += len(items)
        stores.append({
            'business_name': r['name'],
            'distance_miles': r['distance_miles'],
            'latitude': r.get('latitude'),
            'longitude': r.get('longitude'),
            'city': r.get('city', ''),
            'state': r.get('state', ''),
            'tier': r['tier'],
            'domain': r.get('domain', ''),
            'phones': r.get('phones', []),
            'items': items,
        })

    # Sort stores by distance
    stores.sort(key=lambda s: s['distance_miles'])

    elapsed = int((time.time() - start) * 1000)
    _log_search(query, lat, lng, radius_miles, total_items,
                len(instances), stores_responded, elapsed)

    return {
        'query': query,
        'location': {'lat': lat, 'lng': lng},
        'radius_miles': radius_miles,
        'columns': columns,
        'stores': stores,
        'total_items': total_items,
        'stores_queried': len(instances),
        'stores_responded': stores_responded,
        'elapsed_ms': elapsed,
    }


def _log_search(query, lat, lng, radius, results_count,
                instances_queried, instances_responded, elapsed_ms):
    """Log the search for Alice demand pattern analysis."""
    try:
        from apps.webserving.models import SearchLog
        SearchLog.objects.create(
            query=query[:500],
            latitude=lat,
            longitude=lng,
            radius_miles=radius,
            results_count=results_count,
            stores_queried=instances_queried,
            stores_responded=instances_responded,
            elapsed_ms=elapsed_ms,
        )
    except Exception:
        logger.warning("Failed to log search", exc_info=True)
