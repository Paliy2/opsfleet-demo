import pytest

from opsfleet_agent.core import errors
from opsfleet_agent.core.errors import AgentError, ErrorCode

ALL_ERRORS = [
    cls
    for cls in vars(errors).values()
    if isinstance(cls, type) and issubclass(cls, AgentError) and cls is not AgentError
]


def test_every_error_code_has_exactly_one_error_class() -> None:
    codes = [cls.code for cls in ALL_ERRORS]

    assert sorted(codes) == sorted(ErrorCode)


@pytest.mark.parametrize("error_cls", ALL_ERRORS)
def test_user_message_never_leaks_detail(error_cls: type[AgentError]) -> None:
    error = error_cls("SELECT email FROM users")

    assert "email" not in error.user_message
    assert error.detail == "SELECT email FROM users"
