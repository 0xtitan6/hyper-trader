"""Protocol types for the Hyperliquid SDK surface we depend on.

Replacing `Any` with these makes mypy meaningful and documents the contract
without forcing a hard dependency on the concrete SDK classes (helpful for
testing).
"""

from collections.abc import Callable
from typing import Any, Protocol


class InfoProto(Protocol):
    base_url: str

    def meta(self) -> dict[str, Any]: ...
    def spot_meta(self) -> dict[str, Any]: ...
    def all_mids(self) -> dict[str, Any]: ...
    def user_state(self, address: str) -> dict[str, Any]: ...
    def user_fills(self, address: str) -> list[dict[str, Any]]: ...
    def post(self, url_path: str, payload: Any) -> Any: ...
    def subscribe(self, subscription: dict[str, Any], callback: Callable[..., Any]) -> int: ...


class ExchangeProto(Protocol):
    def order(
        self,
        coin: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
        order_type: dict[str, Any],
        reduce_only: bool = False,
    ) -> dict[str, Any]: ...
