from django.urls import path
from . import views

app_name = 'webserving'

urlpatterns = [
    path('search/', views.SearchView.as_view(), name='search'),
    path('register/', views.RegisterView.as_view(), name='register'),
    path('heartbeat/', views.HeartbeatView.as_view(), name='heartbeat'),
    path('stats/', views.StatsView.as_view(), name='stats'),
]
