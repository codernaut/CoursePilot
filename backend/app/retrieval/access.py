"""The access filter: the ONLY place the access predicate is built (CLAUDE.md sections 1 and 8).

Today the rule is: every authenticated, active user may search and read ALL material, and nobody
else may read anything. The predicate is a SQL fragment that the retrieval queries put inside their
``WHERE`` clause, so rows a caller may not see are never produced (no post-filtering in Python).
Per-module or per-document permissions can be added here later without touching any caller.

The context comes from the server-side session (``AccessContext.from_user``); it never comes from
request parameters or from the LLM.
"""

from dataclasses import dataclass
from typing import Any, Protocol


class _UserLike(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def is_active(self) -> bool: ...


@dataclass(frozen=True)
class AccessContext:
    """Who is asking. ``user_id`` is ``None`` for an anonymous caller."""

    user_id: str | None
    is_active: bool = False

    @classmethod
    def from_user(cls, user: _UserLike | None) -> "AccessContext":
        if user is None:
            return cls.anonymous()
        return cls(user_id=user.id, is_active=bool(user.is_active))

    @classmethod
    def anonymous(cls) -> "AccessContext":
        return cls(user_id=None, is_active=False)

    @property
    def may_read_material(self) -> bool:
        return self.user_id is not None and self.is_active


@dataclass(frozen=True)
class AccessFilter:
    """A SQL boolean expression plus its bind parameters."""

    sql: str
    params: dict[str, Any]


# Matches nothing: anonymous and inactive callers retrieve no rows at all.
DENY_ALL = "0 = 1"


def access_filter(ctx: AccessContext) -> AccessFilter:
    """The predicate for chunks the caller may read, to be ANDed into a ``WHERE`` clause.

    For an allowed caller it re-checks, inside the query, that the user row still exists and is
    active, so a user deactivated after the session was loaded is refused too.
    """
    if not ctx.may_read_material:
        return AccessFilter(DENY_ALL, {})
    return AccessFilter(
        "EXISTS (SELECT 1 FROM users u_access WHERE u_access.id = :access_user_id "
        "AND u_access.is_active = 1)",
        {"access_user_id": ctx.user_id},
    )
