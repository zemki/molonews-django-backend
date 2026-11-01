from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmailOrUsernameBackend(ModelBackend):
    """Anmeldung mit Benutzername oder E-Mail-Adresse.

    Ein exakter Benutzername hat Vorrang. Sonst wird über die E-Mail gesucht;
    ist sie nicht eindeutig, schlägt die Anmeldung fehl, statt einen beliebigen
    Treffer zu nehmen.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None
        UserModel = get_user_model()
        user = UserModel.objects.filter(username=username).first()
        if user is None:
            matches = list(UserModel.objects.filter(email__iexact=username)[:2])
            user = matches[0] if len(matches) == 1 else None
        if user is None:
            # Laufzeit angleichen, damit sich Konten nicht über die Antwortzeit erkennen lassen
            UserModel().set_password(password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
