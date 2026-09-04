"""
WebServing models — local commerce directory and inventory router.

WebServing is a business directory (like Google Maps / Yelp) combined
with a live inventory router. The directory data lives here; the
inventory stays at each store's own WebClerk instance and is queried
in real time.

All models use plain Django — no WC3 BaseModel, no WC3 dependencies.
WebServing has its own database (commerce_webserving) and can be
deployed independently.

Identity pattern (same as WebClerk):
  id   — BigInt PK, internal to this database, never crosses a boundary
  ida  — human-readable identifier, unique within this instance
  uuid — globally unique, the cross-database identity for federation

Multiple WebServing instances per city/region. They collaborate at
geographic boundaries using uuid for identity. Same sovereignty model
as WebClerk profit bubbles.
"""
import uuid as uuid_lib
from django.db import models


class WebServingBase(models.Model):
    """Lightweight base for all WebServing models.

    Same field shape as WebClerk's CoreModel/BaseModel — id/ida/uuid
    identity triple, JSON envelope (config, metadata, refs, prefs,
    actions, comments), timestamps. Agents and people use the same
    PJPV patterns across both systems.

    No WC3 machinery — no pending, no denormalize, no ida autogeneration.
    The value is the consistent data shape, not the save() hooks.
    """
    # Identity triple
    uuid = models.UUIDField(default=uuid_lib.uuid4, unique=True, editable=False)
    ida = models.CharField(
        max_length=40, unique=True, db_index=True,
        help_text='Human-readable identifier, unique within this instance.',
    )

    # JSON envelope — same as WebClerk CoreModel
    config = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    refs = models.JSONField(default=dict, blank=True)
    prefs = models.JSONField(default=dict, blank=True)
    actions = models.JSONField(default=dict, blank=True)
    comments = models.JSONField(default=dict, blank=True)

    # Status and lifecycle
    status = models.CharField(max_length=50, default='active', blank=True)
    is_active = models.BooleanField(default=True)

    # Timestamps
    dt_created = models.DateTimeField(auto_now_add=True)
    dt_modified = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def __str__(self):
        return self.ida


class Category(WebServingBase):
    """Business category — hierarchical.

    Examples: Sporting Goods, Hearth Products, Hardware, Grocery.
    Stores can belong to multiple categories.
    """
    name = models.CharField(max_length=100)
    parent = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='children',
    )
    description = models.TextField(blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = 'webserving_category'
        ordering = ['sort_order', 'name']
        verbose_name_plural = 'categories'

    def __str__(self):
        return self.name


class Company(WebServingBase):
    """A business listed in the WebServing directory.

    This is the public-facing store profile — what a searcher sees.
    The inventory is queried live from the store's own WebClerk instance
    via api_url. WebServing never stores inventory.

    Fields modeled after Google Maps / Yelp business listings:
    identity, location, contact, hours, categories, ratings.
    """
    # Identity (uuid, ida from base)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    logo_url = models.URLField(max_length=500, blank=True)

    # Location
    address = models.CharField(max_length=300, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=50, blank=True)
    zip_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=50, default='US')
    latitude = models.FloatField()
    longitude = models.FloatField()

    # Contact
    domain = models.CharField(max_length=200, blank=True, help_text='Store website.')
    phones = models.JSONField(
        default=list, blank=True,
        help_text='[{"dept": "sales", "number": "918-555-0100", "hours": "9-5"}, ...]',
    )
    email = models.EmailField(blank=True)

    # Categories
    categories = models.ManyToManyField(
        Category, blank=True, related_name='companies',
    )

    # Hours of operation
    hours = models.JSONField(
        default=dict, blank=True,
        help_text='{"mon": ["9:00","17:00"], "tue": ["9:00","17:00"], ...}',
    )

    # Ratings (aggregated from reviews)
    rating_avg = models.FloatField(default=0.0)
    rating_count = models.IntegerField(default=0)

    # WebClerk connection
    instance_uuid = models.UUIDField(
        null=True, blank=True, unique=True,
        help_text='UUID from the WebClerk instance wc:company_profile Setting.',
    )
    api_url = models.URLField(
        max_length=500, blank=True,
        help_text='WebClerk wcapi base URL for live inventory queries.',
    )
    athena_token = models.CharField(
        max_length=200, blank=True,
        help_text='Auth token for querying this instance.',
    )

    # Network membership
    TIER_CHOICES = [
        ('free', 'Free — included in network'),
        ('standard', 'Standard — priority placement'),
        ('professional', 'Professional — priority + analytics'),
    ]
    tier = models.CharField(max_length=20, choices=TIER_CHOICES, default='free')

    # Health
    is_online = models.BooleanField(default=True)
    dt_last_heartbeat = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.IntegerField(default=0)

    # Staff contact — the person who keeps this listing current
    contact_name = models.CharField(max_length=200, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=30, blank=True)

    # Auth — for self-service registration and listing management
    contact_password = models.CharField(max_length=128, blank=True, help_text='Hashed password for login.')
    email_verified = models.BooleanField(default=False)
    email_verify_token = models.CharField(max_length=64, blank=True)

    class Meta:
        db_table = 'webserving_company'
        indexes = [
            models.Index(fields=['latitude', 'longitude'], name='ws_co_lat_lng_idx'),
            models.Index(fields=['is_online', 'is_active', 'tier'], name='ws_co_online_idx'),
            models.Index(fields=['city', 'state'], name='ws_co_city_state_idx'),
        ]
        ordering = ['name']
        verbose_name_plural = 'companies'

    def __str__(self):
        return f'{self.name} ({self.city}, {self.state})'


class Review(WebServingBase):
    """Customer review of a store."""
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='reviews')
    rating = models.IntegerField(help_text='1-5 stars.')
    text = models.TextField(blank=True)
    author_name = models.CharField(max_length=100, blank=True)
    source = models.CharField(
        max_length=50, default='webserving',
        help_text='Origin: webserving, google, yelp, etc.',
    )

    class Meta:
        db_table = 'webserving_review'
        ordering = ['-dt_created']

    def __str__(self):
        return f'{self.company.name} — {self.rating}★ by {self.author_name}'


class SearchLog(WebServingBase):
    """Log of searches — Alice's demand signal.

    What people search for, where, and whether they find it locally.
    Zero-result searches are demand gaps.
    """
    query = models.CharField(max_length=500)
    latitude = models.FloatField()
    longitude = models.FloatField()
    radius_miles = models.FloatField(default=5.0)
    results_count = models.IntegerField(default=0)
    stores_queried = models.IntegerField(default=0)
    stores_responded = models.IntegerField(default=0)
    elapsed_ms = models.IntegerField(default=0)

    class Meta:
        db_table = 'webserving_search_log'
        indexes = [
            models.Index(fields=['-dt_created'], name='ws_search_dt_idx'),
        ]
        ordering = ['-dt_created']

    def __str__(self):
        return f'"{self.query}" — {self.results_count} results'


class HealthCheck(WebServingBase):
    """API health check record for a company.

    Tracks response time, status, and failures over time.
    """
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='health_checks')
    is_reachable = models.BooleanField()
    response_ms = models.IntegerField(null=True, blank=True)
    status_code = models.IntegerField(null=True, blank=True)
    error = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = 'webserving_health'
        indexes = [
            models.Index(fields=['company', '-dt_created'], name='ws_health_co_dt_idx'),
        ]
        ordering = ['-dt_created']

    def __str__(self):
        status = 'UP' if self.is_reachable else 'DOWN'
        return f'{self.company.name} — {status} ({self.response_ms}ms)'
