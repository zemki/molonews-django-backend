/**
 * Tom Select initialization for all multi-select fields in newadmin.
 * Automatically transforms <select multiple class="tom-select-multi"> elements
 * into searchable tag-input widgets with a search field inside the dropdown.
 */
document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('select.tom-select-multi').forEach(function (el) {
        var maxRaw = el.dataset.maxItems;
        var maxItems = maxRaw ? parseInt(maxRaw, 10) : null;
        if (isNaN(maxItems)) maxItems = null;

        var ts = new TomSelect(el, {
            plugins: ['remove_button', 'dropdown_input'],
            maxOptions: null,
            maxItems: maxItems,
            placeholder: 'Klicken zum Auswaehlen...',
            hidePlaceholder: true,
            closeAfterSelect: false,
            controlInput: null,
            render: {
                no_results: function () {
                    return '<div class="no-results">Kein Ergebnis</div>';
                }
            }
        });

        // Show a hint when the user tries to exceed the limit.
        if (maxItems) {
            var hintEl = el.parentNode && el.parentNode.querySelector('.tom-select-max-hint');
            var hideTimer = null;
            ts.on('item_add', function () {
                if (!hintEl) return;
                if (ts.items.length >= maxItems) {
                    hintEl.classList.add('tom-select-max-hint--active');
                }
            });
            ts.on('item_remove', function () {
                if (!hintEl) return;
                if (ts.items.length < maxItems) {
                    hintEl.classList.remove('tom-select-max-hint--active');
                }
            });
            // Tom Select triggers no event when a user clicks a locked option,
            // so briefly flash the hint on clicks that try to open the dropdown.
            // Skip clicks on the remove (x) button — those reduce the count and
            // would otherwise cause a red-flash-then-clear glitch.
            ts.control.addEventListener('mousedown', function (ev) {
                if (!hintEl) return;
                if (ev.target.closest('.remove')) return;
                if (ts.items.length >= maxItems) {
                    hintEl.classList.add('tom-select-max-hint--flash');
                    clearTimeout(hideTimer);
                    hideTimer = setTimeout(function () {
                        hintEl.classList.remove('tom-select-max-hint--flash');
                    }, 1200);
                }
            });
            if (ts.items.length >= maxItems && hintEl) {
                hintEl.classList.add('tom-select-max-hint--active');
            }
        }
    });
});
