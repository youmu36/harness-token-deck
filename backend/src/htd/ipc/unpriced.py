"""Pending-price recovery contracts, separate from ordinary 72-hour repricing."""

from htd.application.pricing.unpriced import Unpriced
from htd.ipc.dto import pricing as p
from htd.ipc.dto.base import EmptyRequest
from htd.ipc.router import Router


def register_unpriced(router: Router, unpriced: Unpriced) -> None:
    @router.register(
        "pricing.resolveUnpriced",
        request=p.ResolveUnpricedRequest,
        response=p.ResolveUnpricedResponse,
        kind="write",
    )
    def resolve(request: p.ResolveUnpricedRequest) -> p.ResolveUnpricedResponse:
        return p.ResolveUnpricedResponse.model_validate(
            unpriced.resolve(
                request.model_dump(mode="json", exclude={"operationId"}),
                request.operationId,
                model=request.model.price() if request.model else None,
            )
        )

    @router.register("pricing.unpricedNotice", request=EmptyRequest, response=p.UnpricedNotice, kind="read")
    def notice(request: EmptyRequest) -> p.UnpricedNotice:
        return p.UnpricedNotice.model_validate(unpriced.notice())

    @router.register("pricing.unpriced", request=EmptyRequest, response=p.UnpricedResponse, kind="read")
    def pending(request: EmptyRequest) -> p.UnpricedResponse:
        return p.UnpricedResponse.model_validate(unpriced.list())

    @router.register(
        "pricing.recalculateUnpriced",
        request=p.RecalculateUnpricedRequest,
        response=p.RecalculateUnpricedResponse,
        kind="write",
        timeout=300,
    )
    def recalculate(request: p.RecalculateUnpricedRequest) -> p.RecalculateUnpricedResponse:
        return p.RecalculateUnpricedResponse.model_validate(
            unpriced.recalculate(request.expectedPricingRevision, request.operationId)
        )
