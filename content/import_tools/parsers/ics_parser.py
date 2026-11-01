
from logging import getLogger
from icalendar import Calendar

IMAGE_MIN_SIZE = 150

logger = getLogger("molonews")


def parse_ics_for_events(ics_content, base_url):
    try:
        cal = Calendar.from_ical(ics_content)
    except Exception:
        logger.error("Error parsing .ics content")
        return []

    events = []
    for component in cal.walk():
        if component.name == "VEVENT":
            try:
                title = str(component.get("summary")).strip()
                start_datetime = component.get("dtstart").dt
                # DTEND ist im iCalendar-Standard optional: es darf stattdessen
                # eine DURATION dastehen oder ganz fehlen. Der frühere direkte
                # Zugriff warf dann einen AttributeError - der Termin fiel still
                # aus dem Import.
                dtend = component.get("dtend")
                if dtend is not None:
                    end_datetime = dtend.dt
                else:
                    duration = component.get("duration")
                    end_datetime = (
                        start_datetime + duration.dt if duration is not None else start_datetime
                    )
                location = str(component.get("location", "")).strip()
                description = str(component.get("description", "")).strip()
                title = title.replace("\\n", "\n")
                description = description.replace("\\n", "\n")
                location = location.replace("\\n", "\n")

                if title and start_datetime and end_datetime:
                    events.append({
                        "title": title,
                        "start_datetime": start_datetime,
                        "end_datetime": end_datetime,
                        "location": location,
                        "description": description,
                    })
            except Exception as e:
                logger.error(f"Error parsing event in .ics file: {str(e)}")
                continue
    return events