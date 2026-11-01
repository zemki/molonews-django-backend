from django.db.models import Sum, Count, Q, Avg
from django.db.models.functions import TruncDate, TruncWeek, TruncMonth, Coalesce
from django.utils import timezone
from django.views.generic import TemplateView

from content.models import Article, EventV4, Area
from content.permissions import allowed_sources

from .mixins import NewAdminMixin


def _parse_days(request):
    raw = request.GET.get('days', '30')
    if raw == 'all':
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 30


def _user_role(user):
    if user.is_superuser:
        return 'superuser'
    if user.groups.filter(id=1).exists():
        return 'editor'
    if user.groups.filter(id=2).exists():
        return 'contributor'
    return 'staff'


def _filtered_querysets(user):
    """Return (article_qs, event_qs) filtered by user role. Caches role via single query."""
    role = _user_role(user)
    article_qs = Article.objects.all()
    event_qs = EventV4.objects.all()

    if role in ('contributor', 'editor'):
        # Gleiche Quellen-Regel wie Listen und Formulare (content.permissions)
        own_sources = allowed_sources(user)
        article_qs = article_qs.filter(source__in=own_sources)
        event_qs = event_qs.filter(source__in=own_sources)
    elif role == 'staff':
        article_qs = Article.objects.none()
        event_qs = EventV4.objects.none()
    return article_qs, event_qs


def _calc_delta(current, previous):
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def _section_aggregates(qs, days):
    """Single-query stats for a queryset + previous period deltas.

    Reduces ~9 separate COUNT/SUM queries to 2 aggregate queries per section.
    """
    now = timezone.now()
    if days is not None:
        cutoff = now - timezone.timedelta(days=days)
        prev_cutoff = cutoff - timezone.timedelta(days=days)
        current_qs = qs.filter(date__gte=cutoff)
        prev_qs = qs.filter(date__gte=prev_cutoff, date__lt=cutoff)
    else:
        current_qs = qs
        prev_qs = qs.none()

    # NB: aggregate keys must not collide with model field names referenced
    # inside Q(...) filters, or Django will treat the field as the aggregate.
    current = current_qs.aggregate(
        n_total=Count('id'),
        n_published=Count('id', filter=Q(published=True)),
        n_drafts=Count('id', filter=Q(draft=True)),
        n_review=Count('id', filter=Q(up_for_review=True, published=False)),
        n_total_views=Coalesce(Sum('request_count'), 0),
        n_avg_views=Coalesce(Avg('request_count'), 0.0),
    )
    prev = prev_qs.aggregate(
        n_total=Count('id'),
        n_published=Count('id', filter=Q(published=True)),
        n_total_views=Coalesce(Sum('request_count'), 0),
    )

    return {
        'total': current['n_total'],
        'published': current['n_published'],
        'drafts': current['n_drafts'],
        'review': current['n_review'],
        'total_views': current['n_total_views'],
        'avg_views': round(current['n_avg_views'] or 0, 1),
        'delta_total': _calc_delta(current['n_total'], prev['n_total']),
        'delta_published': _calc_delta(current['n_published'], prev['n_published']),
        'delta_views': _calc_delta(current['n_total_views'], prev['n_total_views']),
    }


def _apply_date_filter(qs, days):
    if days is None:
        return qs
    cutoff = timezone.now() - timezone.timedelta(days=days)
    return qs.filter(date__gte=cutoff)


class StatsView(NewAdminMixin, TemplateView):
    """Shell view — renders page structure + skeleton placeholders only.

    Actual data is fetched async via the 3 partial endpoints below.
    """
    template_name = 'newadmin/stats/index.html'
    current_section = 'stats'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['current_days'] = self.request.GET.get('days', '30')
        return ctx


class StatsArticlesPartialView(NewAdminMixin, TemplateView):
    """HTML partial: article stats, chart, top articles."""
    template_name = 'newadmin/stats/_articles.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        days = _parse_days(self.request)
        article_qs, _ = _filtered_querysets(self.request.user)

        agg = _section_aggregates(article_qs, days)
        ctx.update({
            'article_total': agg['total'],
            'article_published': agg['published'],
            'article_drafts': agg['drafts'],
            'article_review': agg['review'],
            'article_total_views': agg['total_views'],
            'article_avg_views': agg['avg_views'],
            'article_delta_total': agg['delta_total'],
            'article_delta_published': agg['delta_published'],
            'article_delta_views': agg['delta_views'],
        })

        top_articles_qs = _apply_date_filter(article_qs, days)
        ctx['top_articles'] = list(
            top_articles_qs.filter(published=True)
            .only('id', 'title', 'request_count')
            .order_by('-request_count')[:5]
        )

        # Bucket width is chosen so the chart never grows beyond ~60 bars.
        # Otherwise 365 daily bars at 3px gap overflow the column and push
        # the whole page past the viewport.
        chart_days = days if days else 365
        if chart_days <= 60:
            trunc, bucket_label, bucket_name = TruncDate('date'), "j. M", 'day'
        elif chart_days <= 180:
            trunc, bucket_label, bucket_name = TruncWeek('date'), "j. M", 'week'
        else:
            trunc, bucket_label, bucket_name = TruncMonth('date'), "b Y", 'month'
        chart_cutoff = timezone.now() - timezone.timedelta(days=chart_days)
        articles_per_day = list(
            article_qs.filter(date__gte=chart_cutoff)
            .annotate(day=trunc)
            .values('day')
            .annotate(count=Count('id'))
            .order_by('day')
        )
        max_count = max((d['count'] for d in articles_per_day), default=1) or 1
        for d in articles_per_day:
            d['pct'] = round(d['count'] / max_count * 100)
        ctx['articles_per_day'] = articles_per_day
        ctx['chart_bucket'] = bucket_name
        ctx['chart_bucket_label'] = bucket_label
        return ctx


class StatsEventsPartialView(NewAdminMixin, TemplateView):
    """HTML partial: event stats, top events."""
    template_name = 'newadmin/stats/_events.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        days = _parse_days(self.request)
        _, event_qs = _filtered_querysets(self.request.user)

        agg = _section_aggregates(event_qs, days)
        ctx.update({
            'event_total': agg['total'],
            'event_published': agg['published'],
            'event_drafts': agg['drafts'],
            'event_total_views': agg['total_views'],
            'event_avg_views': agg['avg_views'],
            'event_delta_total': agg['delta_total'],
            'event_delta_published': agg['delta_published'],
            'event_delta_views': agg['delta_views'],
        })

        ctx['event_upcoming'] = event_qs.filter(
            occurrences__start_datetime__gte=timezone.now()
        ).distinct().count()

        top_events_qs = _apply_date_filter(event_qs, days)
        ctx['top_events'] = list(
            top_events_qs.filter(published=True)
            .only('id', 'title', 'request_count')
            .order_by('-request_count')[:5]
        )

        chart_days = days if days else 365
        if chart_days <= 60:
            trunc, bucket_label, bucket_name = TruncDate('date'), "j. M", 'day'
        elif chart_days <= 180:
            trunc, bucket_label, bucket_name = TruncWeek('date'), "j. M", 'week'
        else:
            trunc, bucket_label, bucket_name = TruncMonth('date'), "b Y", 'month'
        chart_cutoff = timezone.now() - timezone.timedelta(days=chart_days)
        events_per_day = list(
            event_qs.filter(date__gte=chart_cutoff)
            .annotate(day=trunc)
            .values('day')
            .annotate(count=Count('id'))
            .order_by('day')
        )
        max_count = max((d['count'] for d in events_per_day), default=1) or 1
        for d in events_per_day:
            d['pct'] = round(d['count'] / max_count * 100)
        ctx['events_per_day'] = events_per_day
        ctx['chart_bucket'] = bucket_name
        ctx['chart_bucket_label'] = bucket_label
        return ctx


class StatsAreasPartialView(NewAdminMixin, TemplateView):
    """HTML partial: area breakdown.

    Uses single GROUP BY queries instead of N+1 loop over Area.objects.all().
    """
    template_name = 'newadmin/stats/_areas.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        days = _parse_days(self.request)
        article_qs, event_qs = _filtered_querysets(self.request.user)

        a_qs = _apply_date_filter(article_qs, days)
        e_qs = _apply_date_filter(event_qs, days)

        articles_by_area = {
            row['area']: row for row in
            a_qs.values('area').annotate(
                count=Count('id', distinct=True),
                views=Coalesce(Sum('request_count'), 0),
            )
        }
        events_by_area = {
            row['area']: row for row in
            e_qs.values('area').annotate(
                count=Count('id', distinct=True),
                views=Coalesce(Sum('request_count'), 0),
            )
        }

        area_ids = set(articles_by_area) | set(events_by_area)
        area_ids.discard(None)
        areas = {a.id: a.name for a in Area.objects.filter(id__in=area_ids).only('id', 'name')}

        area_stats = []
        for aid in area_ids:
            a_row = articles_by_area.get(aid, {'count': 0, 'views': 0})
            e_row = events_by_area.get(aid, {'count': 0, 'views': 0})
            total_content = a_row['count'] + e_row['count']
            total_views = (a_row['views'] or 0) + (e_row['views'] or 0)
            if total_content == 0:
                continue
            area_stats.append({
                'name': areas.get(aid, '—'),
                'articles': a_row['count'],
                'events': e_row['count'],
                'total_views': total_views,
                'avg_views': round(total_views / total_content, 1) if total_content else 0,
            })
        area_stats.sort(key=lambda x: x['total_views'], reverse=True)
        ctx['area_stats'] = area_stats[:10]
        return ctx
