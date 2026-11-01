(function($) {
    $(document).ready(function() {
        // Function to toggle area field based on checkbox state
        function toggleAreaField() {
            var autoDetectCheckbox = $('#id_auto_detect_area_from_content');
            var areaField = $('#id_area_to, #id_area_from');

            // If using django-filter-horizontal or similar, target the container
            var areaContainer = $('.field-area');

            if (autoDetectCheckbox.is(':checked')) {
                // Disable the area field and clear selections
                areaField.prop('disabled', true);

                // Clear all selected options in the select field
                $('#id_area_to option').each(function() {
                    $(this).remove().appendTo('#id_area_from');
                });

                // Sort the options in the "from" field
                var options = $('#id_area_from option');
                options.sort(function(a, b) {
                    return $(a).text().localeCompare($(b).text());
                });
                $('#id_area_from').empty().append(options);

                // Add visual indicator that field is disabled
                areaContainer.css('opacity', '0.5');
                areaContainer.css('pointer-events', 'none');
            } else {
                // Enable the area field
                areaField.prop('disabled', false);

                // Remove visual indicator
                areaContainer.css('opacity', '1');
                areaContainer.css('pointer-events', 'auto');
            }
        }

        // Initialize on page load
        toggleAreaField();

        // Add event listener for checkbox changes
        $('#id_auto_detect_area_from_content').on('change', function() {
            toggleAreaField();
        });

        // === Script-Felder nur bei ai-article anzeigen ===
        function toggleScriptFields() {
            var show = $('#id_type').val() === 'ai-article';
            $('.field-generate_script_button').toggle(show);
            $('.field-clear_import_script_button').toggle(show);
            $('.field-import_script').toggle(show);
            $('.field-result_link').toggle(show);
            $('.field-display_errors').toggle(show);
        }

        toggleScriptFields();
        $('#id_type').on('change', function() {
            toggleScriptFields();
        });
    });
})(django.jQuery || jQuery);
