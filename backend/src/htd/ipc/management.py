"""Register pricing, model, wallet and task contracts on the existing router."""

from collections.abc import Callable

from htd.application.pricing import Pricing
from htd.application.pricing.models import Models
from htd.application.pricing.scan import ModelScan
from htd.application.tasks import Tasks
from htd.application.wallet import Wallet
from htd.application.wallet.scan import ProviderScan
from htd.domain.pricing.model import EditorHints
from htd.domain.pricing.model_pool import fill_by_multiplier
from htd.ipc.dto import pricing as p
from htd.ipc.dto import tasks as t
from htd.ipc.dto import wallet as w
from htd.ipc.dto.base import EmptyRequest, WriteRequest
from htd.ipc.router import Router


def register_management(
    router: Router,
    pricing: Pricing,
    models: Models,
    wallet: Wallet,
    scan: ModelScan,
    tasks: Tasks,
    providers: ProviderScan | None = None,
) -> None:
    @router.register("pricing.catalog", request=EmptyRequest, response=p.CatalogResponse, kind="read")
    def catalog(request: EmptyRequest) -> p.CatalogResponse:
        return p.CatalogResponse.model_validate(pricing.catalog())

    @router.register(
        "pricing.previewSwitch", request=p.PreviewSwitchRequest, response=p.ChangeResponse, kind="read"
    )
    def preview(request: p.PreviewSwitchRequest) -> p.ChangeResponse:
        return p.ChangeResponse.model_validate(pricing.preview(request.model_dump()))

    def price_handler(command: str) -> Callable[[p.PricingWrite], p.ChangeResponse]:
        def handle(request: p.PricingWrite) -> p.ChangeResponse:
            prices = (
                tuple(m.price() for m in request.models)
                if isinstance(request, p.SaveVersionPricesRequest)
                else ()
            )
            hints = (
                tuple(EditorHints(request.versionId, h.modelId, h.multiplier) for h in request.hints)
                if isinstance(request, p.SaveVersionPricesRequest)
                else ()
            )
            return p.ChangeResponse.model_validate(
                pricing.change(
                    command,
                    request.model_dump(exclude={"operationId"}),
                    request.operationId,
                    models=prices,
                    hints=hints,
                )
            )

        return handle

    for command, dto in (
        ("pricing.createGroup", p.CreateGroupRequest),
        ("pricing.renameGroup", p.RenameGroupRequest),
        ("pricing.setGroupDisabled", p.SetGroupDisabledRequest),
        ("pricing.deleteGroup", p.GroupRequest),
        ("pricing.addVersion", p.AddVersionRequest),
        ("pricing.setVersionLabel", p.SetVersionLabelRequest),
        ("pricing.setVersionDisabled", p.SetVersionDisabledRequest),
        ("pricing.deleteVersion", p.VersionRequest),
        ("pricing.activateVersion", p.ActivateVersionRequest),
        ("pricing.saveVersionPrices", p.SaveVersionPricesRequest),
        ("pricing.switchCurrent", p.SwitchCurrentRequest),
    ):
        router.register(command, request=dto, response=p.ChangeResponse, kind="write")(price_handler(command))

    @router.register("models.pool", request=EmptyRequest, response=p.PoolResponse, kind="read")
    def pool(request: EmptyRequest) -> p.PoolResponse:
        return p.PoolResponse.model_validate(models.pool())

    def model_handler(command: str) -> Callable[[p.PricingWrite], p.ChangeResponse]:
        def handle(request: p.PricingWrite) -> p.ChangeResponse:
            bases = (
                tuple((m.modelId, m.rates.domain()) for m in request.models)
                if isinstance(request, p.SaveBaseRatesRequest)
                else ()
            )
            return p.ChangeResponse.model_validate(
                models.change(
                    command, request.model_dump(exclude={"operationId"}), request.operationId, bases
                )
            )

        return handle

    for command, dto in (
        ("models.add", p.AddModelsRequest),
        ("models.remove", p.RemoveModelRequest),
        ("models.restore", p.AddModelsRequest),
        ("models.saveBaseRates", p.SaveBaseRatesRequest),
    ):
        router.register(command, request=dto, response=p.ChangeResponse, kind="write")(model_handler(command))

    @router.register(
        "models.fillByMultiplier", request=p.FillByMultiplierRequest, response=p.Rates, kind="read"
    )
    def fill(request: p.FillByMultiplierRequest) -> p.Rates:
        rates = fill_by_multiplier(request.rates.domain(), request.multiplier)
        return p.Rates(
            input=rates.input, cache=rates.cache, output=rates.output, cacheWrite=rates.cache_write
        )

    @router.register("models.scan", request=p.ScanModelsRequest, response=t.TaskStartedResponse, kind="write")
    def start_scan(request: p.ScanModelsRequest) -> t.TaskStartedResponse:
        return t.TaskStartedResponse.model_validate(scan.start(request.harness, request.operationId))

    @router.register("wallet.list", request=w.WalletListRequest, response=w.WalletListResponse, kind="read")
    def wallet_list(request: w.WalletListRequest) -> w.WalletListResponse:
        return w.WalletListResponse.model_validate(wallet.list(request.includeArchived))

    if providers is not None:

        @router.register(
            "wallet.scanProviders", request=WriteRequest, response=t.TaskStartedResponse, kind="write"
        )
        def scan_providers(request: WriteRequest) -> t.TaskStartedResponse:
            return t.TaskStartedResponse.model_validate(providers.start(request.operationId))

        @router.register(
            "wallet.createScannedVendors",
            request=w.CreateScannedVendorsRequest,
            response=w.WalletChangeResponse,
            kind="write",
        )
        def create_providers(request: w.CreateScannedVendorsRequest) -> w.WalletChangeResponse:
            return w.WalletChangeResponse.model_validate(
                providers.create(
                    request.taskId, [p.model_dump() for p in request.providers], request.operationId
                )
            )

    @router.register("wallet.entries", request=w.EntriesRequest, response=w.EntriesResponse, kind="read")
    def entries(request: w.EntriesRequest) -> w.EntriesResponse:
        return w.EntriesResponse.model_validate(
            wallet.entries(
                request.vendorId, request.cursor, request.limit, request.kind, request.offsetMinutes
            )
        )

    @router.register(
        "wallet.previewReconcileInterval", request=w.IntervalRequest, response=w.IntervalPreview, kind="read"
    )
    def preview_interval(request: w.IntervalRequest) -> w.IntervalPreview:
        return w.IntervalPreview.model_validate(wallet.preview_interval(request.vendorId, request.intervalId))

    def wallet_handler(command: str) -> Callable[[WriteRequest], w.WalletChangeResponse]:
        def handle(request: WriteRequest) -> w.WalletChangeResponse:
            return w.WalletChangeResponse.model_validate(
                wallet.change(
                    command,
                    request.model_dump(exclude={"operationId"}, exclude_unset=True),
                    request.operationId,
                )
            )

        return handle

    @router.register(
        "wallet.accountEntries",
        request=w.AccountEntriesRequest,
        response=w.AccountEntriesResponse,
        kind="read",
    )
    def account_entries(request: w.AccountEntriesRequest) -> w.AccountEntriesResponse:
        return w.AccountEntriesResponse.model_validate(
            wallet.account_entries(
                request.vendorId,
                request.cursor,
                request.limit,
                request.scope,
                request.kind,
                request.startDay,
                request.endDay,
            )
        )

    @router.register(
        "wallet.dailyBook", request=w.DailyBookRequest, response=w.DailyBookResponse, kind="read"
    )
    def daily_book(request: w.DailyBookRequest) -> w.DailyBookResponse:
        return w.DailyBookResponse.model_validate(
            wallet.daily_book(request.vendorId, request.scope, request.month)
        )

    for command, dto in (
        ("wallet.createVendor", w.CreateVendorRequest),
        ("wallet.setLocalTotals", w.SetLocalTotalsRequest),
        ("wallet.correctEntry", w.CorrectEntryRequest),
        ("wallet.dismissNotice", w.DismissWalletNoticeRequest),
        ("wallet.setVisibility", w.WalletVisibilityRequest),
        ("wallet.renameVendor", w.RenameVendorRequest),
        ("wallet.topUp", w.TopUpRequest),
        ("wallet.calibrate", w.CalibrateRequest),
        ("wallet.reclassify", w.ReclassifyRequest),
        ("wallet.reconcileInterval", w.ReconcileIntervalRequest),
        ("wallet.correctConsumption", w.CorrectConsumptionRequest),
        ("wallet.linkGroups", w.LinkGroupsRequest),
        ("wallet.deleteVendor", w.VendorRequest),
        ("wallet.restoreVendor", w.VendorRequest),
    ):
        router.register(command, request=dto, response=w.WalletChangeResponse, kind="write")(
            wallet_handler(command)
        )

    @router.register("tasks.get", request=t.TaskRequest, response=t.TaskResponse, kind="read")
    def task_get(request: t.TaskRequest) -> t.TaskResponse:
        return t.TaskResponse.model_validate(tasks.get(request.taskId))

    @router.register("tasks.cancel", request=t.CancelTaskRequest, response=t.TaskResponse, kind="write")
    def task_cancel(request: t.CancelTaskRequest) -> t.TaskResponse:
        return t.TaskResponse.model_validate(tasks.cancel(request.taskId, request.operationId))
