from contextvars import ContextVar, Token


_mcp_actor: ContextVar[str] = ContextVar("mcp_actor", default="mcp-unknown")


def get_mcp_actor() -> str:
    return _mcp_actor.get()


def set_mcp_actor(actor: str) -> Token[str]:
    return _mcp_actor.set(actor)


def reset_mcp_actor(token: Token[str]) -> None:
    _mcp_actor.reset(token)
