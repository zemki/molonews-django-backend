from django.db.models import Q
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import ListView, CreateView, UpdateView, DeleteView

from content.models import Article
from ..forms.article import ArticleForm
from .mixins import ArticlePermissionMixin, ContentSaveMixin


class ArticleListView(ArticlePermissionMixin, ListView):
    model = Article
    template_name = 'newadmin/article/list.html'
    context_object_name = 'articles'
    paginate_by = 25
    current_section = 'artikel'

    def get_queryset(self):
        qs = super().get_queryset()

        # Search
        q = self.request.GET.get('q', '').strip()
        field = self.request.GET.get('field', 'all')
        if q:
            if field == 'title':
                qs = qs.filter(Q(title__icontains=q))
            elif field == 'content':
                qs = qs.filter(Q(abstract__icontains=q) | Q(content__icontains=q))
            elif field == 'topic':
                qs = qs.filter(Q(tags__name__icontains=q)).distinct()
            elif field == 'organization':
                qs = qs.filter(Q(source__name__icontains=q))
            elif field == 'area':
                qs = qs.filter(Q(area__name__icontains=q)).distinct()
            else:  # 'all'
                qs = qs.filter(
                    Q(title__icontains=q)
                    | Q(abstract__icontains=q)
                    | Q(content__icontains=q)
                    | Q(source__name__icontains=q)
                    | Q(tags__name__icontains=q)
                    | Q(area__name__icontains=q)
                ).distinct()

        # Status filter
        status = self.request.GET.get('status', '')
        if status == 'published':
            qs = qs.filter(published=True)
        elif status == 'unpublished':
            qs = qs.filter(published=False, draft=False)
        elif status == 'draft':
            qs = qs.filter(draft=True)
        elif status == 'review':
            qs = qs.filter(up_for_review=True, published=False)

        # Sort
        sort = self.request.GET.get('sort', '-date')
        allowed_sorts = {
            '-date': '-date',
            'date': 'date',
            'title': 'title',
            '-title': '-title',
            '-views': '-request_count',
            'views': 'request_count',
        }
        order = allowed_sorts.get(sort, '-date')
        qs = qs.order_by(order)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['search_query'] = self.request.GET.get('q', '')
        ctx['current_field'] = self.request.GET.get('field', 'all')
        ctx['current_status'] = self.request.GET.get('status', '')
        ctx['current_sort'] = self.request.GET.get('sort', '-date')
        ctx['fab_url'] = reverse_lazy('newadmin:article-create')
        ctx['fab_title'] = 'Neuer Artikel'
        return ctx


class ArticleCreateView(ContentSaveMixin, ArticlePermissionMixin, CreateView):
    model = Article
    form_class = ArticleForm
    template_name = 'newadmin/article/form.html'
    current_section = 'artikel'

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['is_create'] = True
        ctx['page_title'] = 'Neuer Artikel'
        return ctx

    def form_valid(self, form):
        return self._save_article(form, is_change=False)

    def _save_article(self, form, is_change):
        obj = form.save(commit=False)

        # Nur eigene Quellen; veröffentlichen nur mit Veröffentlichungsrecht
        if not self.apply_source_and_publish_rules(obj, form):
            return self.form_invalid(form)

        # Handle image for new objects (VersatileImageField needs ID)
        temp_image = None
        if obj.pk is None and obj.image:
            temp_image = obj.image
            obj.image = None

        obj.up_for_review = True
        obj.draft = False

        obj.save()
        form.save_m2m()

        # Re-add image after ID is available
        if temp_image:
            obj.image = temp_image
            obj.save()

        if self.publish_denied:
            messages.warning(self.request, f'Artikel "{obj.title}" zur Prüfung eingereicht – Ihr Konto darf nicht selbst veröffentlichen.')
        else:
            messages.success(self.request, f'Artikel "{obj.title}" gespeichert.')
        return redirect('newadmin:article-list')


class ArticleUpdateView(ContentSaveMixin, ArticlePermissionMixin, UpdateView):
    model = Article
    form_class = ArticleForm
    template_name = 'newadmin/article/form.html'
    current_section = 'artikel'

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['is_create'] = False
        ctx['page_title'] = 'Artikel bearbeiten'
        return ctx

    def form_valid(self, form):
        return self._save_article(form, is_change=True)

    # Reuse save logic from CreateView
    _save_article = ArticleCreateView._save_article


class ArticleDeleteView(ArticlePermissionMixin, DeleteView):
    model = Article
    template_name = 'newadmin/article/confirm_delete.html'
    success_url = reverse_lazy('newadmin:article-list')
    current_section = 'artikel'

    def form_valid(self, form):
        title = self.object.title
        response = super().form_valid(form)
        messages.success(self.request, f'Artikel "{title}" geloescht.')
        return response
