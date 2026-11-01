"""
Area creation utilities with automatic geocoding.

This module provides functionality to create Area objects automatically
with geocoding using pgeocode and geopy.
"""

from django.conf import settings

import re
import time
from logging import getLogger
from decimal import Decimal
from math import radians, sin, cos, acos

import pgeocode
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderUnavailable


from content.models import Area

logger = getLogger("ai_area_assignment")

# Globale Variablen für lazy initialization
_pgeocode_nomi = None
_pgeocode_df = None
_geopy_geolocator = None


def _is_area_near(area, event_lat, event_lon, max_km):
    """
    Plausibilisiert einen Area-Match gegen die Event-Koordinaten.
    Schützt vor Namens-Kollisionen wie Lindau (Bodensee) vs. Lindau (Holstein).

    Liefert True wenn:
    - keine Event-Koord übergeben wurden (dann ist kein Check möglich)
    - die Area keine eigenen Koord hat
    - der Abstand <= max_km ist
    """
    if event_lat is None or event_lon is None:
        return True
    if area is None or area.latitude is None or area.longitude is None:
        return True
    try:
        lat1, lon1, lat2, lon2 = map(
            radians,
            [float(event_lat), float(event_lon), float(area.latitude), float(area.longitude)],
        )
        d = 6371 * acos(min(1.0, sin(lat1) * sin(lat2) + cos(lat1) * cos(lat2) * cos(lon1 - lon2)))
        return d <= max_km
    except Exception:
        return True


def _init_pgeocode():
    """Lazy initialization für pgeocode"""
    global _pgeocode_nomi, _pgeocode_df
    if _pgeocode_nomi is None:
        _pgeocode_nomi = pgeocode.Nominatim('de')
        df = _pgeocode_nomi._data
        # Nur echte Orts-PLZ als Ortsnamen-Quelle: die GeoNames-Tabelle fuehrt
        # daneben rund 8.200 Grosskunden-PLZ, deren place_name der Firmenname ist
        # ("Hamburg Commercial Bank AG", "Sparkasse Holstein"). Ueber die
        # Namenssuche und das known_de-Gate sind daraus Areas wie
        # "Egon Oldendorff Management GmbH" entstanden. Grosskunden-Zeilen haben
        # accuracy = NaN; die PLZ-Lookups ueber _pgeocode_nomi.query_postal_code
        # bleiben davon unberuehrt und finden solche PLZ weiterhin.
        _pgeocode_df = df[df['accuracy'].notna()]
    return _pgeocode_nomi, _pgeocode_df


def _init_geopy():
    """Lazy initialization für geopy"""
    global _geopy_geolocator
    if _geopy_geolocator is None:
        _geopy_geolocator = Nominatim(user_agent=settings.GEOCODER_USER_AGENT, timeout=10)
    return _geopy_geolocator


def _state_code_for_zip(zip_code):
    """Bundesland-Kürzel (z.B. 'SH', 'MV') für eine PLZ via pgeocode (offline)."""
    if not zip_code:
        return None
    try:
        nomi, _ = _init_pgeocode()
        result = nomi.query_postal_code(str(zip_code).strip())
        code = str(result.state_code) if result is not None else ''
        return code if code and code != 'nan' else None
    except Exception as e:
        logger.warning(f"Bundesland-Lookup für PLZ '{zip_code}' fehlgeschlagen: {e}")
        return None


def get_or_create_area(town_name, zip_code=None, latitude=None, longitude=None, create_if_not_found=True, max_distance_km=25):
    """
    Holt eine Area aus der DB oder erstellt sie automatisch mit Geocoding.

    Priorität beim Suchen:
    1a. Exakter Match
    1b. Case-insensitiver Match (z.B. "BREMEN" -> "Bremen")
    1c. Bereinigter Match (z.B. "Bremen." -> "Bremen")
    1d. PLZ-Match gegen existierende Areas

    Nur wenn create_if_not_found=True (Standard):
    2. pgeocode mit PLZ (wenn vorhanden)
    3. pgeocode mit Stadtname
    4. geopy Geocoding (Fallback)
    5. Koordinaten aus JSON

    Wenn latitude/longitude übergeben sind, werden Namens-/PLZ-Matches daraufhin
    validiert, dass die gefundene Area nicht weiter als max_distance_km vom Event
    entfernt liegt. Das verhindert Namens-Kollisionen wie Lindau (Bodensee) vs.
    Lindau (Holstein). Ohne Event-Koord bleibt das Verhalten unverändert.

    Args:
        town_name: Name der Stadt/Gemeinde
        zip_code: Optional - PLZ
        latitude: Optional - Koordinaten
        longitude: Optional - Koordinaten
        create_if_not_found: Wenn False, wird keine neue Area angelegt (Standard: True)
        max_distance_km: Max. Distanz zwischen Event-Koord und Area-Koord beim
            Namens-/PLZ-Match (nur aktiv wenn latitude/longitude gesetzt sind)

    Returns:
        tuple: (Area, created: bool) oder (None, False)
    """
    if not town_name:
        return None, False

    town_name = town_name.strip()
    seen_ids = set()

    def _try_match(qs, via):
        """
        Iteriert Kandidaten aus dem Queryset, überspringt bereits geprüfte,
        und liefert die erste Area, die den Distanz-Check besteht.
        Deckt auch den Fall ab, dass mehrere gleichnamige Areas existieren.
        """
        for candidate in qs.order_by('id'):
            if candidate.id in seen_ids:
                continue
            seen_ids.add(candidate.id)
            if _is_area_near(candidate, latitude, longitude, max_distance_km):
                return candidate
            logger.info(
                f"Area '{candidate.name}' (ID={candidate.id}) via {via} verworfen: "
                f">{max_distance_km} km von Event-Koord ({latitude},{longitude})"
            )
        return None

    # 1a. Exakter (case-insensitiver) Name-Match hat Vorrang.
    match = _try_match(Area.objects.filter(name__iexact=town_name), "Name-Match")
    if match:
        logger.info(f"Area '{town_name}' via Name-Match gefunden: '{match.name}' (ID={match.id})")
        return match, False

    # 1b. Sonst Areas, die den Namen um einen Zusatz erweitern - "Mölln (SH)",
    # "Frankfurt (Oder)", "Frankfurt am Main". Der Zusatz muss an einer
    # Wortgrenze beginnen, damit "Halle" nicht "Hallenberg" trifft.
    #
    # Frueher wurde hier nur die Klammerform gesucht, gleichrangig mit dem
    # exakten Match und entschieden ueber die kleinste ID. "Frankfurt" landete
    # dadurch systematisch in "Frankfurt (Oder)": "Frankfurt am Main" passte
    # nicht auf das Klammer-Muster und war nie Kandidat. Bleibt mehr als eine
    # Erweiterung uebrig, entscheidet nur der Distanz-Check - ohne Koordinaten,
    # wie bei der KI-Gebietszuweisung von Artikeln, ist kein Gebiet besser als
    # ein falsches.
    erweiterungen = [
        area
        for area in Area.objects.filter(name__istartswith=town_name)
        if re.match(rf"^{re.escape(town_name)}[\s\-/(,]", area.name, re.IGNORECASE)
    ]
    if len(erweiterungen) > 1 and (latitude is None or longitude is None):
        logger.info(
            f"Area '{town_name}' ist mehrdeutig "
            f"({', '.join(a.name for a in erweiterungen)}) und ohne Koordinaten "
            f"nicht aufloesbar - uebersprungen"
        )
        return None, False
    if erweiterungen:
        match = _try_match(
            Area.objects.filter(id__in=[a.id for a in erweiterungen]), "Namens-Erweiterung"
        )
        if match:
            logger.info(f"Area '{town_name}' via Namens-Erweiterung gefunden: '{match.name}' (ID={match.id})")
            return match, False

    # 1c. Bereinigter Match: führende/abschließende Satzzeichen entfernen
    cleaned = re.sub(r'^[\s.\-_,;]+|[\s.\-_,;]+$', '', town_name)
    if cleaned and cleaned.lower() != town_name.lower():
        match = _try_match(Area.objects.filter(name__iexact=cleaned), "bereinigtem Match")
        if match:
            logger.info(f"Area '{town_name}' via bereinigtem Match gefunden: '{match.name}' (ID={match.id})")
            return match, False

    # 1d. PLZ-basierter Match
    if zip_code:
        match = _try_match(Area.objects.filter(zip=str(zip_code).strip()), f"PLZ-Match ({zip_code})")
        if match:
            logger.info(f"Area '{town_name}' via PLZ-Match gefunden: '{match.name}' (ID={match.id}, PLZ={zip_code})")
            return match, False

    # 1e. pgeocode-Lookup: Ortsnamen via pgeocode in PLZ auflösen, dann PLZ gegen DB prüfen
    resolved_zip = None
    try:
        _, df = _init_pgeocode()
        # Nur der exakte Ortsname. Ein contains-Fallback stand hier frueher und
        # zog z.B. "Neustadt" auf die erstbeste PLZ, die "Neustadt" enthaelt -
        # so landeten Meldungen aus Neustadt an der Weinstrasse, Bremen-Neustadt
        # und Neustadt in Holstein allesamt in "Neustadt in Sachsen".
        matches = df[df['place_name'].str.lower() == town_name.lower()]
        if not matches.empty:
            resolved_zip = str(matches.iloc[0]['postal_code'])
    except Exception as e:
        logger.warning(f"pgeocode Lookup für '{town_name}' fehlgeschlagen: {e}")

    if resolved_zip:
        match = _try_match(Area.objects.filter(zip=resolved_zip), f"pgeocode-PLZ-Match ({resolved_zip})")
        if match:
            logger.info(f"Area '{town_name}' via pgeocode-PLZ-Match gefunden: '{match.name}' (ID={match.id}, PLZ={resolved_zip})")
            return match, False

    # 1f. geopy-Lookup: Adresse geocodieren, PLZ und Stadtname aus Ergebnis gegen DB prüfen
    _geopy_result = None
    try:
        geolocator = _init_geopy()
        time.sleep(1.1)  # Rate-Limit einhalten (max 1 Request/Sekunde)
        _geopy_result = geolocator.geocode(f"{town_name}, Germany", addressdetails=True)
        if _geopy_result:
            addr = _geopy_result.raw.get('address', {})
            geo_zip = addr.get('postcode')
            geo_city = addr.get('city') or addr.get('town') or addr.get('village') or addr.get('municipality')

            if geo_city:
                match = _try_match(
                    Area.objects.filter(name__iexact=geo_city),
                    f"geopy-Stadt-Match ({geo_city})",
                )
                if match:
                    logger.info(f"Area '{town_name}' via geopy-Stadt-Match gefunden: '{match.name}' (ID={match.id}, Stadt={geo_city})")
                    return match, False

            if geo_zip:
                match = _try_match(
                    Area.objects.filter(zip=geo_zip),
                    f"geopy-PLZ-Match ({geo_zip})",
                )
                if match:
                    logger.info(f"Area '{town_name}' via geopy-PLZ-Match gefunden: '{match.name}' (ID={match.id}, PLZ={geo_zip})")
                    return match, False
    except Exception as e:
        logger.warning(f"geopy Lookup für '{town_name}' fehlgeschlagen: {e}")

    if not create_if_not_found:
        logger.info(f"Area '{town_name}' nicht gefunden und create_if_not_found=False - übersprungen")
        return None, False

    # Gate: Only auto-create if town_name exists as a German place name in pgeocode.
    # Rejects numeric/garbage names (e.g. "24119", "420 0") that Nominatim would
    # otherwise resolve to unrelated or foreign postal codes.
    try:
        _, df = _init_pgeocode()
        known_de = (
            not df[df['place_name'].str.lower() == town_name.lower()].empty
            or not df[df['place_name'].str.contains(
                town_name, case=False, na=False, regex=False
            )].empty
        )
    except Exception as e:
        logger.warning(f"pgeocode-Validierung für '{town_name}' fehlgeschlagen: {e}")
        known_de = False

    if not known_de:
        logger.warning(
            f"Area '{town_name}' ist kein bekannter deutscher Ort - keine automatische Erstellung"
        )
        return None, False

    logger.info(f"Area '{town_name}' nicht gefunden - versuche automatische Erstellung...")

    area_data = None

    # 2. Wenn PLZ vorhanden -> pgeocode (am zuverlässigsten, offline)
    if zip_code:
        try:
            nomi, _ = _init_pgeocode()
            result = nomi.query_postal_code(str(zip_code).strip())
            if result is not None and str(result.place_name) != 'nan':
                area_data = {
                    'latitude': Decimal(str(result.latitude)),
                    'longitude': Decimal(str(result.longitude)),
                    'zip': str(zip_code).strip()
                }
                logger.info(f"pgeocode (PLZ): {zip_code} -> Lat:{result.latitude:.4f}, Lon:{result.longitude:.4f}")
        except Exception as e:
            logger.warning(f"pgeocode PLZ-Lookup fehlgeschlagen: {e}")

    # 3. Stadtname in pgeocode-Datenbank suchen (offline, schnell)
    if not area_data:
        try:
            _, df = _init_pgeocode()
            matches = df[df['place_name'].str.lower() == town_name.lower()]
            if matches.empty:
                matches = df[df['place_name'].str.contains(town_name, case=False, na=False)]

            if not matches.empty:
                # Bei mehreren Treffern (z.B. "Lindau" in Bayern und Holstein): den
                # nächsten zur Event-Koord wählen, statt blind iloc[0] zu nehmen.
                best = matches.iloc[0]
                if latitude is not None and longitude is not None and len(matches) > 1:
                    try:
                        ev_lat_r, ev_lon_r = radians(float(latitude)), radians(float(longitude))
                        def _dist_row(row):
                            try:
                                la, lo = radians(float(row['latitude'])), radians(float(row['longitude']))
                                return 6371 * acos(min(1.0, sin(ev_lat_r) * sin(la) + cos(ev_lat_r) * cos(la) * cos(ev_lon_r - lo)))
                            except Exception:
                                return float('inf')
                        matches = matches.copy()
                        matches['_dist_km'] = matches.apply(_dist_row, axis=1)
                        best = matches.loc[matches['_dist_km'].idxmin()]
                    except Exception as e:
                        logger.warning(f"pgeocode-Distanz-Sortierung fehlgeschlagen: {e}")

                area_data = {
                    'latitude': Decimal(str(best['latitude'])),
                    'longitude': Decimal(str(best['longitude'])),
                    'zip': str(best['postal_code'])
                }
                logger.info(f"pgeocode (Stadt): {town_name} -> PLZ:{best['postal_code']}, Lat:{float(best['latitude']):.4f}")
        except Exception as e:
            logger.warning(f"pgeocode Stadt-Lookup fehlgeschlagen: {e}")

    # 4. geopy-Ergebnis aus Schritt 1f wiederverwenden (kein zweiter API-Call)
    if not area_data and _geopy_result:
        try:
            addr = _geopy_result.raw.get('address', {})
            area_data = {
                'latitude': Decimal(str(_geopy_result.latitude)),
                'longitude': Decimal(str(_geopy_result.longitude)),
                'zip': addr.get('postcode') or zip_code
            }
            logger.info(f"geopy (gecacht): {town_name} -> Lat:{_geopy_result.latitude:.4f}, Lon:{_geopy_result.longitude:.4f}")
        except Exception as e:
            logger.warning(f"geopy Ergebnis konnte nicht verarbeitet werden: {e}")

    # 4b. Fallback: geopy neu aufrufen falls kein gecachtes Ergebnis (z.B. bei Fehler oben)
    if not area_data and _geopy_result is None:
        try:
            geolocator = _init_geopy()
            time.sleep(1.1)
            location = geolocator.geocode(f"{town_name}, Germany", addressdetails=True)
            if location:
                addr = location.raw.get('address', {})
                area_data = {
                    'latitude': Decimal(str(location.latitude)),
                    'longitude': Decimal(str(location.longitude)),
                    'zip': addr.get('postcode') or zip_code
                }
                logger.info(f"geopy: {town_name} -> Lat:{location.latitude:.4f}, Lon:{location.longitude:.4f}")
        except (GeocoderTimedOut, GeocoderUnavailable) as e:
            logger.warning(f"geopy Fehler für '{town_name}': {e}")
        except Exception as e:
            logger.warning(f"geopy unerwarteter Fehler für '{town_name}': {e}")

    # 5. Letzter Fallback: Koordinaten aus JSON
    if not area_data and latitude and longitude:
        try:
            area_data = {
                'latitude': Decimal(str(latitude)),
                'longitude': Decimal(str(longitude)),
                'zip': str(zip_code) if zip_code else None
            }
            logger.info(f"JSON-Koordinaten: {town_name} -> Lat:{latitude}, Lon:{longitude}")
        except Exception as e:
            logger.warning(f"Fehler bei JSON-Koordinaten: {e}")

    # Area erstellen wenn Geodaten gefunden wurden
    if area_data:
        # Require a valid German PLZ (5 digits) before creating the area.
        zip_str = str(area_data.get('zip') or '').strip()
        if not re.fullmatch(r'\d{5}', zip_str):
            logger.warning(
                f"Area '{town_name}' ohne gültige deutsche PLZ - nicht erstellt (zip={zip_str!r})"
            )
            return None, False
        # Plausibilisiere neue Area-Koord gegen Event-Koord (falls vorhanden).
        # Verhindert, dass z.B. bei "Lindau" ohne eindeutigen Treffer eine Area
        # mit Koord angelegt wird, die nicht zum Event-Ort passt.
        if latitude is not None and longitude is not None:
            try:
                ev_lat_r, ev_lon_r = radians(float(latitude)), radians(float(longitude))
                ar_lat_r, ar_lon_r = radians(float(area_data['latitude'])), radians(float(area_data['longitude']))
                d = 6371 * acos(min(1.0, sin(ev_lat_r) * sin(ar_lat_r) + cos(ev_lat_r) * cos(ar_lat_r) * cos(ev_lon_r - ar_lon_r)))
                if d > max_distance_km:
                    logger.warning(
                        f"Area '{town_name}' nicht erstellt: Kandidat-Koord "
                        f"({area_data['latitude']},{area_data['longitude']}) liegt {d:.0f} km "
                        f"vom Event ({latitude},{longitude}) - >{max_distance_km} km"
                    )
                    return None, False
            except Exception as e:
                logger.warning(f"Koord-Plausibilisierung fehlgeschlagen: {e}")
        # Area.name ist unique: existiert der Name schon (anderer Ort gleichen
        # Namens, z.B. Mölln in MV vs. SH), mit Bundesland-Kürzel disambiguieren.
        create_name = town_name
        if Area.objects.filter(name__iexact=create_name).exists():
            state_code = _state_code_for_zip(zip_str)
            if not state_code:
                logger.warning(
                    f"Area '{town_name}' existiert bereits (anderer Ort) und kein "
                    f"Bundesland-Kürzel für PLZ {zip_str!r} ermittelbar - nicht erstellt"
                )
                return None, False
            create_name = f"{town_name} ({state_code})"
            if Area.objects.filter(name__iexact=create_name).exists():
                logger.warning(
                    f"Area '{create_name}' existiert bereits, passt aber nicht zu den "
                    f"Event-Koordinaten - nicht erstellt"
                )
                return None, False

        try:
            area = Area.objects.create(
                name=create_name,
                latitude=area_data['latitude'],
                longitude=area_data['longitude'],
                zip=zip_str,
                visible=True
            )
            logger.info(f"Area '{create_name}' erstellt: PLZ={area.zip}, Lat={area.latitude}, Lon={area.longitude}")
            return area, True
        except Exception as e:
            logger.error(f"Fehler beim Erstellen der Area '{create_name}': {e}")
            return None, False

    logger.warning(f"Keine Geodaten für '{town_name}' gefunden - Area konnte nicht erstellt werden")
    return None, False


def reassign_area_by_coords(event, max_distance_km=50):
    """
    Korrigiert die Area-Zuordnung eines Events anhand seiner Koordinaten.

    Gedacht zum Aufruf, nachdem ein Event neue Koordinaten bekommen hat
    (z.B. durch update_eventv4_coordinates): Areas, die jetzt weiter als
    max_distance_km vom Event entfernt liegen, werden entfernt und durch
    eine passende Area ersetzt, sofern get_or_create_area eine findet.

    Liefert True wenn Zuordnung verändert wurde.
    """
    if event.latitude is None or event.longitude is None:
        return False

    areas = list(event.area.all())
    far = [a for a in areas if not _is_area_near(a, event.latitude, event.longitude, max_distance_km)]
    if not far:
        return False

    changed = False
    has_close = any(a for a in areas if a not in far)
    if not has_close:
        # Suche passende Area (mit Distanz-Check aktiv in get_or_create_area)
        new_area, _ = get_or_create_area(
            event.town,
            zip_code=event.zip_code,
            latitude=event.latitude,
            longitude=event.longitude,
            create_if_not_found=False,
            max_distance_km=max_distance_km,
        )
        if new_area and new_area not in areas:
            event.area.add(new_area)
            changed = True

    for a in far:
        event.area.remove(a)
        changed = True

    return changed
