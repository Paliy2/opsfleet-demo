from enum import StrEnum


class ErrorCode(StrEnum):
    SQL_SYNTAX = "E_SQL_SYNTAX"
    EMPTY_RESULT = "E_EMPTY_RESULT"
    SCOPE_DENIED = "E_SCOPE_DENIED"
    QUERY_REJECTED = "E_QUERY_REJECTED"
    COST_LIMIT = "E_COST_LIMIT"
    QUERY_TIMEOUT = "E_QUERY_TIMEOUT"
    LLM_TIMEOUT = "E_LLM_TIMEOUT"
    LLM_UNAVAILABLE = "E_LLM_UNAVAILABLE"
    LOOP_LIMIT = "E_LOOP_LIMIT"
    OFF_TOPIC = "E_OFF_TOPIC"
    PII_DETECTED = "E_PII_DETECTED"
    NOT_FOUND = "E_NOT_FOUND"
    CONFIRMATION_EXPIRED = "E_CONFIRMATION_EXPIRED"


class AgentError(Exception):
    code: ErrorCode
    user_message: str = "Something went wrong while processing your request."

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class SqlSyntaxError(AgentError):
    code = ErrorCode.SQL_SYNTAX
    user_message = "I couldn't build a valid query for that question."


class EmptyResultError(AgentError):
    code = ErrorCode.EMPTY_RESULT
    user_message = "No data matched that question."


class ScopeDeniedError(AgentError):
    code = ErrorCode.SCOPE_DENIED
    user_message = "That data is outside the brands you have access to."


class QueryRejectedError(AgentError):
    code = ErrorCode.QUERY_REJECTED
    user_message = "That query isn't allowed. Only read-only analysis is permitted."


class CostLimitError(AgentError):
    code = ErrorCode.COST_LIMIT
    user_message = "That query would scan too much data. Try narrowing it."


class QueryTimeoutError(AgentError):
    code = ErrorCode.QUERY_TIMEOUT
    user_message = "That query took too long. Try narrowing the time range or filters."


class LlmTimeoutError(AgentError):
    code = ErrorCode.LLM_TIMEOUT
    user_message = "The AI service is slow right now. Please try again."


class LlmUnavailableError(AgentError):
    code = ErrorCode.LLM_UNAVAILABLE
    user_message = "The AI service is temporarily unavailable. Please try again shortly."


class LoopLimitError(AgentError):
    code = ErrorCode.LOOP_LIMIT
    user_message = "I couldn't finish that analysis within my step budget. Try a narrower question."


class OffTopicError(AgentError):
    code = ErrorCode.OFF_TOPIC
    user_message = "I can only help with sales and performance analysis."


class PiiDetectedError(AgentError):
    code = ErrorCode.PII_DETECTED
    user_message = "The response was blocked because it contained personal data."


class NotFoundError(AgentError):
    code = ErrorCode.NOT_FOUND
    user_message = "Nothing matched that request."


class ConfirmationExpiredError(AgentError):
    code = ErrorCode.CONFIRMATION_EXPIRED
    user_message = "That confirmation has expired. Please ask again."
