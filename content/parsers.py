from bs4 import BeautifulSoup
from datetime import datetime
from locale import LC_TIME, setlocale
from django.utils.timezone import get_current_timezone, make_aware, is_aware
from dateutil.parser import parse as parse_datetime
import re


# Function to extract URLs from <a> tags and replace them in content
def clean_content(html_content):
    # Regex to extract URL from <a href="...">
    match = re.findall(r'<a\s+href=["\'](.*?)["\'].*?>.*?</a>', html_content)
    if match:
        for url in match:
            html_content = html_content.replace(f'<a href="{url}">', url).replace('</a>', '')
    # Remove any remaining HTML tags
    html_content = re.sub(r'<.*?>', '', html_content)
    return html_content.strip()


def _cook(entry_string, parser='lxml'):
    """Create a bs object to parse a feed entry string.

    Args:
        entry_string (str): entry as string

    Returns:
        BeautfulSoup object
    """
    return BeautifulSoup(entry_string, features=parser)


def sanitize_title(title):
    """ Shorten title if necessary

    Args:
        title (str): title string

    Returns:
        sanitized title
    """
    if len(title) > 300:
        return title[0:297] + '...'
    return title


def sanitize_link(link):
    """Remove extra 'https://*' from a link

    Args:
        link (str): link to sanitize

    Returns:
        sanitized link
    """
    if link.startswith("https"):
        link = 'https:{}'.format(link.split("https")[-1].lstrip(":"))
    return link


def sanitize_attribute(attribute_name, sanitize_function, target_object):
    """Sanitize an attribute of an object with a given function.

    Args:
        attribute_name (str): name of targe attribute
        sanitize_function (def): target function
        target_object (object): target entry object

    Returns:
        object with sanitized attribute
    """
    if hasattr(target_object, attribute_name) and getattr(target_object, attribute_name):
        setattr(target_object, attribute_name, sanitize_function(getattr(target_object, attribute_name)))
    return target_object


def sanitize_attributes(entry, attributes):
    """Sanitize all given attributes on an entry object.

      Args:
          entry (object): article entry from feed
          attributes (dict): dict of attributes to be sanitized including sanitize method

      Returns:
        sanitized entry object
      """
    for attribute, sanitize_method in attributes.items():
        entry = sanitize_attribute(attribute, sanitize_method, entry)
    return entry


def parse_date(date_string, scheme="%a %b %d %H:%M:%S %Z %Y"):
    """Parse a time string.

    Args:
        date_string (str): timestring

    Returns:
        timezone aware dateime or None
    """
    if not date_string:
        return None

    _locale = setlocale(LC_TIME)
    try:
        setlocale(LC_TIME, "C")
        _date = datetime.strptime(date_string, scheme)
    finally:
        setlocale(LC_TIME, _locale)
    if is_aware(_date):
        return _date
    return make_aware(_date, get_current_timezone())


def _generic(entry):
    soup = _cook(entry.summary, parser='html.parser')
    entry.summary = soup.text
    attributes = {
        "link": sanitize_link, "image_url": sanitize_link, "title": sanitize_title
    }
    entry = sanitize_attributes(entry, attributes)
    return entry


def _rss_events(entry):
    # TODO refine
    soup = _cook(entry.summary, parser='html.parser')
    entry.summary = soup.text
    attributes = {
        "link": sanitize_link, "image_url": sanitize_link,
    }
    entry = sanitize_attributes(entry, attributes)
    entry.event = True
    return entry


def _parse_feed_date(date_string):
    """Parse a feed timestamp - ISO 8601 (Atom) oder RFC 822 (RSS 2.0).

    Args:
        date_string (str): timestring, z.B. '2026-08-10T14:47:30+02:00'
            oder 'Tue, 05 May 2026 06:34:08 +0000'

    Returns:
        timezone aware datetime or None
    """
    if not date_string:
        return None
    try:
        _date = parse_datetime(date_string)
    except (ValueError, OverflowError):
        return None
    if is_aware(_date):
        return _date
    return make_aware(_date, get_current_timezone())


# WordPress haengt an den Auszug haeufig einen Werbe-Baustein an ("Der Beitrag X
# erschien zuerst auf Y."). Der gehoert nicht in den Abstract.
_WP_NACHSPANN = re.compile(
    r'\s*(?:Der (?:Beitrag|Artikel)\s.*?\serschien zuerst auf\s'
    r'|The post\s.*?\sappeared first on\s).*$',
    re.DOTALL,
)


def _wordpress(entry):
    """WordPress Feedparser.

    WordPress liefert RSS 2.0 mit <pubDate>, aber kein moddate. Ohne das ruft der
    Importer update_article() nie auf - Korrekturen der Quelle an Titel, Text oder
    Datum erreichen bestehende Artikel dann nie. Zusaetzlich wird der Nachspann
    entfernt, den WordPress an den Auszug haengt.

    Args:
        entry (object): entry from a WordPress feed

    Returns:
        parsed entry object
    """
    entry = _generic(entry)
    entry.summary = _WP_NACHSPANN.sub('', entry.summary).strip()
    # Manche Redaktionen formatieren Titel im CMS mit Leerzeichen statt Umbruch -
    # aus dem Feed kommen dann Titel mit langen Leerzeichenketten.
    if getattr(entry, 'title', None):
        entry.title = re.sub(r'\s+', ' ', entry.title).strip()
    entry.statedate = _parse_feed_date(getattr(entry, 'published', None))
    entry.moddate = _parse_feed_date(getattr(entry, 'updated', None)) or entry.statedate
    return entry


parsers = {
    'generic': {
        'description': 'Generic html Parser',
        'func': _generic,
    },
    'events': {
        'description': 'Parse to events',
        'func': _rss_events,
    },
    'wordpress': {
        'description': 'WordPress Parser',
        'func': _wordpress,
    },
}


def get_parser_function(parser_name):
    """Get a bs parser function by parser name

    Args:
        parser_name (str): name of custom parser, defaults to "generic"

    Returns:
        parser or None
    """
    if parser_name:
        parser = parsers.get(parser_name, None)
        if parser:
            return parser["func"]
    return parsers["generic"]["func"]


class DictToObject:
    def __init__(self, dictionary):
        # Store the dictionary internally
        self._dict = dictionary
        for key, value in dictionary.items():
            setattr(self, key, value)

    def get(self, key, default=None):
        return getattr(self, key, default)

    # Add dictionary-like methods
    def items(self):
        return self._dict.items()

    def keys(self):
        return self._dict.keys()

    def values(self):
        return self._dict.values()

    def __iter__(self):
        # Allows iteration over keys, just like a dictionary
        return iter(self._dict)

    def __contains__(self, item):
        # Allows use of `in` keyword to check for keys
        return item in self._dict
    
    def __getitem__(self, key):
        # Allows getting values using square brackets
        return getattr(self, key, None)
    
    def __setitem__(self, key, value):
        # Allows setting values using square brackets
        setattr(self, key, value)
    
    def to_string(self):
        return str(self._dict)
