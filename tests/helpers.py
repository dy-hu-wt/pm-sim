from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any


class FakeResponsesClient:
    def __init__(self, outputs: list[list[SimpleNamespace]]) -> None:
        self._outputs = outputs
        self.calls: list[dict[str, Any]] = []
        self.responses = self

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        output = self._outputs.pop(0) if self._outputs else []
        return SimpleNamespace(output=output, output_text="")


def function_call(call_id: str, name: str, arguments: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        type="function_call",
        call_id=call_id,
        name=name,
        arguments=json.dumps(arguments),
    )
