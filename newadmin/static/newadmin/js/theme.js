(function() {
    'use strict';

    var html = document.documentElement;
    var stored = localStorage.getItem('newadmin-theme') || 'dark';
    html.setAttribute('data-theme', stored);

    window.toggleTheme = function() {
        var current = html.getAttribute('data-theme');
        var next = current === 'dark' ? 'light' : 'dark';
        html.setAttribute('data-theme', next);
        localStorage.setItem('newadmin-theme', next);

        // Update toggle icon
        var icon = document.querySelector('.theme-toggle-icon');
        if (icon) {
            icon.textContent = next === 'dark' ? '\u263E' : '\u2600';
        }
        var label = document.querySelector('.theme-toggle-label');
        if (label) {
            label.textContent = next === 'dark' ? 'Light Mode' : 'Dark Mode';
        }
    };

    // Set initial icon state after DOM loaded
    document.addEventListener('DOMContentLoaded', function() {
        var icon = document.querySelector('.theme-toggle-icon');
        if (icon) {
            icon.textContent = stored === 'dark' ? '\u263E' : '\u2600';
        }
        var label = document.querySelector('.theme-toggle-label');
        if (label) {
            label.textContent = stored === 'dark' ? 'Light Mode' : 'Dark Mode';
        }
    });
})();
