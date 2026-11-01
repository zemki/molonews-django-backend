from django import template
from django.urls import reverse, NoReverseMatch
from django.utils.html import strip_tags
from django.utils.text import Truncator

register = template.Library()


@register.simple_tag(takes_context=True)
def active_nav(context, url_name):
    """Returns 'active' if the current URL starts with the resolved URL."""
    request = context.get('request')
    if not request:
        return ''
    try:
        url = reverse(url_name)
        if request.path.startswith(url):
            return 'active'
    except NoReverseMatch:
        pass
    return ''


@register.filter
def strip_and_truncate(value, length=120):
    """Strip HTML tags and truncate to given length."""
    text = strip_tags(str(value or ''))
    return Truncator(text).chars(length, truncate='...')


@register.filter
def user_initial(user):
    """Get user's first initial for avatar placeholder."""
    if user.first_name:
        return user.first_name[0].upper()
    if user.username:
        return user.username[0].upper()
    return '?'
