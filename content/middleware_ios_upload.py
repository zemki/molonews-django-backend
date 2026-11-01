from django.utils.deprecation import MiddlewareMixin
from django.http import HttpResponse
import logging

logger = logging.getLogger("molonews")


class iOSUploadFixMiddleware(MiddlewareMixin):
    """
    Behebt iOS Resumable Upload Probleme.
    iOS 17+ sendet Upload-Complete/Upload-Incomplete Header die Django nicht versteht.
    Diese Header verwenden RFC 8941 Structured Fields Format (?0, ?1) die Django nicht parsen kann.

    ZUSAETZLICH: iOS sendet Transfer-Encoding: chunked ZUSAMMEN mit Content-Length,
    was gegen HTTP/1.1 RFC 7230 verstoesst und Django verwirrt.
    """

    def process_request(self, request):
        path = request.path

        # Prüfe auf iOS Resumable Upload Header (RFC 8941 format: ?0 = false, ?1 = true)
        upload_incomplete = request.META.get('HTTP_UPLOAD_INCOMPLETE')
        upload_complete = request.META.get('HTTP_UPLOAD_COMPLETE')

        # Wenn Upload explizit als unvollständig markiert ist (?1 = true für incomplete)
        if upload_incomplete == '?1':
            logger.warning(f"Incomplete iOS upload rejected: {path}")
            return HttpResponse(
                "Resumable uploads not supported",
                status=501  # Not Implemented
            )

        # Wenn Upload-Complete auf ?0 (false) gesetzt ist, ist der Upload auch unvollständig
        if upload_complete == '?0':
            logger.warning(f"Incomplete iOS upload rejected (Upload-Complete=?0): {path}")
            return HttpResponse(
                "Resumable uploads not supported",
                status=501  # Not Implemented
            )

        # Transfer-Encoding Header Problem beheben
        # iOS 17+ sendet Transfer-Encoding: chunked UND Content-Length gleichzeitig,
        # was gegen HTTP/1.1 RFC 7230 verstößt und Django verwirrt.
        transfer_encoding = request.META.get('HTTP_TRANSFER_ENCODING', '').lower()
        content_length = request.META.get('CONTENT_LENGTH')

        if 'chunked' in transfer_encoding and content_length:
            request.META.pop('HTTP_TRANSFER_ENCODING', None)

        # Alle problematischen iOS Upload-Header aus META entfernen
        # Diese Header können Django/DRF Parser verwirren
        problematic_headers = [
            'HTTP_UPLOAD_INCOMPLETE',
            'HTTP_UPLOAD_COMPLETE',
            'HTTP_UPLOAD_DRAFT_INTEROP_VERSION',
            'HTTP_UPLOAD_OFFSET',
            'HTTP_UPLOAD_LENGTH',
        ]
        for header in problematic_headers:
            request.META.pop(header, None)

        return None
