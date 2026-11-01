# Privacy and data handling

This page describes which personal data the molo backend processes, so that
operators can write an accurate privacy policy and record of processing
activities (GDPR Art. 30). It is not legal advice.

## App users (no account)

The apps do not require an account. Each installation is identified by a random
device ID sent in the `X-Device-ID` header. For this pseudonymous profile
(`AppUser`) the backend stores:

| Data | Purpose |
|---|---|
| Device ID, date of first use | Recognize the installation |
| Selected areas, organizations, topics, sort order | Personalized feed |
| Bookmarked and archived articles/events | Lists in the app |
| Location (latitude, longitude, radius), if the user enables "nearby" | Events near the user |
| Push token and push preferences, if push is enabled | Push notifications |

There is no link to names, e-mail addresses or advertising IDs. Operators should
define a retention period for inactive profiles and delete them regularly.

## Editorial and contributor accounts

`User` accounts (newadmin / admin) store user name, e-mail address, optional
display name, phone number and profile image, assigned sources and areas,
permissions, and hashed passwords. Activation and password-reset tokens are
stored temporarily.

## Contact, feedback and reports

Contact and feedback forms are forwarded by e-mail to `FEEDBACK_MAIL_TARGET`
together with the sender's e-mail address. Reports of problematic articles
contain the article only, not the reporting device.

## AI features

All language model requests go to the Ollama server configured in
`LLM_BASE_URL`, which you operate yourself. Search queries are processed to
answer the request and are not stored by the backend. Optional voice input is
transcribed locally with faster-whisper; the audio is held in a temporary file
only for the duration of the request.

## Logs

Application logs (`LOG_DIR`) contain technical information such as errors,
import results and IP addresses of failed API key attempts. They do not contain
request bodies or authentication tokens. Configure log rotation and a retention
period that fits your policy. The QR redirect (`/api/v4/redirect/`) does not log.

## Third-party services

| Service | When | Data |
|---|---|---|
| OpenStreetMap Nominatim | Geocoding of event addresses and areas | Addresses of events/venues (no user data) |
| Firebase Cloud Messaging (Google) | Only with `ENABLE_PUSH=true` | Push tokens and notification content |
| Your SMTP provider | Account e-mails, alerts, feedback | E-mail addresses and message content |
| Feed and website sources | Imports | No user data; requests come from the server |

If push notifications are enabled, Firebase must be listed as a processor in your
privacy policy. Leave `ENABLE_PUSH=false` if this is not acceptable.
