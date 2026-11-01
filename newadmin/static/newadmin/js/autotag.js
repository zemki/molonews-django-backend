(function() {
    'use strict';

    document.addEventListener('DOMContentLoaded', function() {
        var btn = document.getElementById('autotag-btn');
        if (!btn) return;

        var apiUrl = btn.dataset.apiUrl;
        var titleSelector = btn.dataset.titleField || '.form-input--title';
        var contentFieldName = btn.dataset.contentField || 'abstract';
        var csrfToken = document.querySelector('[name=csrfmiddlewaretoken]');

        btn.addEventListener('click', function(e) {
            e.preventDefault();

            // Get title
            var titleEl = document.querySelector(titleSelector);
            var title = titleEl ? titleEl.value.trim() : '';

            // Get content - try multiple methods for CKEditor 5 compatibility
            var contentText = getCKEditorContent(contentFieldName);

            if (!title && !contentText) {
                showAutotagMessage('Bitte Titel und Inhalt eingeben.', 'warning');
                return;
            }

            // API needs at least title + abstract, use title as fallback
            if (!contentText) contentText = title;
            if (!title) title = contentText.substring(0, 200);

            // Show loading state
            btn.disabled = true;
            var originalHTML = btn.innerHTML;
            btn.innerHTML = '<svg class="autotag-spinner" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v4m0 12v4m-7.07-3.93l2.83-2.83m8.49-8.49l2.83-2.83M2 12h4m12 0h4M4.93 4.93l2.83 2.83m8.49 8.49l2.83 2.83"/></svg> Ermittle Tags...';

            // Build FormData - both endpoints expect 'title' and 'abstract'
            var formData = new FormData();
            formData.append('title', title);
            formData.append('abstract', contentText);

            fetch(apiUrl, {
                method: 'POST',
                body: formData,
                headers: {
                    'X-CSRFToken': csrfToken ? csrfToken.value : '',
                },
                credentials: 'same-origin',
            })
            .then(function(response) {
                if (response.status === 204) {
                    return [];
                }
                if (!response.ok) {
                    throw new Error('HTTP ' + response.status);
                }
                return response.json();
            })
            .then(function(data) {
                btn.disabled = false;
                btn.innerHTML = originalHTML;

                if (!data || !data.length) {
                    showAutotagMessage('Keine passenden Tags gefunden.', 'info');
                    return;
                }

                // data = [{"id": 5}, {"id": 12}, ...] — dedupe defensively
                var seen = {};
                var tagIds = [];
                data.forEach(function(t) {
                    var id = String(t.id);
                    if (!seen[id]) { seen[id] = true; tagIds.push(id); }
                });
                var count = 0;

                var tagsSelect = document.querySelector('select[name="tags"]');
                if (tagsSelect && tagsSelect.tomselect) {
                    var ts = tagsSelect.tomselect;
                    tagIds.forEach(function(id) {
                        if (ts.options[id] && ts.items.indexOf(id) === -1) {
                            ts.addItem(id, /* silent= */ true);
                            count++;
                        }
                    });
                } else if (tagsSelect) {
                    // Plain <select multiple> fallback
                    tagIds.forEach(function(id) {
                        var opt = tagsSelect.querySelector('option[value="' + id + '"]');
                        if (opt && !opt.selected) {
                            opt.selected = true;
                            count++;
                        }
                    });
                    tagsSelect.dispatchEvent(new Event('change', { bubbles: true }));
                } else {
                    // Checkbox fallback
                    var checkboxes = document.querySelectorAll('input[type="checkbox"][name="tags"]');
                    checkboxes.forEach(function(cb) {
                        if (tagIds.indexOf(cb.value) !== -1 && !cb.checked) {
                            cb.checked = true;
                            count++;
                        }
                    });
                }

                if (count > 0) {
                    showAutotagMessage(count + (count !== 1 ? ' Tags' : ' Tag') + ' automatisch gesetzt.', 'success');
                } else {
                    showAutotagMessage('Tags ermittelt, aber bereits alle gesetzt oder keine passenden Optionen gefunden.', 'info');
                }
            })
            .catch(function(err) {
                btn.disabled = false;
                btn.innerHTML = originalHTML;
                showAutotagMessage('Fehler: ' + err.message, 'error');
            });
        });

        function getCKEditorContent(fieldName) {
            // Method 1: CKEditor 5 instance via .ckeditorInstance on the editable element
            var editable = document.querySelector('.ck-editor__editable');
            if (editable && editable.ckeditorInstance) {
                return stripHtml(editable.ckeditorInstance.getData());
            }

            // Method 2: Read innerHTML from CKEditor editable area
            if (editable) {
                return stripHtml(editable.innerHTML);
            }

            // Method 3: Read the hidden textarea that CKEditor syncs with
            var textarea = document.querySelector('textarea[name="' + fieldName + '"]');
            if (textarea && textarea.value) {
                return stripHtml(textarea.value);
            }

            // Method 4: Look for any div with ck-editor data attribute
            var ckWidget = document.querySelector('[data-ck-widget]');
            if (ckWidget) {
                return stripHtml(ckWidget.innerHTML);
            }

            return '';
        }

        function stripHtml(html) {
            if (!html) return '';
            var tmp = document.createElement('div');
            tmp.innerHTML = html;
            return (tmp.textContent || tmp.innerText || '').trim();
        }

        function showAutotagMessage(text, type) {
            // Remove existing
            var existing = document.querySelector('.autotag-msg');
            if (existing) existing.remove();

            var msg = document.createElement('div');
            msg.className = 'autotag-msg autotag-msg--' + type;
            msg.textContent = text;

            var wrapper = btn.closest('.autotag-wrapper');
            if (wrapper) {
                wrapper.appendChild(msg);
            } else {
                btn.parentNode.insertBefore(msg, btn.nextSibling);
            }

            setTimeout(function() {
                msg.style.opacity = '0';
                setTimeout(function() { msg.remove(); }, 300);
            }, 4000);
        }
    });
})();
