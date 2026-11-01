(function() {
    'use strict';

    document.addEventListener('DOMContentLoaded', function() {
        // --- Server-side search (debounced auto-submit) ---
        var searchInput = document.querySelector('.search-input');
        var searchForm = searchInput ? searchInput.closest('form') : null;

        if (searchInput && searchForm) {
            var debounceTimer;
            var lastSubmittedValue = searchInput.value;
            searchInput.addEventListener('input', function() {
                clearTimeout(debounceTimer);
                debounceTimer = setTimeout(function() {
                    if (searchInput.value !== lastSubmittedValue) {
                        lastSubmittedValue = searchInput.value;
                        // Reset pagination on new search
                        var pageInput = searchForm.querySelector('input[name="page"]');
                        if (pageInput) pageInput.remove();
                        searchForm.submit();
                    }
                }, 400);
            });
        }

        // --- Filter/Sort dropdowns ---
        document.querySelectorAll('.filter-dropdown').forEach(function(dropdown) {
            var btn = dropdown.querySelector('.filter-dropdown__btn');
            if (!btn) return;

            btn.addEventListener('click', function(e) {
                e.stopPropagation();
                // Close others
                document.querySelectorAll('.filter-dropdown.open').forEach(function(d) {
                    if (d !== dropdown) d.classList.remove('open');
                });
                dropdown.classList.toggle('open');
            });
        });

        // Close dropdowns on outside click
        document.addEventListener('click', function() {
            document.querySelectorAll('.filter-dropdown.open').forEach(function(d) {
                d.classList.remove('open');
            });
        });

        // --- Server-side filter/sort links ---
        document.querySelectorAll('.filter-dropdown__item[data-param]').forEach(function(item) {
            item.addEventListener('click', function() {
                var param = this.dataset.param;
                var value = this.dataset.value;
                var url = new URL(window.location.href);
                if (value) {
                    url.searchParams.set(param, value);
                } else {
                    url.searchParams.delete(param);
                }
                url.searchParams.delete('page');
                window.location.href = url.toString();
            });
        });
    });
})();
