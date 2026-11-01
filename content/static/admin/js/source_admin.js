document.addEventListener('DOMContentLoaded', function() {

    // === Area-Feld toggle bei auto_detect_area ===
    var autoDetect = document.getElementById('id_auto_detect_area_from_content');
    if (autoDetect) {
        function toggleAreaField() {
            var areaRow = document.querySelector('.field-area');
            if (!areaRow) return;
            if (autoDetect.checked) {
                areaRow.style.opacity = '0.5';
                areaRow.style.pointerEvents = 'none';
            } else {
                areaRow.style.opacity = '1';
                areaRow.style.pointerEvents = 'auto';
            }
        }
        toggleAreaField();
        autoDetect.addEventListener('change', toggleAreaField);
    }

    // === Script-Felder nur bei Quellentypen mit Import-Script anzeigen ===
    var typeSelect = document.getElementById('id_type');
    if (!typeSelect) return;

    var scriptTypes = ['ai-article', 'ai-event', 'html-event'];
    var hideSelectors = [
        '.field-script_actions',
        '.field-script_review',
        '.field-script_approve_button',
        '.field-import_script',
        '.field-result_link'
    ];

    function toggleScriptFields() {
        var show = scriptTypes.indexOf(typeSelect.value) !== -1;
        hideSelectors.forEach(function(sel) {
            var row = document.querySelector(sel);
            if (row) {
                row.style.display = show ? '' : 'none';
            }
        });
    }

    toggleScriptFields();

    // Native change event (funktioniert ohne Jet)
    typeSelect.addEventListener('change', toggleScriptFields);

    // Polling als Fallback für Jet/Select2 Custom-Widgets
    var lastType = typeSelect.value;
    setInterval(function() {
        if (typeSelect.value !== lastType) {
            lastType = typeSelect.value;
            toggleScriptFields();
        }
    }, 300);
});
