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


class NeedsAnswer(ToolError):
    """Stops a tool call to ask the user: returned as a normal (non-error) result
    {"status": "needs_answer", "questions": [...], **data}. Nothing has been written."""

    def __init__(self, message: str, questions: list, **data) -> None:
        super().__init__(message, **data)
        self.questions = questions

    def to_dict(self) -> dict:
        out = {"status": "needs_answer", "message": self.message, "questions": self.questions}
        out.update(self.data)
        return out
