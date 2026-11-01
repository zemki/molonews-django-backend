from django.urls import resolve, Resolver404


class FlexibleSlashMiddleware:
    """
    Middleware that allows URLs to work both with and without trailing slashes.
    Unlike Django's APPEND_SLASH, this doesn't redirect but resolves both versions.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def _is_real_view(self, path):
        """Check if the path resolves to a real view (not the admin catch-all)."""
        try:
            match = resolve(path)
            qualname = getattr(match.func, '__qualname__', '')
            return 'catch_all_view' not in qualname
        except Resolver404:
            return False

    def __call__(self, request):
        # Try to resolve the current path
        if self._is_real_view(request.path_info):
            return self.get_response(request)

        # If path ends with slash, try without slash
        if request.path_info.endswith('/') and request.path_info != '/':
            new_path = request.path_info[:-1]
            if self._is_real_view(new_path):
                request.path = new_path
                request.path_info = new_path
                return self.get_response(request)

        # If path doesn't end with slash, try with slash
        elif not request.path_info.endswith('/'):
            new_path = request.path_info + '/'
            if self._is_real_view(new_path):
                request.path = new_path
                request.path_info = new_path
                return self.get_response(request)

        # Neither version resolves, let Django handle the 404
        return self.get_response(request)
