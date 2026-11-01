from django.shortcuts import render, get_object_or_404
from content.models import Article
from content.utils.urls import link_scheme
from .article_event_shared import get_source_default_image_url

def article_public_page(request, pk):
    article = get_object_or_404(Article, pk=pk, published=True)

    # Bild-Logik
    if article.image:
        image_url = request.build_absolute_uri(article.image.url)
    elif article.image_url:
        image_url = article.image_url
    else:
        image_url = get_source_default_image_url(article, {"request": request})

    # "Weiterlesen" nur anzeigen wenn ein echter externer Link existiert
    link = article.link or ""
    show_read_more = (
        link.strip() not in ("", "http://", "https://")
        and link_scheme(link) in ("http", "https")
        and article.source.type != "local"
    )

    return render(
        request,
        "articles/article_public.html",
        {
            "article": article,
            "image_url": image_url,
            "show_read_more": show_read_more,
        },
    )
