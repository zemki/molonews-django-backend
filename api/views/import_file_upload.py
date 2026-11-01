import secrets
import os
import shlex
import subprocess
import sys
from rest_framework import status, viewsets, serializers
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from django.conf import settings


# Bearer-Token für Upload und Import (settings.UPLOAD_AUTH_TOKEN, leer = gesperrt)
UPLOAD_AUTH_TOKEN = getattr(settings, 'UPLOAD_AUTH_TOKEN', '')
UPLOAD_DIR = os.path.join(str(settings.IMPORT_DATA_DIR), 'uploads')
PYTHON_PATH = sys.executable
MANAGE_PY_PATH = os.path.join(str(settings.BASE_DIR), 'manage.py')


def _get_lock_file_path(source_id):
    """Returns path of the lock file for a given source_id."""
    return os.path.join(UPLOAD_DIR, f'import_{source_id}.lock')


def _is_import_running(source_id):
    """
    Checks if an import for this source_id is already running.
    Uses a PID-based lock file. Returns True if still running, False if idle/stale.
    """
    lock_path = _get_lock_file_path(source_id)
    if not os.path.exists(lock_path):
        return False
    try:
        with open(lock_path, 'r') as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # Signal 0 = just check if process exists (no actual signal sent)
        return True  # Process still alive
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        # Stale lock file (process dead or PID invalid) - remove it
        try:
            os.remove(lock_path)
        except OSError:
            pass
        return False


class FileUploadSerializer(serializers.Serializer):
    file = serializers.FileField(required=True)

    class Meta:
        fields = ('file',)


class FileImportSerializer(serializers.Serializer):
    filename = serializers.CharField(required=True, help_text='Name of the file to import (e.g., events.json)')
    source_id = serializers.IntegerField(required=True, help_text='Source ID for import')

    class Meta:
        fields = ('filename', 'source_id')


class SourceDeleteSerializer(serializers.Serializer):
    source_id = serializers.IntegerField(required=True, help_text='Source ID to delete events for')

    class Meta:
        fields = ('source_id',)


class ImportFileUploadViewSet(viewsets.ViewSet):
    serializer_class = FileUploadSerializer
    parser_classes = [MultiPartParser, FormParser]
    http_method_names = ['post']

    def _validate_auth_token(self, request):
        """Validiert das Auth Token aus dem Authorization Header"""
        auth_header = request.headers.get('Authorization', '')
        
        if not auth_header.startswith('Bearer '):
            return False
            
        token = auth_header.replace('Bearer ', '')
        return bool(UPLOAD_AUTH_TOKEN) and secrets.compare_digest(token, UPLOAD_AUTH_TOKEN)

    def _ensure_upload_directory(self):
        """Stellt sicher, dass der Upload-Ordner existiert"""
        if not os.path.exists(UPLOAD_DIR):
            os.makedirs(UPLOAD_DIR, exist_ok=True)

    def _save_file(self, uploaded_file):
        """Speichert die hochgeladene Datei im Zielordner"""
        self._ensure_upload_directory()
        
        file_path = os.path.join(UPLOAD_DIR, uploaded_file.name)
        
        # Prüfe ob Datei bereits existiert und erstelle neuen Namen falls nötig
        #original_path = file_path
        #counter = 1
        #while os.path.exists(file_path):
        #    name, ext = os.path.splitext(original_path)
        #    file_path = f"{name}_{counter}{ext}"
        #    counter += 1
        
        # Speichere Datei
        with open(file_path, 'wb') as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)
        
        return file_path

    @swagger_auto_schema(
        operation_description='Upload a file to the import directory. Requires Bearer token authentication.',
        manual_parameters=[
            openapi.Parameter(
                'Authorization',
                openapi.IN_HEADER,
                description='Bearer token for authentication',
                type=openapi.TYPE_STRING,
                required=True,
                format='Bearer <token>'
            ),
            openapi.Parameter(
                'file',
                openapi.IN_FORM,
                description='File to upload',
                type=openapi.TYPE_FILE,
                required=True
            )
        ],
        responses={
            200: openapi.Response(
                description='File uploaded successfully',
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'message': openapi.Schema(type=openapi.TYPE_STRING),
                        'filename': openapi.Schema(type=openapi.TYPE_STRING),
                        'file_path': openapi.Schema(type=openapi.TYPE_STRING),
                        'file_size': openapi.Schema(type=openapi.TYPE_INTEGER)
                    }
                )
            ),
            400: openapi.Response(description='Bad request - invalid file or missing data'),
            401: openapi.Response(description='Unauthorized - invalid or missing token'),
            500: openapi.Response(description='Server error during file upload')
        }
    )
    def create(self, request, *args, **kwargs):
        """
        Upload eine Datei in den Import-Ordner
        
        Args:
            request: HTTP Request mit Datei und Authorization Header
            
        Returns:
            Response: JSON Response mit Upload-Status
        """
        
        # 1. Auth Token validieren
        if not self._validate_auth_token(request):
            return Response(
                {'error': 'Invalid or missing authorization token'}, 
                status=status.HTTP_401_UNAUTHORIZED
            )
        
        # 2. Datei validieren
        if 'file' not in request.FILES:
            return Response(
                {'error': 'No file provided'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        uploaded_file = request.FILES['file']
        
        # Serializer für zusätzliche Validierung
        serializer = self.serializer_class(data={'file': uploaded_file})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # 3. Datei speichern
            file_path = self._save_file(uploaded_file)
            
            # 4. Erfolgreiche Antwort
            return Response({
                'message': 'File uploaded successfully',
                'filename': uploaded_file.name,
                'file_path': file_path,
                'file_size': uploaded_file.size
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            return Response(
                {'error': f'Failed to save file: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )



class ImportFileViewSet(viewsets.ViewSet):
    serializer_class = FileImportSerializer
    parser_classes = [JSONParser]
    http_method_names = ['post']

    def _validate_auth_token(self, request):
        """Validiert das Auth Token aus dem Authorization Header"""
        auth_header = request.headers.get('Authorization', '')

        if not auth_header.startswith('Bearer '):
            return False

        token = auth_header.replace('Bearer ', '')
        return bool(UPLOAD_AUTH_TOKEN) and secrets.compare_digest(token, UPLOAD_AUTH_TOKEN)

    @swagger_auto_schema(
        operation_description='Import a previously uploaded file into the database asynchronously. The import runs in the background and the endpoint returns immediately with 202 Accepted. Check the log file for import progress. Requires Bearer token authentication.',
        operation_id='import_import_file',
        request_body=FileImportSerializer,
        responses={
            202: openapi.Response(
                description='Import started successfully in background',
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'message': openapi.Schema(type=openapi.TYPE_STRING, description='Success message'),
                        'status': openapi.Schema(type=openapi.TYPE_STRING, description='Import status (processing)'),
                        'filename': openapi.Schema(type=openapi.TYPE_STRING, description='Name of imported file'),
                        'source_id': openapi.Schema(type=openapi.TYPE_INTEGER, description='Source ID used for import'),
                        'command': openapi.Schema(type=openapi.TYPE_STRING, description='Import command executed'),
                        'log_file': openapi.Schema(type=openapi.TYPE_STRING, description='Path to log file for monitoring import progress'),
                        'process_id': openapi.Schema(type=openapi.TYPE_INTEGER, description='Process ID of background import')
                    }
                )
            ),
            400: openapi.Response(description='Bad request - invalid filename or missing data'),
            401: openapi.Response(description='Unauthorized - invalid or missing token'),
            404: openapi.Response(description='File not found'),
            500: openapi.Response(description='Server error during import startup')
        }
    )
    def create(self, request):
        """
        Import a previously uploaded file into the database

        Args:
            request: HTTP Request with filename and optional source_id, and Authorization Header

        Returns:
            Response: JSON Response with import status
        """

        # 1. Auth Token validieren
        if not self._validate_auth_token(request):
            return Response(
                {'error': 'Invalid or missing authorization token'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # 2. Request Daten validieren
        serializer = FileImportSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        # Nur Dateinamen im Upload-Ordner zulassen (kein Pfad)
        filename = os.path.basename(serializer.validated_data['filename'])
        source_id = serializer.validated_data['source_id']

        # 3. Prüfe ob Datei existiert
        file_path = os.path.join(UPLOAD_DIR, filename)
        if not os.path.exists(file_path):
            return Response(
                {'error': f'File not found: {filename}'},
                status=status.HTTP_404_NOT_FOUND
            )

        # 4. Prüfen ob bereits ein Import für diese Source läuft (Concurrency-Schutz)
        if _is_import_running(source_id):
            return Response(
                {
                    'error': f'Import for source_id {source_id} is already running. '
                             f'Please wait for it to finish before starting a new import.',
                    'status': 'already_running',
                    'source_id': source_id,
                    'lock_file': _get_lock_file_path(source_id),
                },
                status=status.HTTP_409_CONFLICT
            )

        try:
            # 5. Django Management Command im Hintergrund starten (asynchron)
            # Log-Datei für den Import-Prozess erstellen
            log_file_path = os.path.join(UPLOAD_DIR, f'import_{filename}.log')

            # Lock-File Pfad bestimmen
            lock_path = _get_lock_file_path(source_id)

            # Shell-Wrapper: Management Command ausführen, danach Lock-File automatisch löschen
            shell_command = (
                f'{shlex.quote(PYTHON_PATH)} {shlex.quote(MANAGE_PY_PATH)} import_events_json '
                f'{shlex.quote(file_path)} {int(source_id)}'
                f' >> {shlex.quote(log_file_path)} 2>&1'
                f'; rm -f {shlex.quote(lock_path)}'
            )

            # Command im Hintergrund starten (non-blocking)
            process = subprocess.Popen(
                ['bash', '-c', shell_command],
                start_new_session=True  # Startet Prozess in neuer Session (läuft auch nach Request-Ende weiter)
            )

            # Lock-File mit PID anlegen (verhindert parallele Imports für dieselbe Source)
            with open(lock_path, 'w') as lf:
                lf.write(str(process.pid))

            # 6. Sofort mit 202 Accepted antworten (Import läuft im Hintergrund)
            return Response({
                'message': 'Import started successfully in background',
                'status': 'processing',
                'filename': filename,
                'source_id': source_id,
                'command': shell_command,
                'log_file': log_file_path,
                'process_id': process.pid
            }, status=status.HTTP_202_ACCEPTED)

        except Exception as e:
            # Lock-File aufräumen falls Prozessstart fehlschlug
            try:
                lock_path = _get_lock_file_path(source_id)
                if os.path.exists(lock_path):
                    os.remove(lock_path)
            except OSError:
                pass
            return Response(
                {'error': f'Failed to start import: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )



class ImportStatusViewSet(viewsets.ViewSet):
    """
    ViewSet zum Prüfen des Import-Status
    """
    parser_classes = [JSONParser]
    http_method_names = ['get']

    def _validate_auth_token(self, request):
        """Validiert das Auth Token aus dem Authorization Header"""
        auth_header = request.headers.get('Authorization', '')

        if not auth_header.startswith('Bearer '):
            return False

        token = auth_header.replace('Bearer ', '')
        return bool(UPLOAD_AUTH_TOKEN) and secrets.compare_digest(token, UPLOAD_AUTH_TOKEN)

    @swagger_auto_schema(
        operation_description='Check the status of an import process by reading its log file. Requires Bearer token authentication.',
        operation_id='import_check_status',
        manual_parameters=[
            openapi.Parameter(
                'filename',
                openapi.IN_QUERY,
                description='Filename to check import status for',
                type=openapi.TYPE_STRING,
                required=True
            ),
            openapi.Parameter(
                'Authorization',
                openapi.IN_HEADER,
                description='Bearer token for authentication',
                type=openapi.TYPE_STRING,
                required=True
            )
        ],
        responses={
            200: openapi.Response(
                description='Import status retrieved successfully',
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'filename': openapi.Schema(type=openapi.TYPE_STRING),
                        'log_file': openapi.Schema(type=openapi.TYPE_STRING),
                        'log_exists': openapi.Schema(type=openapi.TYPE_BOOLEAN),
                        'log_content': openapi.Schema(type=openapi.TYPE_STRING, description='Last 50 lines of log')
                    }
                )
            ),
            400: openapi.Response(description='Bad request - missing filename'),
            401: openapi.Response(description='Unauthorized - invalid or missing token'),
            404: openapi.Response(description='Log file not found')
        }
    )
    def list(self, request):
        """
        Prüft den Status eines Import-Prozesses
        """
        # 1. Auth Token validieren
        if not self._validate_auth_token(request):
            return Response(
                {'error': 'Invalid or missing authorization token'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # 2. Filename aus Query-Parametern holen
        filename = request.query_params.get('filename')
        if not filename:
            return Response(
                {'error': 'Filename parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 3. Log-Datei prüfen (nur Dateiname, kein Pfad)
        filename = os.path.basename(filename)
        log_file_path = os.path.join(UPLOAD_DIR, f'import_{filename}.log')

        if not os.path.exists(log_file_path):
            return Response(
                {
                    'filename': filename,
                    'log_file': log_file_path,
                    'log_exists': False,
                    'message': 'No import log found for this file'
                },
                status=status.HTTP_404_NOT_FOUND
            )

        try:
            # Letzte 50 Zeilen der Log-Datei lesen
            with open(log_file_path, 'r') as log_file:
                lines = log_file.readlines()
                last_lines = ''.join(lines[-50:]) if len(lines) > 50 else ''.join(lines)

            return Response({
                'filename': filename,
                'log_file': log_file_path,
                'log_exists': True,
                'log_size': os.path.getsize(log_file_path),
                'log_content': last_lines
            }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {'error': f'Failed to read log file: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class DeleteEventsBySourceViewSet(viewsets.ViewSet):
    serializer_class = SourceDeleteSerializer
    parser_classes = [JSONParser]
    http_method_names = ['post']

    def _validate_auth_token(self, request):
        """Validiert das Auth Token aus dem Authorization Header"""
        auth_header = request.headers.get('Authorization', '')

        if not auth_header.startswith('Bearer '):
            return False

        token = auth_header.replace('Bearer ', '')
        return bool(UPLOAD_AUTH_TOKEN) and secrets.compare_digest(token, UPLOAD_AUTH_TOKEN)

    @swagger_auto_schema(
        operation_description='Delete all events by a specific source ID. Requires Bearer token authentication.',
        operation_id='import_delete_events_by_source',
        request_body=SourceDeleteSerializer,
        responses={
            200: openapi.Response(
                description='Events deleted successfully',
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'message': openapi.Schema(type=openapi.TYPE_STRING),
                        'source_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'command': openapi.Schema(type=openapi.TYPE_STRING),
                        'output': openapi.Schema(type=openapi.TYPE_STRING),
                        'return_code': openapi.Schema(type=openapi.TYPE_INTEGER)
                    }
                )
            ),
            400: openapi.Response(description='Bad request - invalid source_id'),
            401: openapi.Response(description='Unauthorized - invalid or missing token'),
            500: openapi.Response(description='Server error during deletion')
        }
    )
    def create(self, request):
        """
        Delete all events by a specific source ID

        Args:
            request: HTTP Request with source_id and Authorization Header

        Returns:
            Response: JSON Response with deletion status
        """

        # 1. Auth Token validieren
        if not self._validate_auth_token(request):
            return Response(
                {'error': 'Invalid or missing authorization token'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # 2. Request Daten validieren
        serializer = SourceDeleteSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        source_id = serializer.validated_data['source_id']

        try:
            # 3. Django Management Command ausführen
            command = [
                PYTHON_PATH,
                MANAGE_PY_PATH,
                'delete_events_by_source',
                str(source_id)
            ]

            # Command ausführen mit auto-confirm
            result = subprocess.run(
                command,
                input='j\n',  # Auto-confirm the deletion
                capture_output=True,
                text=True,
                timeout=300  # 5 Minuten Timeout
            )

            # 4. Antwort zurückgeben
            if result.returncode == 0:
                return Response({
                    'message': 'Events deleted successfully',
                    'source_id': source_id,
                    'command': ' '.join(command),
                    'output': result.stdout,
                    'return_code': result.returncode
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    'error': 'Delete command failed',
                    'source_id': source_id,
                    'command': ' '.join(command),
                    'stdout': result.stdout,
                    'stderr': result.stderr,
                    'return_code': result.returncode
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        except subprocess.TimeoutExpired:
            return Response(
                {'error': 'Delete command timed out (exceeded 5 minutes)'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        except Exception as e:
            return Response(
                {'error': f'Failed to execute delete: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )