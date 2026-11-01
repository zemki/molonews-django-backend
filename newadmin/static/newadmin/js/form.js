(function() {
    'use strict';

    document.addEventListener('DOMContentLoaded', function() {
        // --- Image preview on file select ---
        document.querySelectorAll('input[type="file"]').forEach(function(input) {
            input.addEventListener('change', function() {
                var group = this.closest('.form-group');
                if (!group) return;

                var preview = group.querySelector('.image-preview--new');
                if (!preview) {
                    preview = document.createElement('div');
                    preview.className = 'image-preview image-preview--new';
                    this.parentNode.insertBefore(preview, this.nextSibling);
                }

                if (this.files && this.files[0]) {
                    var file = this.files[0];

                    // Validate size (5MB)
                    if (file.size > 5 * 1024 * 1024) {
                        preview.innerHTML = '<span class="field-error">Das Bild ist zu gross (max. 5 MB).</span>';
                        return;
                    }

                    var reader = new FileReader();
                    reader.onload = function(e) {
                        preview.innerHTML = '<img src="' + e.target.result + '" alt="Vorschau">';
                    };
                    reader.readAsDataURL(file);
                } else {
                    preview.innerHTML = '';
                }
            });
        });

        // --- Restyle Django ClearableFileInput ---
        document.querySelectorAll('input.form-file').forEach(function(fileInput) {
            var parent = fileInput.parentNode;
            var clearCheckbox = parent.querySelector('input[type="checkbox"][name$="-clear"]');
            if (!clearCheckbox) return;

            var clearLabel = parent.querySelector('label[for="' + clearCheckbox.id + '"]');
            if (!clearLabel) return;

            var currentLink = parent.querySelector('a');

            // Collect Django-generated nodes to remove (text nodes, link, checkbox, label, br)
            var toRemove = [];
            var nodes = parent.childNodes;
            for (var i = 0; i < nodes.length; i++) {
                var node = nodes[i];
                if (node === fileInput || node === clearCheckbox || node === clearLabel || node === currentLink) {
                    toRemove.push(node);
                } else if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
                    toRemove.push(node);
                } else if (node.tagName === 'BR') {
                    toRemove.push(node);
                }
            }

            // Build new wrapper
            var wrapper = document.createElement('div');
            wrapper.className = 'form-file-wrapper';

            if (currentLink) {
                var currentRow = document.createElement('div');
                currentRow.className = 'file-current';
                currentRow.innerHTML = 'Aktuell: ';
                var fileName = currentLink.textContent.split('/').pop();
                currentLink.textContent = fileName;
                currentRow.appendChild(currentLink);

                var clearWrap = document.createElement('span');
                clearWrap.className = 'file-clear';
                clearWrap.appendChild(clearCheckbox);
                clearLabel.textContent = 'Entfernen';
                clearWrap.appendChild(clearLabel);
                currentRow.appendChild(clearWrap);

                wrapper.appendChild(currentRow);
            }

            wrapper.appendChild(fileInput);

            // Remove old Django nodes
            toRemove.forEach(function(n) {
                if (n.parentNode === parent) parent.removeChild(n);
            });

            // Insert wrapper at the position where the file input was
            parent.insertBefore(wrapper, parent.querySelector('.image-preview--new'));
        });

        // --- Auto-resize title textarea ---
        var titleField = document.querySelector('.form-input--title');
        if (titleField && titleField.tagName === 'TEXTAREA') {
            function autoResize() {
                titleField.style.height = 'auto';
                titleField.style.height = titleField.scrollHeight + 'px';
            }
            titleField.addEventListener('input', autoResize);
            autoResize();
        }

        // --- "Auswahl entfernen" für TomSelect-Multifelder (z. B. Gebiete) ---
        // Delegated handler: funktioniert auch wenn der Button erst nachträglich
        // im DOM landet. Sucht das passende <select> entweder via
        // data-clear-select="<id>" oder über das umschließende .form-group.
        document.addEventListener('click', function(ev) {
            var btn = ev.target.closest('.area-clear-btn, [data-clear-select]');
            if (!btn) return;
            ev.preventDefault();

            var sel = null;
            var targetId = btn.getAttribute('data-clear-select');
            if (targetId) sel = document.getElementById(targetId);
            if (!sel) {
                var group = btn.closest('.form-group');
                if (group) sel = group.querySelector('select');
            }
            if (!sel) return;

            if (sel.tomselect && typeof sel.tomselect.clear === 'function') {
                sel.tomselect.clear();
            } else {
                Array.prototype.forEach.call(sel.options, function(o) { o.selected = false; });
            }
            sel.dispatchEvent(new Event('change', { bubbles: true }));
        });

        // --- Custom validation message for required tags field ---
        var tagsSelect = document.querySelector('select[name="tags"]');
        if (tagsSelect) {
            tagsSelect.addEventListener('invalid', function() {
                this.setCustomValidity('Bitte wähle ein Thema für deinen Beitrag.');
            });
            tagsSelect.addEventListener('change', function() {
                this.setCustomValidity('');
            });
        }

        // --- Confirm before leaving with unsaved changes ---
        var form = document.querySelector('.article-form');
        if (form) {
            var formChanged = false;
            var pendingHref = null;

            form.addEventListener('input', function() { formChanged = true; });
            form.addEventListener('change', function() { formChanged = true; });
            form.addEventListener('submit', function() { formChanged = false; });

            // Build modal
            var modal = document.createElement('div');
            modal.className = 'unsaved-modal';
            modal.innerHTML =
                '<div class="unsaved-modal__backdrop"></div>' +
                '<div class="unsaved-modal__dialog" role="dialog" aria-modal="true" aria-labelledby="unsaved-modal-title">' +
                    '<h2 id="unsaved-modal-title" class="unsaved-modal__title">Ungespeicherte Änderungen</h2>' +
                    '<p class="unsaved-modal__text">Sie haben Änderungen gemacht. Sollen diese verworfen werden?</p>' +
                    '<div class="unsaved-modal__actions">' +
                        '<button type="button" class="btn btn--secondary unsaved-modal__cancel">Nein</button>' +
                        '<button type="button" class="btn btn--danger unsaved-modal__confirm">Ja</button>' +
                    '</div>' +
                '</div>';
            document.body.appendChild(modal);

            function closeModal() {
                modal.classList.remove('unsaved-modal--open');
                pendingHref = null;
            }
            function openModal(href) {
                pendingHref = href;
                modal.classList.add('unsaved-modal--open');
            }

            modal.querySelector('.unsaved-modal__confirm').addEventListener('click', function() {
                var href = pendingHref;
                formChanged = false;
                closeModal();
                if (href) window.location.href = href;
            });
            modal.querySelector('.unsaved-modal__cancel').addEventListener('click', closeModal);
            modal.querySelector('.unsaved-modal__backdrop').addEventListener('click', closeModal);

            document.addEventListener('keydown', function(e) {
                if (e.key === 'Escape' && modal.classList.contains('unsaved-modal--open')) {
                    closeModal();
                }
            });

            // Intercept link navigation away from the form
            document.addEventListener('click', function(e) {
                if (!formChanged) return;
                var link = e.target.closest('a');
                if (!link) return;
                var href = link.getAttribute('href');
                if (!href || href.charAt(0) === '#' || href.indexOf('javascript:') === 0) return;
                if (e.metaKey || e.ctrlKey || e.shiftKey || link.target === '_blank') return;
                if (link.href === window.location.href) return;
                e.preventDefault();
                openModal(link.href);
            }, true);

            // Browser-level fallback (refresh / close tab / back button)
            window.addEventListener('beforeunload', function(e) {
                if (formChanged) {
                    e.preventDefault();
                    e.returnValue = '';
                }
            });
        }
    });
})();
