"""Register preference, source, collector and statistics use cases with strict DTOs."""

from htd.application.collection.commands import CollectionCommands
from htd.application.preferences import Preferences
from htd.application.statistics import Statistics
from htd.domain.common.time import parse_instant
from htd.domain.statistics.analysis import AnalysisQuery
from htd.domain.statistics.window import Window
from htd.domain.usage.model import Harness
from htd.ipc.dto.base import EmptyRequest, WriteRequest
from htd.ipc.dto.collection import (
    AutoDetectRequest,
    CollectionStatusRequest,
    CollectionStatusResponse,
    ScanNowResponse,
    SetPathRequest,
    SetPausedRequest,
    SetPausedResponse,
    SourcePathRequest,
    SourcesResponse,
    ValidatePathResponse,
)
from htd.ipc.dto.preferences import PreferencesResponse, PreferencesUpdateRequest
from htd.ipc.dto.statistics import (
    AnalysisRequest,
    AnalysisResponse,
    AuxiliaryRequest,
    AuxiliaryResponse,
    CacheMonitorRequest,
    CacheMonitorResponse,
    LongTermResponse,
    SummaryRequest,
    SummaryResponse,
)
from htd.ipc.router import Router


def register_commands(
    router: Router, preferences: Preferences, collection: CollectionCommands, statistics: Statistics
) -> None:
    @router.register("preferences.get", request=EmptyRequest, response=PreferencesResponse, kind="read")
    def get_preferences(request: EmptyRequest) -> PreferencesResponse:
        snapshot = preferences.get()
        return PreferencesResponse.model_validate(
            {"preferences": snapshot.values, "revision": snapshot.revision}
        )

    @router.register(
        "preferences.update", request=PreferencesUpdateRequest, response=PreferencesResponse, kind="write"
    )
    def update_preferences(request: PreferencesUpdateRequest) -> PreferencesResponse:
        return PreferencesResponse.model_validate(
            preferences.update(
                request.patch.model_dump(mode="json", exclude_unset=True),
                request.expectedRevision,
                request.operationId,
            )
        )

    @router.register("sources.list", request=EmptyRequest, response=SourcesResponse, kind="read")
    def list_sources(request: EmptyRequest) -> SourcesResponse:
        return SourcesResponse.model_validate(collection.list_sources())

    @router.register("sources.setPath", request=SetPathRequest, response=SourcesResponse, kind="write")
    def set_path(request: SetPathRequest) -> SourcesResponse:
        return SourcesResponse.model_validate(
            collection.configure(
                request.operationId,
                request.expectedRevision,
                harness=Harness(request.harness),
                path=request.path,
            )
        )

    @router.register("sources.autoDetect", request=AutoDetectRequest, response=SourcesResponse, kind="write")
    def auto_detect(request: AutoDetectRequest) -> SourcesResponse:
        return SourcesResponse.model_validate(
            collection.configure(request.operationId, request.expectedRevision)
        )

    @router.register(
        "sources.validatePath", request=SourcePathRequest, response=ValidatePathResponse, kind="read"
    )
    def validate_path(request: SourcePathRequest) -> ValidatePathResponse:
        return ValidatePathResponse.model_validate(
            collection.validate_path(Harness(request.harness), request.path)
        )

    @router.register(
        "collection.status", request=CollectionStatusRequest, response=CollectionStatusResponse, kind="read"
    )
    def status(request: CollectionStatusRequest) -> CollectionStatusResponse:
        return CollectionStatusResponse.model_validate(collection.status(details=request.details))

    @router.register("collection.scanNow", request=WriteRequest, response=ScanNowResponse, kind="write")
    def scan_now(request: WriteRequest) -> ScanNowResponse:
        return ScanNowResponse.model_validate(collection.scan_now(request.operationId))

    @router.register(
        "collection.setPaused", request=SetPausedRequest, response=SetPausedResponse, kind="write"
    )
    def set_paused(request: SetPausedRequest) -> SetPausedResponse:
        return SetPausedResponse.model_validate(collection.set_paused(request.paused, request.operationId))

    @router.register("stats.analytics", request=AnalysisRequest, response=AnalysisResponse, kind="read")
    def analytics(request: AnalysisRequest) -> AnalysisResponse:
        return AnalysisResponse.model_validate(
            statistics.analytics(
                AnalysisQuery(
                    harness=request.harness,
                    period=request.period,
                    year=request.year,
                    end_year=request.endYear,
                    quarter=request.quarter,
                    start=request.start,
                    end=request.end,
                    model_key=request.modelKey,
                    offset=request.offset,
                    limit=request.limit,
                )
            )
        )

    @router.register("stats.longTerm", request=AuxiliaryRequest, response=LongTermResponse, kind="read")
    def long_term(request: AuxiliaryRequest) -> LongTermResponse:
        Window(parse_instant(request.start), parse_instant(request.end), 0)
        return LongTermResponse.model_validate(
            statistics.long_term(parse_instant(request.start), parse_instant(request.end), request.harness)
        )

    @router.register(
        "stats.auxiliaryCosts", request=AuxiliaryRequest, response=AuxiliaryResponse, kind="read"
    )
    def auxiliary(request: AuxiliaryRequest) -> AuxiliaryResponse:
        Window(parse_instant(request.start), parse_instant(request.end), 0)
        return AuxiliaryResponse.model_validate(
            statistics.auxiliary(parse_instant(request.start), parse_instant(request.end), request.harness)
        )

    @router.register("stats.summary", request=SummaryRequest, response=SummaryResponse, kind="read")
    def summary(request: SummaryRequest) -> SummaryResponse:
        result, revision = statistics.summary(
            "all" if request.harness == "all" else Harness(request.harness),
            Window(
                parse_instant(request.start), parse_instant(request.end), request.offsetMinutes, request.grain
            ),
            sections=None if request.sections is None else frozenset(request.sections),
            model_key=request.modelKey,
        )
        return SummaryResponse.model_validate(result.to_dict() | {"revision": revision})

    @router.register(
        "stats.cacheMonitor", request=CacheMonitorRequest, response=CacheMonitorResponse, kind="read"
    )
    def cache_monitor(request: CacheMonitorRequest) -> CacheMonitorResponse:
        return CacheMonitorResponse.model_validate(
            statistics.cache_monitor(Harness(request.harness), request.mode, request.offsetMinutes)
        )
