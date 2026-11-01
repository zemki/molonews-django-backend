
from logging import getLogger
from urllib.parse import urlparse
from PIL import Image, ImageFile
import requests
import re
from bs4 import BeautifulSoup
import html

from content.import_tools.url_safety import safe_get

IMAGE_MIN_SIZE = 150

logger = getLogger("molonews")

GERMAN_WEEKDAY_MAP = {
    "Mo,": "Mon,",
    "Di,": "Tue,",
    "Mi,": "Wed,",
    "Do,": "Thu,",
    "Fr,": "Fri,",
    "Sa,": "Sat,",
    "So,": "Sun,"
}

GERMAN_MONTH_MAP = {
    "Jan": "Jan",  
    "Feb": "Feb",
    "Mär": "Mar",  
    "Apr": "Apr",
    "Mai": "May",
    "Jun": "Jun",
    "Jul": "Jul",
    "Aug": "Aug",
    "Sep": "Sep",
    "Okt": "Oct",
    "Nov": "Nov",
    "Dez": "Dec",
}

def convert_german_datetime_str(dt_str: str) -> str:
    """
    Ersetzt in einem Datums-String die gängigen deutschen Wochentage sowie Monats-Abkürzungen 
    durch englische, damit dateutil.parser.parse() damit umgehen kann.
    """
    # 1) Wochentage ersetzen (z.B. "Sa," -> "Sat,")
    for ger_wd, eng_wd in GERMAN_WEEKDAY_MAP.items():
        dt_str = dt_str.replace(ger_wd, eng_wd)

    # 2) Monate ersetzen (z.B. "Mär" -> "Mar")
    for ger_mon, eng_mon in GERMAN_MONTH_MAP.items():
        dt_str = dt_str.replace(ger_mon, eng_mon)

    return dt_str

def correct_encoding(text, source):
    try:
        if source == "Lüne Blog Quelle":
            return text.encode('latin-1').decode('utf-8')
    except:
        pass
    
    return text 

def extract_address_parts_json (address: str):
    try:
        parts = address.rsplit(",", 1)  
        if len(parts) < 2:
            return address, "", ""  
        
        street = parts[0].strip()
        plz_ort_match = re.match(r"(\d{5})\s+(.+)", parts[1].strip())
        
        if plz_ort_match:
            plz, ort = plz_ort_match.groups()
        else:
            plz, ort = "", parts[1].strip()
        
        return street, plz, ort
    except Exception as e:
        print(f"Fehler bei der Verarbeitung der Adresse: {e}")
        return "", "", ""

def clean_content(html_content):
    # HTML-Tags die erhalten bleiben sollen (einfach erweiterbar)
    ALLOWED_TAGS = ['p', 'br']

    # Links erhalten und target="_blank" setzen
    placeholders = {}
    for link_match in re.finditer(r'<a\s+href=["\']([^"\']*)["\'][^>]*>', html_content, re.IGNORECASE):
        original = link_match.group(0)
        url = link_match.group(1)
        key = f'\x00KEEP{len(placeholders)}\x00'
        placeholders[key] = f'<a href="{url}" target="_blank" rel="noopener noreferrer">'
        html_content = html_content.replace(original, key, 1)
    for variant in re.findall(r'</a>', html_content, re.IGNORECASE):
        key = f'\x00KEEP{len(placeholders)}\x00'
        placeholders[key] = '</a>'
        html_content = html_content.replace(variant, key, 1)

    # Erlaubte Tags mit Platzhaltern schützen
    for tag in ALLOWED_TAGS:
        for variant in re.findall(rf'</?{tag}\s*/?\s*>', html_content, re.IGNORECASE):
            key = f'\x00KEEP{len(placeholders)}\x00'
            placeholders[key] = variant
            html_content = html_content.replace(variant, key, 1)

    # Alle übrigen HTML-Tags entfernen
    html_content = re.sub(r'<.*?>', '', html_content)

    # Platzhalter wieder durch die erlaubten Tags ersetzen
    for key, original in placeholders.items():
        html_content = html_content.replace(key, original)

    return html_content.strip()

def convert_escaped_tags(description: str) -> str:
    """
    Ersetzt einige typische HTML-Escape-Tags (z.B. &lt;p&gt;, &lt;br&gt;)
    durch ihre echten HTML-Pendants (<p>, <br>).
    """
    replacements = {
        '&lt;br&gt;': '<br>',
        '&lt;br /&gt;': '<br />',
        '&lt;p&gt;': '<p>',
        '&lt;/p&gt;': '</p>',
        '&lt;': '<',
        '&gt;': '>',
    }

    for escaped, real_tag in replacements.items():
        description = description.replace(escaped, real_tag)
    
    return description


def get_baseurl(url):
    """
    This function takes a URL as an input and returns the base URL.
    It uses the urlparse function from the urllib.parse module to parse the URL into its components.
    It then formats and returns the base URL, which consists of the scheme (e.g., http, https) and the network location (netloc).

    Parameters:
    url (str): The URL to parse.

    Returns:
    str: The base URL.
    """
    parsed_link = urlparse(url)
    return "{}://{}".format(parsed_link.scheme, parsed_link.netloc)


def get_image_infos(image_url):
    if not image_url or not image_url.startswith(("http://", "https://")):
        logger.warning(f"Skipping invalid image URL (no scheme): {image_url}")
        return None
    try:
        # Some origin servers reject the
        # python-requests default User-Agent with 403, so mirror the browser UA
        # used in import_articles.py for the feed fetch.
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        # Bild-URLs stammen aus fremden Feeds: interne Adressen sperren (SSRF)
        with safe_get(image_url, timeout=10, headers=headers, stream=True) as response:
            response.raise_for_status()  # Raise an error for HTTP issues
            content_type = response.headers.get("Content-Type", "")

            # Check if the response is an image
            if not content_type.startswith("image/"):
                logger.error(f"Invalid content type for URL {image_url}: {content_type}")
                return None

            # Aufrufer brauchen nur Breite und Hoehe, und die stehen im Dateikopf -
            # meist in den ersten Kilobytes. Deshalb streamen und abbrechen, sobald
            # PIL das Bild erkannt hat, statt mehrere hundert KB zu laden. Erkennt
            # PIL nichts, ist am Ende trotzdem die ganze Datei durchgelaufen.
            parser = ImageFile.Parser()
            for chunk in response.iter_content(8192):
                parser.feed(chunk)
                if parser.image:
                    return parser.image  # Return image dimensions (width, height)

        logger.error(f"Cannot identify image file from URL {image_url}")
    except ValueError as e:
        logger.warning(f"Blocked image URL {image_url}: {e}")
    except requests.RequestException as e:
        logger.error(f"Request failed for URL {image_url}: {e}")
    except Image.UnidentifiedImageError:
        logger.error(f"Cannot identify image file from URL {image_url}")
    except Exception as e:
        logger.error(f"Unexpected error for URL {image_url}: {e}")
    return None

def image_has_min_size(image_url):
    image_infos = get_image_infos(image_url)
    if (
        image_infos
        and image_infos.width >= IMAGE_MIN_SIZE
        and image_infos.height >= IMAGE_MIN_SIZE
    ):
        return True
    return False


def normalize_image_url(image_url, base_url):
    if image_url.startswith("//"):
        return "https:{}".format(image_url)
    elif image_url.startswith("/"):
        return "{}{}".format(base_url, image_url)
    elif not image_url.startswith(("http://", "https://")):
        return None
    return image_url


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



def get_image(image_url, default_image):
    image = None
    if not image_url:
        image = default_image
    return image


def get_og_image(link):
    """
    Diese Funktion nimmt einen Link als Eingabe und versucht, das OpenGraph-Bild des Links zu extrahieren.
    Wenn das Bild existiert und die Mindestgröße erfüllt, wird das Bild zurückgegeben.
    Bei einem Fehler während des Prozesses wird None zurückgegeben.

    Parameter:
    link (str): Der Link der Webseite, von der das OpenGraph-Bild extrahiert werden soll.

    Rückgabe:
    str: Der Link zum OpenGraph-Bild, wenn es existiert und die Mindestgröße erfüllt. Andernfalls None.
    """
    try:
        # Mit Zeitlimit: ohne hängt ein einzelner Abruf bei einem nicht
        # antwortenden Server minutenlang - und das in jedem Importlauf erneut.
        # safe_get sperrt interne Adressen, auch nach Weiterleitungen (SSRF).
        response = safe_get(link, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        content_type = response.headers.get("Content-Type", "")
        if response.status_code >= 400 or (content_type and "html" not in content_type):
            return None
        tag = BeautifulSoup(response.text, "html.parser").find("meta", property="og:image")
        image = (tag.get("content") or "").strip() if tag else ""
        if image and image_has_min_size(image):
            return image
    except Exception:
        return None
    return None




def extract_image_from_content_encoded(entry):
    """
    Extrahiere das erste Bild aus dem content:encoded-Element des RSS-Feeds.
    Unterstützt: <img> tags, YouTube iframes, und CSS background-images.
    """
    if 'content' in entry:
        # Parse the content:encoded field using BeautifulSoup
        for content_item in entry['content']:
            if 'value' in content_item:
                soup = BeautifulSoup(content_item['value'], 'html.parser')

                # 1. Try YouTube iframe first (video portals)
                iframe = soup.find('iframe', src=re.compile(r'youtube\.com/embed/'))
                if iframe and iframe.get('src'):
                    # Extract video ID from URL like: https://www.youtube.com/embed/VIDEO_ID
                    match = re.search(r'youtube\.com/embed/([a-zA-Z0-9_-]+)', iframe['src'])
                    if match:
                        video_id = match.group(1)
                        return f'https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg'

                # 2. Try to find background-image in style attributes
                for tag in soup.find_all(style=re.compile(r'background-image')):
                    style = tag.get('style', '')
                    # Extract URL from background-image:url(...)
                    bg_match = re.search(r'background-image:\s*url\(["\']?(//|https?://)?([^"\')\s]+)["\']?\)', style)
                    if bg_match:
                        url = bg_match.group(0)
                        # Extract the actual URL part
                        url_match = re.search(r'url\(["\']?(//|https?://)?([^"\')\s]+)["\']?\)', url)
                        if url_match:
                            extracted_url = url_match.group(2)
                            if not extracted_url.startswith('http'):
                                extracted_url = 'https://' + extracted_url
                            return extracted_url

                # 3. Standard <img> tag (existing functionality)
                img_tag = soup.find('img')
                if img_tag and 'src' in img_tag.attrs:
                    # Return the image URL from the 'src' attribute
                    return img_tag['src']

    # Return None if no image found
    return None



def get_image_url(entry, default_image_url=None):
    """
    Holt die URL des Bildes aus verschiedenen möglichen Feldern des RSS-Feeds.
    """
    if hasattr(entry, "force_default_image") and entry.force_default_image:
        return default_image_url
    
    article_link = entry.link
    base_url = get_baseurl(article_link)
    
    # Check for media content
    for media_content in entry.get("media_content", []):
        media_type = media_content.get("type", "") if hasattr(media_content, "get") else ""
        if media_type.startswith("image/") and "url" in media_content:
            image_url = normalize_image_url(media_content["url"], base_url)
            if image_url and image_has_min_size(image_url):
                return image_url
        if "url" in media_content:
            image_url = normalize_image_url(media_content["url"], base_url)
            if image_url:
                # Strip query string before checking the file extension so URLs like
                # ".../foo.jpeg?auth=…&width=596" are still recognized as images.
                path_only = urlparse(image_url).path.lower()
                if path_only.endswith(('.jpg', '.jpeg', '.png', '.gif')):
                    if image_has_min_size(image_url):
                        return image_url
    
    if hasattr(entry, "image_url") and entry.image_url:
        logger.info("Image URL found in entry: %s", entry.image_url)
        image = entry.image_url
        image_url = normalize_image_url(image, base_url)
        if image_has_min_size(image_url):
            return image_url
    
    if hasattr(entry, "links"):
        for link in entry.links:
            if link.get("rel") == "enclosure" and link.get("type", "").startswith("image/"):
                image_url = normalize_image_url(link["href"], base_url)
                if image_url and image_has_min_size(image_url):
                    return image_url
        

    if 'greifswald' in entry.link.lower():
        if 'href' in entry:
            href_val = normalize_image_url(entry['href'], base_url)
            if href_val and href_val.lower().endswith(('.jpg', '.jpeg', '.png', '.gif')):
                return href_val

    # Try OpenGraph image
    og_image = get_og_image(article_link)
    if og_image:
        return og_image

    # Check if entry has an image_url
    if hasattr(entry, "image_url") and entry.image_url:
        image = entry.image_url
        image_url = normalize_image_url(image, base_url)
        if image_has_min_size(image_url):
            return image_url

    # Check for images in content:encoded
    content_encoded_image = extract_image_from_content_encoded(entry)
    if content_encoded_image:
        image_url = normalize_image_url(content_encoded_image, base_url)
        if image_has_min_size(image_url):
            return image_url
        
    return default_image_url


def fix_misencoded_text(text):
    if isinstance(text, bytes):
        text = text.decode('utf-8', errors='replace')  # oder zuerst versuchen utf-8
    text = html.unescape(text)
    replacements = {
        'Ã¼': 'ü', 'Ãœ': 'Ü', 'Ã¤': 'ä', 'Ã„': 'Ä', 'Ã¶': 'ö', 'Ã–': 'Ö', 'ÃŸ': 'ß',
        'Ã©': 'é', 'Ã¨': 'è', 'Ã ': 'à', 'Ã´': 'ô', 'Ãª': 'ê', 'Ã¢': 'â', 'Ã¹': 'ù',
        'Ã»': 'û', 'â€“': '–', 'â€”': '—', 'â€ž': '„', 'â€œ': '“', 'â€': '”', 
        'â€˜': '‘', 'â€™': '’', 'â‚¬': '€', 'â€¦': '…', 'ï¿œ': 'ü'
    }
    for wrong, correct in replacements.items():
        text = text.replace(wrong, correct)
    return text

def effective_location(location, street=None, town=None):
    """Veranstaltungsort, ersatzweise aus Straße und Stadt gebildet.

    Liefert ein Feed keinen Ort, trägt der Cron fill_event_location spätestens
    eine Stunde später "Straße, Stadt" in event_location ein. Die Importer
    erkennen ein Event aber an Titel, Zeit und Ort wieder: mit dem leeren Ort
    aus dem Feed fanden sie das nachträglich ergänzte Event nie und haben es bei
    jedem Lauf gelöscht und neu angelegt (rund 330 Events pro Woche, jedes Mal
    mit neuer ID und ohne KI-Tags). Deshalb bilden jetzt beide Seiten - Feed und
    Bestand - den Ort nach derselben Regel wie fill_event_location.

    Die Längen folgen den Modellfeldern street/town (varchar(100)).
    """
    if location is not None and str(location).strip():
        return location if isinstance(location, str) else str(location)
    street = str(street or '')[:100].strip()
    town = str(town or '')[:100].strip()
    if street and town:
        return f"{street}, {town}"
    return street or town


def fix_misencoded_text_deep(obj):
    if isinstance(obj, str):
        return fix_misencoded_text(obj)
    elif isinstance(obj, list):
        return [fix_misencoded_text_deep(item) for item in obj]
    elif isinstance(obj, dict):
        return {key: fix_misencoded_text_deep(value) for key, value in obj.items()}
    else:
        return obj

def strip_xml_encoding_declaration(text):
    """
    Entfernt die XML-Encoding-Zeile, wenn vorhanden.
    """
    return re.sub(r'^<\?xml[^>]+\?>', '', text).lstrip()

def normalize_url(value):
    """Kaputte URLs aus Feeds geradeziehen.

    Feeds liefern immer wieder Adressen mit Leerzeichen. Ungepruefte Uebernahme
    fuehrt zu Links, die in der App nicht klickbar sind. Drei Faelle treten auf:

    1. Leerzeichen direkt hinter dem Schema
       "http:// www.example.de"          -> "http://www.example.de"
    2. Zwei Adressen in einem Feld; nur die erste ist gemeint
       "https://a.de/ https://b.de/"     -> "https://a.de/"
    3. Leerzeichen im Wert eines Query-Parameters - das gehoert zur Adresse und
       darf nicht abgeschnitten, sondern muss kodiert werden
       "...?camefrom=A B&x=1"            -> "...?camefrom=A%20B&x=1"

    Fall 3 ist der Grund, warum hier nicht einfach am ersten Leerzeichen
    abgeschnitten wird: das verstuemmelte sonst Ticket-Links mit Leerzeichen.
    """
    if not value:
        return ''

    url = str(value).strip()
    if not url:
        return ''

    # Fall 1
    url = re.sub(r'^(https?://)\s+', r'\1', url)

    # Fall 2: folgt nach einem Leerzeichen ein neues Schema, endet die erste URL dort
    zweite = re.search(r'\s+(?=https?://)', url)
    if zweite:
        url = url[:zweite.start()]

    # Fall 3
    url = url.strip().replace(' ', '%20')

    return url
