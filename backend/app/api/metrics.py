"""
Metrics API (prompt §12).

One endpoint. The whole report is computed server-side rather than shipping
raw suggestions to the browser, because the interesting part is a comparison
across three tables and the client has no business re-deriving it — two
implementations of "was this accepted" would eventually disagree, and the
disagreement would be invisible.
"""

import logging

from fastapi import APIRouter, Depends, Query

from ..core import metrics
from ..core.auth import CurrentUser, get_current_user
from ..schemas.invoices import MetricsBucket, MetricsOut

log = logging.getLogger(__name__)

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("", response_model=MetricsOut)
def get_metrics(
    window_days: int = Query(default=90, ge=7, le=730),
    user: CurrentUser = Depends(get_current_user),
):
    """AI acceptance, by vendor, cost code, and confidence band.

    Authenticated but not role-gated, same posture as every other read in the
    app: a reviewer deciding how hard to look at a suggestion benefits from
    knowing how often this vendor's suggestions survive, and that is the
    question the page answers.
    """
    report = metrics.build_report(window_days=window_days)
    return MetricsOut(
        generated_at=report.generated_at,
        window_days=report.window_days,
        judged=report.judged,
        accepted=report.accepted,
        rate=report.rate,
        by_vendor=[_bucket(b) for b in report.by_vendor],
        by_cost_code=[_bucket(b) for b in report.by_cost_code],
        by_confidence=[_bucket(b) for b in report.by_confidence],
        unsuggested=report.unsuggested,
        notes=report.notes,
    )


def _bucket(b: metrics.Bucket) -> MetricsBucket:
    return MetricsBucket(
        key=b.key, label=b.label, total=b.total, accepted=b.accepted, rate=b.rate
    )
