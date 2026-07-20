"""
URL configuration for barathrum_backend project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path

from accounts import views as accounts_views
from machines.webhooks import paymongo_webhook_view

urlpatterns = [
    path('admin/', admin.site.urls),
    path('signup/', accounts_views.signup_view, name='signup'),
    path('verify/', accounts_views.verify_view, name='verify'),
    path('login/', accounts_views.login_view, name='login'),
    path('webhooks/paymongo/', paymongo_webhook_view, name='paymongo_webhook'),
    path('', include('dashboard.urls')),
]
