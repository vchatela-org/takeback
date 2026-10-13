"""Platform pages: every import of one platform (Amazon, Facebook...) viewed
together as one continuous, deduplicated history -- importing a fresh export
every year extends the same dashboards instead of starting a new silo.

Each dashboard element that aggregates items can be clicked to open the
items behind it in a side panel (/p/{source}/panel, /p/{source}/purchases/
panel): "I spent a lot in Kitchen" -> what exactly.
"""
import json
from collections import Counter
from datetime import datetime
from typing import List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import crud, platforms, stats
from app.database import get_db
from app.importers.meta_common import find_instagram_post_urls
from app.routers.web import parse_date
from app.templating import display_label, templates, url_with

router = APIRouter(prefix="/p/{source}", tags=["platforms"])

PANEL_LIMIT = 100
PURCHASE_PANEL_LIMIT = 200
PURCHASE_LIST_PAGE_SIZE = 50
MESSAGE_PAGE_SIZE = 50
PEOPLE_PANEL_LIMIT = 500


def _page(request: Request, db: Session, source: str, view: str, include_1970: bool = False) -> Optional[dict]:
    """Common context of every platform page (header, tabs, the "include
    1970" switch of the filters), or None when the platform holds no data
    for this view -- the caller then redirects to the platform's overview."""
    if not platforms.is_supported(source):
        raise HTTPException(status_code=404, detail="Plateforme inconnue")
    item_types = crud.get_item_types_by_source(db, source).get(source, set())
    views = platforms.available_views(item_types)
    if view not in {v.key for v in views}:
        return None
    return {
        "platform": platforms.get(source),
        "summary": crud.get_platform_summary(db, source),
        "views": views,
        "active_view": view,
        "item_types": item_types,
        "undated": {"count": crud.count_1970(db, source), "included": include_1970},
    }


def _period_context(date_from: Optional[str], date_to: Optional[str], years: List[int]) -> dict:
    return {"date_from": date_from or "", "date_to": date_to or "", "years": years}


def _choices(options: List[dict], selected: List[str]) -> List[str]:
    """A multi-select's values, cleaned: unknown ones dropped, and every
    option ticked read as no filter at all."""
    known = {o["value"] for o in options}
    selected = [s for s in selected if s in known]
    return [] if known and set(selected) >= known else selected


def _content(db: Session, source: str, selected: List[str], include_1970: bool) -> tuple:
    """The content filter of the overview and activity pages: (categories
    kept -- none means all --, their colour ranking, the filter's options
    with their dated item counts)."""
    counts = crud.get_dated_counts(db, source, include_1970=include_1970)
    options = [{"value": c, "label": display_label(c), "count": n} for c, n in counts if c != "Autre"]
    selected = _choices(options, selected)
    ranking = [c for c, _n in counts if not selected or c in selected]
    return selected, ranking, options


def _years_of(rows) -> List[int]:
    return sorted({row[1].year for row in rows if row[1] is not None}, reverse=True)


def _granularity(value: str, default: str = "month") -> str:
    return value if value in stats.GRANULARITIES else default


# ────────────────────────────────────────────────
# Overview
# ────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
def overview(
    source: str, request: Request,
    category: List[str] = Query([]), include_1970: bool = False,
    db: Session = Depends(get_db),
):
    ctx = _page(request, db, source, "overview", include_1970)

    categories: dict = {}
    for cat, item_type, count in crud.get_category_counts(db, source=source):
        bucket = categories.setdefault(cat, {"total": 0, "item_types": []})
        bucket["total"] += count
        bucket["item_types"].append((item_type, count))

    # The whole history at a glance, which content made it: by year once
    # it spans a few, by month before that.
    category, ranking, content_options = _content(db, source, category, include_1970)
    years = crud.get_years(db, source, include_1970=include_1970)
    granularity = "year" if years and years[0] - years[-1] >= 3 else "month"
    timeline = stats.build_count_series(
        crud.get_activity_counts(db, source, granularity, category, include_1970), granularity, ranking,
    )
    for series in timeline["series"]:
        series["label"] = display_label(series["label"])

    batches = [b for b in crud.get_batches(db, source) if b.status.value == "done"]
    ctx.update({
        "categories": sorted(categories.items(), key=lambda kv: -kv[1]["total"]),
        "batches": batches,
        "coverage": crud.get_batch_coverage(db, source) if batches else {},
        "content_options": content_options,
        "selected_categories": category,
        "timeline": timeline,
        "timeline_granularity": granularity,
    })
    return templates.TemplateResponse(request, "platform/overview.html", ctx)


# ────────────────────────────────────────────────
# Generic drill-down panel: the items behind a chart bar / heatmap cell
# ────────────────────────────────────────────────

@router.get("/panel", response_class=HTMLResponse)
def items_panel(
    source: str, request: Request,
    category: List[str] = Query([]), item_type: List[str] = Query([]), device: List[str] = Query([]),
    context: Optional[str] = None, title: Optional[str] = None, author: Optional[str] = None,
    q: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    weekday: Optional[int] = None, hour: Optional[int] = None, batch_id: Optional[int] = None,
    include_1970: bool = False,
    db: Session = Depends(get_db),
):
    items, total = crud.search_items(
        db, page=1, page_size=PANEL_LIMIT,
        source=source, category=category, item_type=item_type, device=device, context=context, title=title, author=author, q=q,
        date_from=parse_date(date_from), date_to=parse_date(date_to, end_of_day=True),
        weekday=weekday, hour=hour, batch_id=batch_id, include_1970=include_1970,
    )
    browse_url = None
    if (len(category) <= 1 and len(item_type) <= 1 and weekday is None and hour is None and not title and not author
            and not device):
        browse_url = url_with(
            request, "/browse", source=source, category=category[0] if category else None,
            item_type=item_type[0] if item_type else None, weekday=None, hour=None,
            include_1970=None, group_by=None,
        )
    return _items_panel_response(request, items, total, browse_url)


def _items_panel_response(request: Request, items: list, total: int, browse_url: Optional[str] = None,
                          open_url: Optional[str] = None, open_label: Optional[str] = None):
    return templates.TemplateResponse(
        request, "platform/_panel_items.html",
        {
            "items": items, "total": total, "limit": PANEL_LIMIT, "browse_url": browse_url,
            "open_url": open_url, "open_label": open_label,
            "items_fields": {it.id: json.loads(it.fields_json) for it in items if it.fields_json},
        },
    )


# ────────────────────────────────────────────────
# Activity (any platform): items over time, weekday x hour heatmap
# ────────────────────────────────────────────────

ACTIVITY_GROUP_DIMENSIONS = {"category": "Par catégorie", "item_type": "Par type de contenu"}


@router.get("/activity", response_class=HTMLResponse)
def activity(
    source: str, request: Request,
    granularity: str = "month", date_from: Optional[str] = None, date_to: Optional[str] = None,
    category: List[str] = Query([]), group_by: str = "category", include_1970: bool = False,
    db: Session = Depends(get_db),
):
    ctx = _page(request, db, source, "activity", include_1970)
    if ctx is None:
        return RedirectResponse(f"/p/{source}")
    granularity = _granularity(granularity)
    group_by = group_by if group_by in ACTIVITY_GROUP_DIMENSIONS else "category"

    category, ranking, content_options = _content(db, source, category, include_1970)
    if group_by == "item_type":
        # Within the content kept, which file each item came from: the
        # detail behind a category ("likes" vs "comments").
        ranking = [t for t, _n in crud.get_dated_counts(db, source, "item_type", category, include_1970)]
    events = crud.get_activity_events(
        db, source, parse_date(date_from), parse_date(date_to, end_of_day=True), categories=category,
        by=group_by, include_1970=include_1970,
    )
    timeseries = stats.build_timeseries(events, granularity, ranking)
    for series in timeseries["series"]:
        series["label"] = display_label(series["label"])
    heatmap = stats.build_heatmap(events)
    top_weekdays, top_hours = stats.most_active(events)

    ctx.update(_period_context(date_from, date_to, crud.get_years(db, source, include_1970=include_1970)))
    ctx.update({
        "granularity": granularity,
        "group_by": group_by,
        "group_dimensions": ACTIVITY_GROUP_DIMENSIONS,
        "selected_categories": category,
        "content_options": content_options,
        "timeseries": timeseries,
        "heatmap": heatmap,
        "heatmap_max": max((max(row) for row in heatmap), default=0),
        "untimed_events": sum(1 for ts, _ in events if not stats.has_time_of_day(ts)),
        "total_events": len(events),
        "estimated_duration": stats.format_duration_minutes(stats.estimate_time_spent_minutes(events)),
        "top_weekdays": [(stats.WEEKDAY_NAMES_FR[d], n) for d, n in top_weekdays],
        "top_hours": top_hours,
        "weekday_names": stats.WEEKDAY_NAMES_FR,
    })
    return templates.TemplateResponse(request, "platform/activity.html", ctx)


# ────────────────────────────────────────────────
# Usages (Google): what you do, whichever service logged it
# ────────────────────────────────────────────────

@router.get("/usage", response_class=HTMLResponse)
def usage(
    source: str, request: Request,
    granularity: str = "month", date_from: Optional[str] = None, date_to: Optional[str] = None,
    family: str = "", include_1970: bool = False,
    db: Session = Depends(get_db),
):
    ctx = _page(request, db, source, "usage", include_1970)
    if ctx is None:
        return RedirectResponse(f"/p/{source}")
    granularity = _granularity(granularity)
    present = ctx["item_types"]
    families = stats.usage_families(present)
    if not families:
        return RedirectResponse(f"/p/{source}")
    selected = next((f for f in families if f.key == family), families[0])
    period_from, period_to = parse_date(date_from), parse_date(date_to, end_of_day=True)

    # Every family's rankings come out of two grouped counts over the
    # period: by title, and by author for those ranked by it.
    def ranked_types(by_author: bool) -> List[str]:
        return sorted({t for f in families for r in f.rankings if (r.column == "author") == by_author
                       for t in r.types(present)})

    title_counts = crud.get_value_counts(db, source, ranked_types(False), "title", period_from, period_to)
    author_types = ranked_types(True)
    author_counts = crud.get_value_counts(db, source, author_types, "author", period_from, period_to) \
        if author_types else []
    rankings = []
    for index, ranking in enumerate(selected.rankings):
        types = set(ranking.types(present))
        counts = author_counts if ranking.column == "author" else title_counts
        rankings.append({"index": index, "title": ranking.title, "noun": ranking.noun,
                         **stats.build_ranking((c for c in counts if c[0] in types), ranking.by)})

    trend = None
    if selected.trend:
        first = selected.rankings[0]
        types = first.types(present)
        # The leaders of the whole history, so a series keeps its colour
        # whatever the period.
        leaders = stats.build_ranking(crud.get_value_counts(db, source, types, first.column), first.by,
                                      limit=stats.TOP_SERIES)["top"]
        events = crud.get_value_events(db, source, types, first.column, period_from, period_to, include_1970)
        trend = stats.build_usage_trend([(ts, value) for _id, ts, value in events], granularity, leaders)

    _categories, ranking, _options = _content(db, source, [], include_1970)
    for_hours = crud.get_hour_counts(db, source, period_from, period_to, include_1970)
    hour_profile = stats.build_hour_profile(for_hours, ranking)
    for row in hour_profile:
        row["label"] = display_label(row["label"])
    # Per day with any activity, not per calendar day: a history that goes
    # back years for one service (YouTube) but 18 months for the rest would
    # otherwise read as a fraction of the real daily rate.
    dated, days = crud.get_active_days(db, source, period_from, period_to, include_1970)

    ctx.update(_period_context(date_from, date_to, crud.get_years(db, source, include_1970=include_1970)))
    ctx.update({
        "granularity": granularity,
        "families": families,
        "family": selected,
        "headlines": stats.build_usage_headlines(families, present, title_counts, author_counts),
        "rankings": rankings,
        "trend": trend,
        "hour_profile": hour_profile,
        "coverage": stats.build_coverage(crud.get_context_coverage(db, source, include_1970), ranking),
        "per_day": {"dated": dated, "days": days, "average": dated / days if days else None},
    })
    return templates.TemplateResponse(request, "platform/usage.html", ctx)


@router.get("/usage/panel", response_class=HTMLResponse)
def usage_panel(
    source: str, request: Request, family: str, rank: int = 0,
    key: Optional[str] = None, exclude: List[str] = Query([]),
    date_from: Optional[str] = None, date_to: Optional[str] = None, include_1970: bool = False,
    db: Session = Depends(get_db),
):
    """Items behind a usage ranking entry (`key`), or behind a bar of its
    leaders over time: one leader (`key`), every other entity (`exclude`
    lists the leaders), or all of them."""
    selected = stats.USAGE_FAMILIES.get(family)
    if selected is None or not 0 <= rank < len(selected.rankings):
        return _items_panel_response(request, [], 0)
    ranking = selected.rankings[rank]
    present = crud.get_item_types_by_source(db, source).get(source, set())
    rows = crud.get_value_events(
        db, source, ranking.types(present), ranking.column, parse_date(date_from), parse_date(date_to, end_of_day=True),
        include_1970, dated_only=bool(date_from or date_to),
    )
    ids = stats.select_usage(rows, ranking.by, key, exclude)
    return _items_panel_response(request, crud.get_items_by_ids(db, ids[:PANEL_LIMIT]), len(ids))


# ────────────────────────────────────────────────
# Purchases (Amazon): dashboard, drill-down panel, full list
# ────────────────────────────────────────────────

def _group_by(value: str, source: Optional[str] = None) -> str:
    """The dimension to group by: the one asked for if this platform offers
    it, its first one otherwise (Amazon's and Seazon's purchase pages don't
    offer the same set -- see stats.purchase_group_dimensions)."""
    dimensions = stats.purchase_group_dimensions(source)
    return value if value in dimensions else next(iter(dimensions))


@router.get("/purchases", response_class=HTMLResponse)
def purchases(
    source: str, request: Request,
    granularity: str = "month", date_from: Optional[str] = None, date_to: Optional[str] = None,
    group_by: str = "category", include_1970: bool = False,
    db: Session = Depends(get_db),
):
    ctx = _page(request, db, source, "purchases", include_1970)
    if ctx is None:
        return RedirectResponse(f"/p/{source}")
    granularity = _granularity(granularity)
    group_by = _group_by(group_by, source)

    # Whole history fetched once (a purchase history is thousands of rows,
    # not millions): it ranks the groups for stable colors and lists the
    # years, then the chosen period is cut out of it in Python.
    all_rows = crud.get_dashboard_rows(db, source, stats.PURCHASE_ITEM_TYPES, include_1970=include_1970)
    period_from, period_to = parse_date(date_from), parse_date(date_to, end_of_day=True)
    rows = stats.in_period(all_rows, period_from, period_to)
    ranking = stats.purchase_group_ranking(all_rows, group_by)
    dashboard = stats.build_purchase_dashboard(rows, granularity, group_by, ranking)
    breakdown = stats.build_group_breakdown(rows, group_by)
    top_purchases = sorted(
        (p for p in stats.build_purchase_list(rows, group_by) if p["amount"] is not None),
        key=lambda p: -p["amount"],
    )[:10]
    years = _years_of(all_rows)
    yearly = stats.build_yearly_cumulative(
        rows, years, period_from, period_to, max((r[1] for r in all_rows if r[1] is not None), default=None),
    )

    ctx.update(_period_context(date_from, date_to, years))
    ctx.update({
        "granularity": granularity,
        "group_by": group_by,
        "group_dimensions": stats.purchase_group_dimensions(source),
        "currency": stats.detect_currency(all_rows),
        "dashboard": dashboard,
        "breakdown": breakdown,
        "top_purchases": top_purchases,
        "rhythm": stats.build_purchase_rhythm(rows, group_by, ranking),
        # One year on its own is just the running total above, day by day.
        "yearly": yearly if len(yearly["series"]) > 1 else None,
        "weekday_names": stats.WEEKDAY_NAMES_FR,
    })
    return templates.TemplateResponse(request, "platform/purchases.html", ctx)


def _filter_purchases(purchases: list, groups: List[str], q: Optional[str],
                      amount_min: Optional[float], amount_max: Optional[float]) -> list:
    if groups:
        wanted = set(groups)
        purchases = [p for p in purchases if p["group"] in wanted]
    if q:
        needle = q.lower()
        purchases = [
            p for p in purchases
            if needle in (p["product"] or "").lower() or needle in (p["order_id"] or "").lower()
        ]
    if amount_min is not None:
        purchases = [p for p in purchases if p["amount"] is not None and p["amount"] >= amount_min]
    if amount_max is not None:
        purchases = [p for p in purchases if p["amount"] is not None and p["amount"] <= amount_max]
    return purchases


def _purchase_totals(purchases: list) -> dict:
    return {
        "lines": len(purchases),
        "orders": len({p["order_id"] for p in purchases if p["order_id"]}),
        "amount": round(sum(p["amount"] or 0 for p in purchases), 2),
    }


def _parse_amount(value: Optional[str]) -> Optional[float]:
    if value is None or not value.strip():
        return None
    try:
        return float(value.replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


@router.get("/purchases/panel", response_class=HTMLResponse)
def purchases_panel(
    source: str, request: Request,
    group_by: str = "category", group: List[str] = Query([]),
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    weekday: Optional[int] = None, hour: Optional[int] = None, include_1970: bool = False,
    db: Session = Depends(get_db),
):
    group_by = _group_by(group_by, source)
    rows = crud.get_dashboard_rows(
        db, source, stats.PURCHASE_LINE_ITEM_TYPES, parse_date(date_from), parse_date(date_to, end_of_day=True),
        include_1970,
    )
    selected = _filter_purchases(stats.build_purchase_list(rows, group_by), group, None, None, None)
    if weekday is not None:
        selected = [p for p in selected if p["timestamp"] and p["timestamp"].weekday() == weekday]
    if hour is not None:
        # Same rule as the heatmap cell that opened this: a date-only
        # purchase has no hour, so it never answers "at 0h".
        selected = [
            p for p in selected
            if p["timestamp"] and stats.has_time_of_day(p["timestamp"]) and p["timestamp"].hour == hour
        ]
    selected.sort(key=lambda p: (p["amount"] is not None, p["amount"] or 0), reverse=True)

    # Within a group, which other dimension explains the spend best is
    # unknowable in general -- but "when" always is: spend per year (or
    # per month inside a single year) gives the panel a shape at a glance.
    by_period: dict = {}
    years = {p["timestamp"].year for p in selected if p["timestamp"]}
    period_fmt = "%m/%Y" if len(years) == 1 else "%Y"
    for p in selected:
        if p["timestamp"] and p["amount"]:
            key = p["timestamp"].strftime(period_fmt)
            sort_key = p["timestamp"].strftime("%Y%m" if period_fmt == "%m/%Y" else "%Y")
            entry = by_period.setdefault(sort_key, {"label": key, "value": 0.0})
            entry["value"] += p["amount"]
    period_series = [by_period[k] for k in sorted(by_period)]
    period_max = max((e["value"] for e in period_series), default=0)

    return templates.TemplateResponse(
        request, "platform/_panel_purchases.html",
        {
            "source": source, "purchases": selected[:PURCHASE_PANEL_LIMIT], "limit": PURCHASE_PANEL_LIMIT,
            "totals": _purchase_totals(selected),
            "currency": stats.detect_currency(rows),
            "group_by": group_by, "dimension_label": stats.purchase_group_dimensions(source)[group_by],
            "period_series": period_series, "period_max": period_max,
            # The list has no weekday/hour filter: it would show more than
            # the panel claims to, so it isn't offered for such a slice.
            "list_url": url_with(request, f"/p/{source}/purchases/list")
            if weekday is None and hour is None else None,
        },
    )


@router.get("/purchases/list", response_class=HTMLResponse)
def purchases_list(
    source: str, request: Request,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    group_by: str = "category", group: List[str] = Query([]),
    q: Optional[str] = None, amount_min: Optional[str] = None, amount_max: Optional[str] = None,
    sort: str = "date", direction: str = "desc", page: int = 1, include_1970: bool = False,
    db: Session = Depends(get_db),
):
    ctx = _page(request, db, source, "purchases", include_1970)
    if ctx is None:
        return RedirectResponse(f"/p/{source}")
    group_by = _group_by(group_by, source)
    sort = sort if sort in ("amount", "date", "product", "group") else "date"
    direction = direction if direction in ("asc", "desc") else "desc"

    all_rows = crud.get_dashboard_rows(db, source, stats.PURCHASE_LINE_ITEM_TYPES, include_1970=include_1970)
    group_values = stats.purchase_group_ranking(all_rows, group_by)
    # Switching the dimension submits the value picked for the previous
    # one along with it -- a value the new dimension doesn't have is dropped
    # rather than silently emptying the list.
    group = [g for g in group if g in set(group_values)]
    rows = stats.in_period(all_rows, parse_date(date_from), parse_date(date_to, end_of_day=True))
    purchases = _filter_purchases(
        stats.build_purchase_list(rows, group_by), group, q, _parse_amount(amount_min), _parse_amount(amount_max),
    )

    reverse = direction == "desc"
    if sort == "amount":
        purchases.sort(key=lambda p: (p["amount"] is not None, p["amount"] or 0), reverse=reverse)
    elif sort == "product":
        purchases.sort(key=lambda p: (p["product"] or "").lower(), reverse=reverse)
    elif sort == "group":
        purchases.sort(key=lambda p: (p["group"] or "").lower(), reverse=reverse)
    else:
        purchases.sort(key=lambda p: (p["timestamp"] is not None, p["timestamp"] or datetime.min), reverse=reverse)

    total = len(purchases)
    page_count = max(1, -(-total // PURCHASE_LIST_PAGE_SIZE))  # ceil division
    page = min(max(1, page), page_count)
    start = (page - 1) * PURCHASE_LIST_PAGE_SIZE

    ctx.update(_period_context(date_from, date_to, _years_of(all_rows)))
    ctx.update({
        "purchases": purchases[start:start + PURCHASE_LIST_PAGE_SIZE],
        "totals": _purchase_totals(purchases),
        "currency": stats.detect_currency(all_rows),
        "group_by": group_by, "group_dimensions": stats.purchase_group_dimensions(source),
        "groups": group, "group_values": group_values,
        "q": q or "", "amount_min": amount_min or "", "amount_max": amount_max or "",
        "sort": sort, "direction": direction, "page": page, "page_count": page_count,
    })
    return templates.TemplateResponse(request, "platform/purchases_list.html", ctx)


# ────────────────────────────────────────────────
# Seazon: the formulas, and the dishes that went in them
# ────────────────────────────────────────────────
#
# A Seazon box holds N plates of a chosen formula and is charged as one
# amount, and the same dish comes back week after week. These two pages are
# what a meal-kit account is actually read for; the Achats tab keeps the money
# (spend, refunds), grouped by formula rather than by a category that is
# always the same word.

@router.get("/formulas", response_class=HTMLResponse)
def formulas(
    source: str, request: Request,
    granularity: str = "month", date_from: Optional[str] = None, date_to: Optional[str] = None,
    include_1970: bool = False, db: Session = Depends(get_db),
):
    ctx = _page(request, db, source, "formulas", include_1970)
    if ctx is None:
        return RedirectResponse(f"/p/{source}")
    granularity = _granularity(granularity)

    # Whole history once, like the purchases page: it ranks the formulas by
    # spend (stable chart colors when the period changes), then the chosen
    # period is cut out of it.
    all_rows = crud.get_dashboard_rows(db, source, stats.FORMULA_VIEW_TYPES, include_1970=include_1970)
    rows = stats.in_period(all_rows, parse_date(date_from), parse_date(date_to, end_of_day=True))
    dashboard = stats.build_formula_dashboard(
        rows, granularity, stats.purchase_group_ranking(all_rows, "formula"),
    )

    ctx.update(_period_context(date_from, date_to, _years_of(all_rows)))
    ctx.update({
        "granularity": granularity,
        "dashboard": dashboard,
        "currency": stats.detect_currency(all_rows),
    })
    return templates.TemplateResponse(request, "platform/formulas.html", ctx)


@router.get("/formulas/panel", response_class=HTMLResponse)
def formulas_panel(
    source: str, request: Request,
    formula: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
    include_1970: bool = False, db: Session = Depends(get_db),
):
    """The boxes behind a bar of the boxes chart or a row of the formula
    table (`formula`), or behind every one of them."""
    rows = crud.get_dashboard_rows(db, source, stats.FORMULA_VIEW_TYPES, include_1970=include_1970)
    ids = stats.select_formula_orders(
        rows, formula, parse_date(date_from), parse_date(date_to, end_of_day=True),
    )
    return _items_panel_response(
        request, crud.get_items_by_ids(db, ids[:PANEL_LIMIT]), len(ids),
        browse_url=f"/browse?source={source}&item_type={stats.SEAZON_ORDER_TYPE}",
    )


@router.get("/dishes", response_class=HTMLResponse)
def dishes(
    source: str, request: Request,
    granularity: str = "month", date_from: Optional[str] = None, date_to: Optional[str] = None,
    include_1970: bool = False, db: Session = Depends(get_db),
):
    ctx = _page(request, db, source, "dishes", include_1970)
    if ctx is None:
        return RedirectResponse(f"/p/{source}")
    granularity = _granularity(granularity)

    all_rows = crud.get_titled_rows(db, source, stats.DISH_VIEW_TYPES, include_1970=include_1970)
    rows = stats.in_period(all_rows, parse_date(date_from), parse_date(date_to, end_of_day=True))
    # Two rankings, on purpose: the list below is the period's (the default
    # period being everything, it is then the whole history), while the trend
    # is drawn from the whole history's leaders so that a dish keeps its
    # colour whatever the period shown, as the usage page does.
    all_time = stats.build_dish_ranking(all_rows)

    ctx.update(_period_context(date_from, date_to, _years_of(all_rows)))
    ctx.update({
        "granularity": granularity,
        "ranking": stats.build_dish_ranking(rows),
        "trend": stats.build_dish_trend(rows, granularity, all_time["top"]),
    })
    return templates.TemplateResponse(request, "platform/dishes.html", ctx)


@router.get("/dishes/panel", response_class=HTMLResponse)
def dishes_panel(
    source: str, request: Request,
    dish: Optional[str] = None, exclude: List[str] = Query([]),
    date_from: Optional[str] = None, date_to: Optional[str] = None, delivered: bool = True,
    include_1970: bool = False, db: Session = Depends(get_db),
):
    """The plates of one dish (`dish`), of every dish a chart did not name
    (`exclude`, its "Autres" series), or of every dish."""
    rows = crud.get_titled_rows(db, source, stats.DISH_VIEW_TYPES, include_1970=include_1970)
    ids = stats.select_meals(
        rows, dish, exclude, parse_date(date_from), parse_date(date_to, end_of_day=True),
        delivered_only=delivered,
    )
    return _items_panel_response(
        request, crud.get_items_by_ids(db, ids[:PANEL_LIMIT]), len(ids),
        browse_url=f"/browse?source={source}&item_type=meal",
    )


# ────────────────────────────────────────────────
# Reading & listening (Kindle / Audible)
# ────────────────────────────────────────────────

@router.get("/reading", response_class=HTMLResponse)
def reading(
    source: str, request: Request,
    granularity: str = "month", date_from: Optional[str] = None, date_to: Optional[str] = None,
    device: List[str] = Query([]), include_1970: bool = False,
    db: Session = Depends(get_db),
):
    ctx = _page(request, db, source, "reading", include_1970)
    if ctx is None:
        return RedirectResponse(f"/p/{source}")
    granularity = _granularity(granularity)

    all_rows = crud.get_dashboard_rows(
        db, source, stats.READING_ITEM_TYPES + [stats.KINDLE_FINISHED_TYPE], include_1970=include_1970,
    )
    # One colour per device whatever the filter, so the same device keeps
    # its colour when the others are ticked off.
    devices = stats.reading_devices(all_rows)
    device = _choices(devices, device)
    all_rows = stats.filter_reading_rows(all_rows, device)
    rows = stats.in_period(all_rows, parse_date(date_from), parse_date(date_to, end_of_day=True))
    dashboard = stats.build_reading_dashboard(rows, granularity, devices)
    finished = {row[3] for row in all_rows if row[2] == stats.KINDLE_FINISHED_TYPE}
    titles = stats.build_reading_titles(
        rows, crud.get_titles_by_asin(db, source), limit=None, finished=finished, devices=devices,
    )
    days = stats.reading_days(rows)
    heatmap = stats.build_reading_heatmap(rows)

    ctx.update(_period_context(date_from, date_to, _years_of(all_rows)))
    ctx.update({
        "granularity": granularity,
        "dashboard": dashboard,
        "device_options": devices if len(devices) > 1 else [],
        "selected_devices": device,
        "titles": titles[:15],
        "title_count": len(titles),
        "total_minutes_label": stats.format_duration_minutes(dashboard["total_minutes"]),
        "has_finished": bool(finished),
        "reading_days": len(days),
        "per_day_label": stats.format_duration_minutes(dashboard["total_minutes"] / len(days)) if days else None,
        "streak": stats.longest_streak(days),
        "heatmap": heatmap,
        "heatmap_max": max((max(row) for row in heatmap), default=0),
        "weekday_names": stats.WEEKDAY_NAMES_FR,
    })
    return templates.TemplateResponse(request, "platform/reading.html", ctx)


# ────────────────────────────────────────────────
# Library (Kindle): every book owned, sent or read
# ────────────────────────────────────────────────

LIBRARY_PAGE_SIZE = 50
LIBRARY_SORTS = {
    "activity": lambda b: b["last_activity"] or datetime.min,
    "title": lambda b: (b["title"] or "").lower(),
    "added": lambda b: b["added"] or datetime.min,
    "minutes": lambda b: b["minutes"],
    "finished": lambda b: b["finished"] or datetime.min,
    "annotations": lambda b: b["annotations"],
}


def _library_books(db: Session, source: str, device: List[str] = ()) -> tuple:
    """(every row, the rows of the devices chosen, the devices, the books
    of those rows). Devices come from all rows so a colour never moves."""
    all_rows = crud.get_library_rows(db, source, stats.LIBRARY_ROW_TYPES)
    devices = stats.reading_devices(all_rows)
    device = _choices(devices, list(device))
    rows = stats.filter_reading_rows(all_rows, device)
    return all_rows, devices, device, stats.build_library(rows, crud.get_titles_by_asin(db, source))


def _filter_books(books: list, status: Optional[str], kind: Optional[str], q: Optional[str]) -> list:
    if status in stats.LIBRARY_STATUSES:
        books = [b for b in books if b["status"] == status]
    if kind in stats.LIBRARY_KINDS:
        books = [b for b in books if b["kind"] == kind]
    if q:
        needle = q.lower()
        books = [b for b in books if needle in (b["title"] or "").lower() or needle in (b["author"] or "").lower()]
    return books


@router.get("/library", response_class=HTMLResponse)
def library(
    source: str, request: Request,
    status: Optional[str] = None, kind: Optional[str] = None, q: Optional[str] = None,
    sort: str = "activity", direction: str = "desc", page: int = 1, device: List[str] = Query([]),
    db: Session = Depends(get_db),
):
    """The whole history, no period: a catalogue of every book, read or
    not -- "what did I read in 2025" is the reading tab's."""
    ctx = _page(request, db, source, "library")
    if ctx is None:
        return RedirectResponse(f"/p/{source}")
    sort = sort if sort in LIBRARY_SORTS else "activity"
    direction = direction if direction in ("asc", "desc") else "desc"
    kind = kind if kind in stats.LIBRARY_KINDS else None

    rows, devices, device, books = _library_books(db, source, device)
    years = sorted({row[1].year for row in rows if row[1] is not None and row[2] in stats.LIBRARY_ITEM_TYPES})
    granularity = "year" if years and years[-1] - years[0] >= 3 else "month"
    status_counts = Counter(b["status"] for b in books)

    selected = _filter_books(books, status, kind, q)
    selected.sort(key=LIBRARY_SORTS[sort], reverse=direction == "desc")
    page_count = max(1, -(-len(selected) // LIBRARY_PAGE_SIZE))
    page = min(max(1, page), page_count)
    start = (page - 1) * LIBRARY_PAGE_SIZE
    shown = selected[start:start + LIBRARY_PAGE_SIZE]
    for book in shown:
        # Every item of the book, whatever the filters of this page.
        book["panel_url"] = f"/p/{source}/panel?" + urlencode(
            [("item_type", t) for t in stats.LIBRARY_ROW_TYPES] + [("context", book["context"])]
            + [("device", d) for d in device]
        )

    ctx.update({
        "books": shown,
        "book_count": len(selected),
        "totals": {
            "books": len(books),
            "in_library": sum(1 for b in books if b["kind"] != "other"),
            "annotations": sum(b["annotations"] for b in books),
            "annotated_books": sum(1 for b in books if b["annotations"]),
            **{key: status_counts.get(key, 0) for key in stats.LIBRARY_STATUSES},
        },
        "statuses": stats.LIBRARY_STATUSES,
        "kinds": stats.LIBRARY_KINDS,
        "arrivals": stats.build_library_arrivals(rows, granularity),
        "arrivals_granularity": granularity,
        "breakdown": stats.library_breakdown(books),
        "device_options": devices if len(devices) > 1 else [], "selected_devices": device,
        "status": status if status in stats.LIBRARY_STATUSES else None, "kind": kind, "q": q or "",
        "sort": sort, "direction": direction, "page": page, "page_count": page_count,
    })
    return templates.TemplateResponse(request, "platform/library.html", ctx)


@router.get("/library/panel", response_class=HTMLResponse)
def library_panel(
    source: str, request: Request, status: Optional[str] = None, kind: Optional[str] = None,
    device: List[str] = Query([]), db: Session = Depends(get_db),
):
    """The books of one status and kind (a segment of the breakdown chart),
    one line each: its library entry, or its latest session for a book read
    outside the library. Most recent activity first."""
    _all_rows, _devices, device, books = _library_books(db, source, device)
    rows = stats.filter_reading_rows(_all_rows, device)
    contexts = {b["context"] for b in _filter_books(books, status, kind, None)}
    lines: dict = {}
    for row_id, _ts, item_type, context, *_rest in reversed(rows):
        if context not in contexts or item_type not in ("reading_session", *stats.LIBRARY_ITEM_TYPES):
            continue
        current = lines.get(context)
        if current is None or (current[1] == "reading_session" and item_type != "reading_session"):
            lines[context] = (row_id, item_type)
    ids = [row_id for row_id, _type in lines.values()]
    return _items_panel_response(
        request, crud.get_items_by_ids(db, ids[:PANEL_LIMIT]), len(ids),
        open_url=url_with(request, f"/p/{source}/library", status=status, kind=kind, device=device),
        open_label="Voir ces livres dans la liste",
    )


# ────────────────────────────────────────────────
# Conversations (chat-style view over item_type == "message")
# ────────────────────────────────────────────────

def _message_extras(messages: List) -> tuple:
    """Per-message extras keyed by item id: parsed `fields` (reaction emoji,
    external attachment links, share text/link), and any Instagram post/reel
    links found in them -- lets the chat bubble show a reaction pill, a
    resolved-but-external attachment, and the same on-demand preview the
    item detail page offers, instead of a blank bubble."""
    fields_by_id = {}
    instagram_urls = {}
    for m in messages:
        if not m.fields_json:
            continue
        fields = json.loads(m.fields_json)
        fields_by_id[m.id] = fields
        urls = find_instagram_post_urls(fields.values())
        if urls:
            instagram_urls[m.id] = urls
    return fields_by_id, instagram_urls


@router.get("/conversations", response_class=HTMLResponse)
def conversations(source: str, request: Request, context: Optional[str] = None, db: Session = Depends(get_db)):
    ctx = _page(request, db, source, "conversations")
    if ctx is None:
        return RedirectResponse(f"/p/{source}")

    messages: list = []
    has_more = False
    if context is not None:
        # Only the most recent page loads up front -- a long conversation
        # (thousands of messages) used to render its entire history at
        # once, which is what made this page slow to open.
        messages, has_more = crud.get_thread_message_page(db, source, context, limit=MESSAGE_PAGE_SIZE)

    message_fields, message_instagram_urls = _message_extras(messages)
    ctx.update({
        "threads": crud.get_threads(db, source), "me": crud.get_me_candidate(db, source),
        "selected_context": context, "messages": messages,
        "has_more": has_more, "next_offset": MESSAGE_PAGE_SIZE,
        "message_fields": message_fields, "message_instagram_urls": message_instagram_urls,
    })
    return templates.TemplateResponse(request, "platform/conversations.html", ctx)


@router.get("/conversations/messages", response_class=HTMLResponse)
def conversation_older_messages(
    source: str, request: Request, context: Optional[str] = None,
    offset: int = 0, db: Session = Depends(get_db),
):
    """HTMX partial: the next page of older messages, prepended to the chat
    window when the user asks to load them, instead of fetching the whole
    thread up front."""
    messages, has_more = crud.get_thread_message_page(db, source, context, offset=offset, limit=MESSAGE_PAGE_SIZE)
    message_fields, message_instagram_urls = _message_extras(messages)
    return templates.TemplateResponse(
        request, "platform/_message_page.html",
        {
            "platform": platforms.get(source), "messages": messages,
            "me": crud.get_me_candidate(db, source), "selected_context": context, "has_more": has_more,
            "next_offset": offset + MESSAGE_PAGE_SIZE,
            "message_fields": message_fields, "message_instagram_urls": message_instagram_urls,
        },
    )


# ────────────────────────────────────────────────
# Messaging (Facebook / Instagram): who you talk with, how much, how fast
# ────────────────────────────────────────────────

@router.get("/messaging", response_class=HTMLResponse)
def messaging(
    source: str, request: Request,
    granularity: str = "month", date_from: Optional[str] = None, date_to: Optional[str] = None,
    threads: Optional[str] = None, include_1970: bool = False,
    db: Session = Depends(get_db),
):
    ctx = _page(request, db, source, "messaging", include_1970)
    if ctx is None:
        return RedirectResponse(f"/p/{source}")
    granularity = _granularity(granularity)
    threads = threads if threads in stats.THREAD_KINDS else None

    all_rows = stats.filter_threads(crud.get_message_rows(db, source, include_1970), threads)
    me = crud.get_me_candidate(db, source)
    dashboard = stats.build_messaging_dashboard(
        all_rows, me, granularity, parse_date(date_from), parse_date(date_to, end_of_day=True),
    )
    medians = dashboard["reply_medians"]

    ctx.update(_period_context(date_from, date_to, _years_of(all_rows)))
    ctx.update({
        "granularity": granularity,
        "thread_kind": threads,
        "thread_kinds": stats.THREAD_KINDS,
        "dashboard": dashboard,
        "me": me,
        "reply_median_sent": stats.format_delay(medians["sent"]),
        "reply_median_received": stats.format_delay(medians["received"]),
        "reply_window_hours": int(stats.REPLY_WINDOW.total_seconds() // 3600),
        "weekday_names": stats.WEEKDAY_NAMES_FR,
    })
    return templates.TemplateResponse(request, "platform/messaging.html", ctx)


@router.get("/messaging/panel", response_class=HTMLResponse)
def messaging_panel(
    source: str, request: Request,
    direction: Optional[str] = None, context: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    weekday: Optional[int] = None, hour: Optional[int] = None,
    reply_band: Optional[int] = None, new_threads: bool = False, overview: bool = False,
    threads: Optional[str] = None, include_1970: bool = False,
    db: Session = Depends(get_db),
):
    """Messages behind a messaging chart: by direction (sent/received) and/or
    conversation, over a period or a weekday x hour cell -- plain filters --
    or a reply-delay band / the conversations started in a period, which
    are worked out from the message sequence instead. `overview` lists the
    conversations of the period instead, each one opening its messages.
    `threads` is the page's private / group conversations filter."""
    me = crud.get_me_candidate(db, source)
    period_from, period_to = parse_date(date_from), parse_date(date_to, end_of_day=True)
    direction = direction if direction in stats.MESSAGE_DIRECTIONS else None
    threads = threads if threads in stats.THREAD_KINDS else None

    if overview:
        all_rows = stats.filter_threads(crud.get_message_rows(db, source, include_1970), threads)
        # Same query string as the messages panel, so each row opens the
        # messages of its conversation with the period and filters kept.
        shared = {"date_from": date_from, "date_to": date_to, "threads": threads,
                  "include_1970": "1" if include_1970 else None}
        rows = stats.period_threads(all_rows, me, direction, period_from, period_to)
        for row in rows:
            row["url"] = _messages_panel_url(source, {**shared, "direction": direction, "context": row["context"]}) \
                if row["context"] else None
        return templates.TemplateResponse(request, "platform/_panel_threads.html", {
            "threads": rows, "direction": direction,
            "all_url": _messages_panel_url(source, {**shared, "direction": direction}),
            "direction_label": stats.MESSAGE_DIRECTIONS[direction][0].lower() if direction else None,
        })

    if reply_band is not None or new_threads:
        all_rows = stats.filter_threads(crud.get_message_rows(db, source, include_1970), threads)
        if new_threads:
            ids = stats.select_first_messages(all_rows, period_from, period_to)
        else:
            ids = stats.select_replies(all_rows, me, reply_band, direction, period_from, period_to)
        return _items_panel_response(request, crud.get_items_by_ids(db, ids[:PANEL_LIMIT]), len(ids))

    if direction == "sent" and me is None:
        return _items_panel_response(request, [], 0)
    items, total = crud.search_items(
        db, page=1, page_size=PANEL_LIMIT, source=source, item_type="message", context=context,
        date_from=period_from, date_to=period_to, weekday=weekday, hour=hour,
        author=me if direction == "sent" else None, not_author=me if direction == "received" else None,
        group_chat=None if threads is None else threads == "group", include_1970=include_1970,
    )
    open_url = f"/p/{source}/conversations?" + urlencode({"context": context}) if context else None
    return _items_panel_response(request, items, total, open_url=open_url, open_label="Ouvrir la conversation")


def _messages_panel_url(source: str, params: dict) -> str:
    return f"/p/{source}/messaging/panel?" + urlencode({k: v for k, v in params.items() if v not in (None, "")})


# ────────────────────────────────────────────────
# Network (Facebook friends, Instagram followers / following)
# ────────────────────────────────────────────────

def _network_snapshot(db: Session, source: str, include_1970: bool = False):
    rows = crud.get_connection_rows(db, source, include_1970=include_1970)
    batches = crud.get_batches_by_item_type(db, source, stats.NETWORK_ITEM_TYPES)
    return rows, stats.network_snapshot(rows, batches)


@router.get("/network", response_class=HTMLResponse)
def network(
    source: str, request: Request,
    granularity: str = "month", date_from: Optional[str] = None, date_to: Optional[str] = None,
    include_1970: bool = False,
    db: Session = Depends(get_db),
):
    ctx = _page(request, db, source, "network", include_1970)
    if ctx is None:
        return RedirectResponse(f"/p/{source}")
    granularity = _granularity(granularity)

    rows, snapshot = _network_snapshot(db, source, include_1970)
    dashboard = stats.build_network_dashboard(
        rows, snapshot, granularity, parse_date(date_from), parse_date(date_to, end_of_day=True),
    )
    # A list is as of the export it comes from; the latest dated item of
    # that import is the closest thing to the export date it carries.
    as_of = {batch_id: cov["last"] for batch_id, cov in crud.get_batch_coverage(db, source).items()}
    groups = stats.reciprocity(snapshot)

    ctx.update(_period_context(date_from, date_to, _years_of(rows)))
    ctx.update({
        "granularity": granularity,
        "dashboard": dashboard,
        "roles": [
            {
                "role": role, "label": info["label"], "item_type": info["item_type"],
                "count": len(info["members"]), "undated": info["undated"],
                "batch_id": info["batch_id"], "as_of": as_of.get(info["batch_id"]),
                "lost": len(info["lost"]) if info["lost"] is not None else None,
                "lost_label": stats.LOST_LABELS[role],
                "previous_as_of": as_of.get(info["previous_batch_id"]),
            }
            for role, info in snapshot.items()
        ],
        "reciprocity": [
            {"group": group, "label": stats.RECIPROCITY_GROUPS[group], "count": len(people)}
            for group, people in groups.items()
        ] if groups else None,
        "following_count": len(snapshot["following"]["members"]) if groups else 0,
        "companies": stats.network_companies(snapshot),
        "network_categories": stats.NETWORK_CATEGORIES,
    })
    return templates.TemplateResponse(request, "platform/network.html", ctx)


@router.get("/network/panel", response_class=HTMLResponse)
def network_panel(
    source: str, request: Request, group: str, role: Optional[str] = None, company: Optional[str] = None,
    include_1970: bool = False,
    db: Session = Depends(get_db),
):
    """Accounts behind a network figure: a reciprocity group, the accounts
    that left one of the lists since an earlier export (most recently seen
    first), or the connections working at one company."""
    _rows, snapshot = _network_snapshot(db, source, include_1970)
    people: list = []
    if group == "company" and company:
        people = stats.sort_people(stats.company_people(snapshot, company))
    elif group == "lost":
        lost = snapshot.get(role, {}).get("lost") or {}
        people = sorted(lost.values(), key=lambda p: p["name"].lower())
        people.sort(key=lambda p: p["batch_id"], reverse=True)
    elif group in stats.RECIPROCITY_GROUPS:
        people = stats.sort_people((stats.reciprocity(snapshot) or {}).get(group, []))
    as_of = {batch_id: cov["last"] for batch_id, cov in crud.get_batch_coverage(db, source).items()}
    return templates.TemplateResponse(
        request, "platform/_panel_people.html",
        {"people": people[:PEOPLE_PANEL_LIMIT], "total": len(people), "limit": PEOPLE_PANEL_LIMIT,
         "lost": group == "lost", "as_of": as_of},
    )


# ────────────────────────────────────────────────
# Career (LinkedIn): positions, studies, endorsements, recommendations
# ────────────────────────────────────────────────

def _career_list(rows: list, item_types, date_from: Optional[datetime] = None,
                 date_to: Optional[datetime] = None) -> List[dict]:
    """Records of the given types, most recent first, as plain dicts."""
    selected = [r for r in stats.in_period(rows, date_from, date_to) if r[2] in item_types]
    selected.sort(key=lambda r: (r[1] is not None, r[1] or datetime.min), reverse=True)
    return [
        {"id": id_, "timestamp": ts, "item_type": item_type, "title": title, "author": author, "context": context,
         "fields": json.loads(fields_json) if fields_json else {}}
        for id_, ts, item_type, title, author, context, fields_json in selected
    ]


@router.get("/career", response_class=HTMLResponse)
def career(
    source: str, request: Request,
    granularity: str = "year", date_from: Optional[str] = None, date_to: Optional[str] = None,
    include_1970: bool = False,
    db: Session = Depends(get_db),
):
    ctx = _page(request, db, source, "career", include_1970)
    if ctx is None:
        return RedirectResponse(f"/p/{source}")
    granularity = _granularity(granularity, "year")
    period_from, period_to = parse_date(date_from), parse_date(date_to, end_of_day=True)

    rows = crud.get_career_rows(db, source, stats.CAREER_ITEM_TYPES, include_1970)
    timeline = stats.build_career_timeline(rows, datetime.utcnow())
    dashboard = stats.build_career_dashboard(rows, granularity, period_from, period_to)
    # The period presets come from what the period acts on: endorsements,
    # recommendations and courses -- not from the start of a degree.
    dated = [r for r in rows if r[2] not in stats.CAREER_PERIODS and r[2] != "certification"]

    ctx.update(_period_context(date_from, date_to, _years_of(dated)))
    ctx.update({
        "granularity": granularity,
        "timeline": timeline,
        "experience_label": stats.format_months(timeline["experience_months"]) if timeline else None,
        "dashboard": dashboard,
        "certifications": _career_list(rows, ("certification",)),
        "recommendations": _career_list(rows, stats.RECOMMENDATION_TYPES, period_from, period_to),
    })
    return templates.TemplateResponse(request, "platform/career.html", ctx)


# ────────────────────────────────────────────────
# Posts (Facebook / Instagram)
# ────────────────────────────────────────────────

def _media_options(rows: list, media_by_post: dict) -> List[dict]:
    counts = stats.post_kind_counts(rows, media_by_post)
    return [{"value": k, "label": label, "count": counts[k]} for k, (label, _slot) in stats.POST_KINDS.items() if counts[k]]


@router.get("/posts", response_class=HTMLResponse)
def posts(
    source: str, request: Request,
    granularity: str = "month", date_from: Optional[str] = None, date_to: Optional[str] = None,
    media: List[str] = Query([]), include_1970: bool = False,
    db: Session = Depends(get_db),
):
    ctx = _page(request, db, source, "posts", include_1970)
    if ctx is None:
        return RedirectResponse(f"/p/{source}")
    granularity = _granularity(granularity)

    post_media = crud.get_post_media(db, source)
    all_rows = crud.get_post_rows(db, source, include_1970)
    media_options = _media_options(all_rows, post_media)
    media = _choices(media_options, media)
    all_rows = stats.filter_posts(all_rows, post_media, media)
    rows = stats.in_period(all_rows, parse_date(date_from), parse_date(date_to, end_of_day=True))
    dashboard = stats.build_posts_dashboard(rows, post_media, granularity)

    ctx.update(_period_context(date_from, date_to, _years_of(all_rows)))
    ctx.update({
        "granularity": granularity,
        "media_options": media_options,
        "selected_media": media,
        "dashboard": dashboard,
        "weekday_names": stats.WEEKDAY_NAMES_FR,
    })
    return templates.TemplateResponse(request, "platform/posts.html", ctx)


@router.get("/posts/panel", response_class=HTMLResponse)
def posts_panel(
    source: str, request: Request,
    kind: Optional[str] = None, month: Optional[int] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    weekday: Optional[int] = None, hour: Optional[int] = None,
    media: List[str] = Query([]), include_1970: bool = False,
    db: Session = Depends(get_db),
):
    """Posts behind a chart slice (`kind`, `month`, a weekday x hour cell)
    -- `media` is the page's own media filter, the slice is within it."""
    kind = kind if kind in stats.POST_KINDS else None
    post_media = crud.get_post_media(db, source)
    rows = stats.in_period(
        stats.filter_posts(crud.get_post_rows(db, source, include_1970), post_media, media),
        parse_date(date_from), parse_date(date_to, end_of_day=True),
    )
    ids = stats.select_posts(rows, post_media, kind, month, weekday, hour)
    # The search can't narrow by media kind or calendar month: offered only
    # for a plain period, where it shows exactly the same posts.
    browse_url = None
    if kind is None and not media and month is None and weekday is None and hour is None:
        browse_url = url_with(request, "/browse", source=source, item_type="post", include_1970=None)
    return _items_panel_response(request, crud.get_items_by_ids(db, ids[:PANEL_LIMIT]), len(ids), browse_url)
