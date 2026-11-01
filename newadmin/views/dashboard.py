from django.contrib.auth import authenticate, login, logout
from django.core.cache import cache
from django.db.models import Q
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import RedirectView

from api.views.admin_user import send_activation_email
from content.models import User
from .mixins import NewAdminMixin


class IndexView(NewAdminMixin, RedirectView):
    pattern_name = 'newadmin:stats'


def _find_inactive_user(identifier, password):
    """Return an inactive user matching identifier+password, else None.

    The password check prevents leaking which identifiers exist — only
    someone who already has the credentials learns the account is pending
    activation.
    """
    if not identifier or not password:
        return None
    user = User.objects.filter(
        Q(username=identifier) | Q(email=identifier),
        is_active=False,
    ).first()
    if user and user.check_password(password):
        return user
    return None


@method_decorator(ensure_csrf_cookie, name='get')
class LoginView(View):
    template_name = 'newadmin/login.html'

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect('newadmin:index')
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        return render(request, self.template_name)

    def post(self, request):
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            next_url = request.GET.get('next', '') or request.POST.get('next', '')
            if not url_has_allowed_host_and_scheme(
                next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
            ):
                next_url = ''
            return redirect(next_url or 'newadmin:index')

        inactive_user = _find_inactive_user(username, password)
        if inactive_user is not None:
            return render(request, self.template_name, {
                'error': 'Ihr Konto ist noch nicht aktiviert. Bitte bestätigen Sie Ihre E-Mail-Adresse.',
                'username': username,
                'show_resend': True,
            })

        return render(request, self.template_name, {
            'error': 'Benutzername/E-Mail oder Passwort ist falsch.',
            'username': username,
        })


class ResendActivationView(View):
    """Re-send the activation mail to an inactive user.

    Always renders the login page with a generic info message so the form
    cannot be used to enumerate accounts. A per-user cooldown prevents mail
    spam when the button is repeatedly clicked.
    """

    template_name = 'newadmin/login.html'

    def post(self, request):
        identifier = request.POST.get('username', '').strip()
        info = 'Falls für diese Angabe ein noch nicht aktiviertes Konto existiert, wurde eine neue Aktivierungsmail versendet.'

        if identifier:
            user = User.objects.filter(
                Q(username=identifier) | Q(email=identifier),
                is_active=False,
            ).first()
            if user:
                cache_key = f'resend-activation:{user.pk}'
                if not cache.get(cache_key):
                    if send_activation_email(user):
                        cache.set(cache_key, True, 60)

        return render(request, self.template_name, {
            'info': info,
            'username': identifier,
        })


class LogoutView(View):
    def post(self, request):
        logout(request)
        return redirect('/')
