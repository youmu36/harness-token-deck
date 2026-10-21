"""Model editor commands; the pool and pricing history share one revision scope."""

from typing import cast

from htd.application.command import execute, expected
from htd.application.ports import ModelsChanged, PricingStore, UnitOfWork
from htd.domain.common.time import Clock, Instant
from htd.domain.pricing.model import PriceRates
from htd.domain.pricing.model_pool import add_models, remove_model, set_base_price


class Models:
    def __init__(self, store: PricingStore, clock: Clock) -> None:
        self.store, self.clock = store, clock

    def pool(self) -> dict[str, object]:
        pool, revision = self.store.models()
        return {
            "models": [
                {
                    "modelId": name,
                    "rates": {
                        "input": rates.input,
                        "cache": rates.cache,
                        "output": rates.output,
                        "cacheWrite": rates.cache_write,
                    },
                }
                for name in pool.models
                for rates in [pool.base_for(name) or PriceRates()]
            ],
            "excluded": list(pool.excluded),
            "revision": revision,
        }

    def change(
        self,
        command: str,
        parameters: dict[str, object],
        operation_id: str,
        bases: tuple[tuple[str, PriceRates], ...] = (),
    ) -> dict[str, object]:
        def save(work: UnitOfWork, at: Instant) -> dict[str, object]:
            expected(work, "models", cast(int, parameters["expectedRevision"]))
            old, _ = self.store.models(work)
            match command:
                case "models.add" | "models.restore":
                    new = add_models(old, tuple(cast(list[str], parameters["modelIds"])))
                case "models.remove":
                    new = remove_model(old, cast(str, parameters["modelId"]))
                case "models.saveBaseRates":
                    new = old
                    for name, rates in bases:
                        new = set_base_price(new, name, rates)
                case _:
                    raise ValueError("Unknown model use case")
            self.store.save_models(work, old, new, at=at)
            if new != old:
                work.emit(ModelsChanged(work.revision("models")))
            return {"changed": new != old, "revision": work.revision("models")}

        return execute(self.store, self.clock, command, parameters, operation_id, save)
