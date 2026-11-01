from logging import getLogger
from bs4 import BeautifulSoup
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
import firebase_admin
from firebase_admin import credentials, messaging

from content.models import AppUser, Article

logger = getLogger("push_notifications")


class FirebasePushService:
    """Firebase Admin SDK Service für Push-Notifications"""
    
    def __init__(self):
        self.app = None
        self.initialized = False
        self._initialize_firebase()
    
    def _initialize_firebase(self):
        """Initialisiert Firebase Admin SDK einmalig"""
        if self.initialized:
            return
            
        if not hasattr(settings, 'FIREBASE_SERVICE_ACCOUNT_KEY'):
            raise Exception("FIREBASE_SERVICE_ACCOUNT_KEY ist nicht in den Settings konfiguriert!")
        
        try:
            # Prüfen ob Firebase Admin bereits initialisiert ist
            try:
                self.app = firebase_admin.get_app()
                logger.info("Firebase Admin SDK bereits initialisiert")
            except ValueError:
                # App existiert noch nicht, neue erstellen
                service_account_key = settings.FIREBASE_SERVICE_ACCOUNT_KEY
                
                if isinstance(service_account_key, str):
                    cred = credentials.Certificate(service_account_key)
                elif isinstance(service_account_key, dict):
                    cred = credentials.Certificate(service_account_key)
                else:
                    raise Exception("FIREBASE_SERVICE_ACCOUNT_KEY muss Dateipfad oder Dictionary sein")
                
                self.app = firebase_admin.initialize_app(cred)
                logger.info("Firebase Admin SDK erfolgreich initialisiert")
            
            self.initialized = True
            
        except Exception as e:
            raise Exception(f"Fehler beim Initialisieren von Firebase Admin SDK: {str(e)}")
    
    def send_to_multiple(self, tokens, title, body, data=None):
        """
        Sendet Push-Notifications an multiple Devices mit Firebase Admin SDK
        
        Returns:
            dict: {"success": int, "failure": int, "invalid_tokens": list, "results": list}
        """
        if not tokens:
            return {"success": 0, "failure": 0, "invalid_tokens": [], "results": []}
        
        try:
            # Multicast Message erstellen
            message = messaging.MulticastMessage(
                notification=messaging.Notification(
                    title=title,
                    body=body
                ),
                tokens=tokens,
                data={str(k): str(v) for k, v in (data or {}).items()}
            )
            
            # Multicast senden
            response = messaging.send_multicast(message)
            
            # Ergebnisse analysieren
            invalid_tokens = []
            results = []
            
            for i, send_response in enumerate(response.responses):
                token = tokens[i]
                
                if send_response.success:
                    results.append({
                        "token": token[:20] + "...",
                        "success": True,
                        "message_id": send_response.message_id
                    })
                else:
                    # Fehler analysieren
                    error_code = "UNKNOWN"
                    should_remove_token = False
                    
                    if send_response.exception:
                        if isinstance(send_response.exception, messaging.UnregisteredError):
                            error_code = "UNREGISTERED"
                            should_remove_token = True
                        elif isinstance(send_response.exception, messaging.SenderIdMismatchError):
                            error_code = "SENDER_ID_MISMATCH"
                            should_remove_token = True
                        elif isinstance(send_response.exception, messaging.QuotaExceededError):
                            error_code = "QUOTA_EXCEEDED"
                    
                    results.append({
                        "token": token[:20] + "...",
                        "success": False,
                        "error": str(send_response.exception) if send_response.exception else "Unknown error",
                        "error_code": error_code
                    })
                    
                    if should_remove_token:
                        invalid_tokens.append(token)
            
            return {
                "success": response.success_count,
                "failure": response.failure_count,
                "invalid_tokens": invalid_tokens,
                "results": results
            }
            
        except Exception as e:
            logger.error(f"Multicast send failed: {str(e)}")
            return {
                "success": 0,
                "failure": len(tokens),
                "invalid_tokens": [],
                "error": str(e),
                "results": [{"token": token[:20] + "...", "success": False, "error": str(e)} for token in tokens]
            }
    
    def cleanup_invalid_tokens(self, invalid_tokens):
        """Entfernt ungültige Firebase-Tokens aus der Datenbank"""
        if not invalid_tokens:
            return 0
            
        removed_count = 0
        for token in invalid_tokens:
            try:
                users = AppUser.objects.filter(firebase_token=token)
                for user in users:
                    logger.info(f"Removing invalid firebase token for user {user.device_id}")
                    user.firebase_token = None
                    user.save()
                    removed_count += 1
            except Exception as e:
                logger.error(f"Error removing invalid token {token[:20]}...: {str(e)}")
        
        return removed_count


# Globaler Firebase Service
firebase_service = None


def get_firebase_service():
    """Initialisiert Firebase erst beim ersten Versand (nicht schon beim Import)."""
    global firebase_service
    if firebase_service is None:
        if not settings.FIREBASE_SERVICE_ACCOUNT_KEY:
            raise CommandError("FIREBASE_SERVICE_ACCOUNT_KEY ist nicht gesetzt.")
        firebase_service = FirebasePushService()
    return firebase_service


def query_push_articles():
    """Holt alle Artikel die für Push-Notifications vorgesehen sind"""
    articles = Article.objects.filter(push_notification_queued=True)
    return articles


def push_messages(push_queue):
    """Versendet Push-Notifications für Artikel in der Queue"""
    logger.info("Processing messages with Firebase Admin SDK")
    
    service = get_firebase_service()

    for article in push_queue:
        _do_push = all([
            article.push_notification_sent is False,
            article.push_notification_queued is True,
        ])

        if not _do_push:
            continue

        logger.debug(f"Processing article {article.id} for push notification")

        try:
            push_type = article.source.organization.type
            _filter = {"push_{}".format(push_type): True}
            firebase_users = AppUser.objects.filter(**_filter).exclude(firebase_token=None)
            
            # Firebase Tokens sammeln und basic validation
            registration_ids = list(
                set([
                    user.firebase_token.strip()
                    for user in firebase_users
                    if (user.firebase_token and len(user.firebase_token.strip()) > 25)
                ])
            )
            
            if not registration_ids:
                logger.info(f"No valid firebase tokens found for article {article.id}")
                continue

            logger.debug(f"Sending push to {len(registration_ids)} devices for article {article.id}")

            message_title = article.title
            # HTML Tags aus abstract entfernen
            message_body = BeautifulSoup(article.abstract, "html.parser").text
            data = {"article_id": str(article.id)}

            # Mit Firebase Admin SDK senden
            result = service.send_to_multiple(
                tokens=registration_ids,
                title=message_title,
                body=message_body,
                data=data
            )
            
            logger.info(f"Push result for article {article.id}: {result['success']} successful, {result['failure']} failed")
            
            # Ungültige Tokens aus Datenbank entfernen
            if result.get('invalid_tokens'):
                removed_count = service.cleanup_invalid_tokens(result['invalid_tokens'])
                logger.info(f"Removed {removed_count} invalid firebase tokens from database")

            # Artikel als gesendet markieren wenn mindestens eine Nachricht erfolgreich war
            if result['success'] > 0:
                article.push_notification_sent = True
                article.push_notification_queued = False
                article.save()
                logger.info(f"Successfully sent push notification for article {article.id}")
            else:
                logger.error(f"Failed to send push notification for article {article.id}: No successful sends")
                # Log detailed errors für Debugging
                for res in result.get('results', []):
                    if not res['success']:
                        logger.debug(f"Failed send: {res.get('error', 'Unknown')} ({res.get('error_code', 'UNKNOWN')})")

        except Exception as e:
            logger.error(f"Failed to send push notification for article {article.id}: {str(e)}")
            continue


def run():
    """Hauptfunktion für Push-Notification Verarbeitung"""
    push_queue = query_push_articles()
    if push_queue:
        logger.info(f"Found {len(push_queue)} articles to push")
        push_messages(push_queue)
    else:
        logger.info("No messages to send")


class Command(BaseCommand):
    help = "Sends push messages using Firebase Admin SDK"

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Führt einen Dry-Run aus ohne tatsächlich Push-Nachrichten zu senden'
        )
        parser.add_argument(
            '--cleanup-tokens',
            action='store_true',
            help='Testet alle Firebase Tokens und entfernt ungültige'
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        cleanup_tokens = options.get('cleanup_tokens', False)
        
        if not settings.ENABLE_PUSH and not dry_run:
            self.stdout.write("Push-Nachrichten sind deaktiviert (ENABLE_PUSH=false).")
            return

        if dry_run:
            self.stdout.write("🧪 Dry-Run Modus - keine Nachrichten werden gesendet")
            
        if cleanup_tokens:
            self.cleanup_invalid_tokens()
            return
            
        logger.info("Starting push message run with Firebase Admin SDK")
        try:
            if dry_run:
                self.dry_run()
            else:
                run()
            logger.info("Push message run completed")
        except Exception as e:
            logger.error(f"Push message run failed: {str(e)}")
            raise CommandError(f"Push message run failed: {str(e)}")
    
    def dry_run(self):
        """Dry-Run zeigt was gesendet werden würde ohne zu senden"""
        push_queue = query_push_articles()
        if not push_queue:
            self.stdout.write("Keine Artikel zum Senden gefunden")
            return
            
        self.stdout.write(f"📊 Würde Push-Notifications für {len(push_queue)} Artikel senden:")
        
        for article in push_queue:
            push_type = article.source.organization.type
            _filter = {"push_{}".format(push_type): True}
            firebase_users = AppUser.objects.filter(**_filter).exclude(firebase_token=None)
            
            valid_tokens = [
                user.firebase_token.strip()
                for user in firebase_users
                if (user.firebase_token and len(user.firebase_token.strip()) > 25)
            ]
            
            self.stdout.write(f"  📄 Artikel {article.id}: '{article.title[:50]}...'")
            self.stdout.write(f"     Push-Typ: {push_type}")
            self.stdout.write(f"     Empfänger: {len(valid_tokens)} Benutzer")
    
    def cleanup_invalid_tokens(self):
        """Testet alle Firebase Tokens und entfernt ungültige"""
        self.stdout.write("🧹 Teste alle Firebase Tokens...")
        
        users_with_tokens = AppUser.objects.exclude(firebase_token=None).exclude(firebase_token='')
        total_users = users_with_tokens.count()
        
        if total_users == 0:
            self.stdout.write("Keine Benutzer mit Firebase Tokens gefunden")
            return
            
        self.stdout.write(f"Teste {total_users} Firebase Tokens...")
        
        test_batches = []
        current_batch = []
        
        # Tokens in Batches von 500 aufteilen (Firebase Limit)
        for user in users_with_tokens:
            current_batch.append((user.device_id, user.firebase_token))
            if len(current_batch) >= 500:
                test_batches.append(current_batch)
                current_batch = []
        
        if current_batch:
            test_batches.append(current_batch)
        
        total_invalid = 0
        
        for i, batch in enumerate(test_batches):
            self.stdout.write(f"Teste Batch {i+1}/{len(test_batches)} ({len(batch)} Tokens)...")
            
            tokens = [token for _, token in batch]
            
            try:
                # Test-Message erstellen ohne zu senden
                test_result = get_firebase_service().send_to_multiple(
                    tokens=tokens,
                    title="Token Validation Test",
                    body="This is a test - message should not be sent",
                    data={"test": "validation"}
                )
                
                if test_result.get('invalid_tokens'):
                    batch_invalid = len(test_result['invalid_tokens'])
                    total_invalid += batch_invalid
                    self.stdout.write(f"   Gefunden: {batch_invalid} ungültige Tokens in diesem Batch")
                    
                    # Ungültige Tokens entfernen
                    removed = get_firebase_service().cleanup_invalid_tokens(test_result['invalid_tokens'])
                    self.stdout.write(f"   Entfernt: {removed} ungültige Tokens")
                
            except Exception as e:
                self.stdout.write(f"   ❌ Fehler beim Testen von Batch {i+1}: {str(e)}")
        
        self.stdout.write("✅ Token-Cleanup abgeschlossen")
        self.stdout.write(f"   Gesamt getestet: {total_users}")
        self.stdout.write(f"   Ungültige entfernt: {total_invalid}")


if __name__ == "__main__":
    c = Command()
    c.handle()