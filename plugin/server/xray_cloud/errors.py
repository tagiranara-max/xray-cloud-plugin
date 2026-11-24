from __future__ import annotations


class ToolError(Exception):
    """Error reported to the model as an isError tool result: {"error": message, **data}."""

    def __init__(self, message: str, **data) -> None:
        super().__init__(message)
        self.message = message
        self.data = data

    def to_dict(self) -> dict:
        out = {"error": self.message}
        out.update(self.data)
        return out
