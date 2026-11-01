"""HTML-Mails über Djangos Mail-Backend (``EMAIL_BACKEND``).

So greifen die ``EMAIL_*``-Einstellungen aus der ``.env`` für alle Mails, und in
der Entwicklung landen sie mit dem Konsolen-Backend in der Ausgabe von
``runserver`` statt an einem SMTP-Server zu scheitern.
"""

from django.conf import settings
from django.core.mail import EmailMessage


def send_html_mail(subject, html, recipient, from_email=None):
    """Sendet eine HTML-Mail an ``recipient``; Fehler werfen eine Exception."""
    message = EmailMessage(
        subject=subject,
        body=html,
        from_email=from_email or settings.DEFAULT_FROM_EMAIL,
        to=[recipient],
    )
    message.content_subtype = "html"
    message.send(fail_silently=False)
