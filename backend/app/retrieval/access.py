"""The access filter: the ONLY place the access predicate is built (CLAUDE.md sections 1 and 8).

The rule is: every authenticated, active ACCOUNT (admin, faculty, student) may search and read ALL
material; a GUEST (admitted by a temporary module access code, no account) may read only the
material of the one module its code was issued for, and only while that code is neither expired nor
revoked; nobody else may read anything. The predicate is a SQL fragment that the retrieval queries
put inside their ``WHERE`` clause, so rows a caller may not see are never produced (no
post-filtering in Python).
Per-module or per-document permissions can be added here later without touching any caller.

The context comes from the server-side session (``AccessContext.from_user``); it never comes from
request parameters or from the LLM.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from app.db.base import utcnow

GUEST_ROLE = "guest"  # the value of models.enums.Role.GUEST (no model imports in this module)


class _UserLike(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def is_active(self) -> bool: ...

    @property
    def role(self) -> Any: ...


@dataclass(frozen=True)
class AccessContext:
    """Who is asking. ``user_id`` is ``None`` for an anonymous caller."""

    user_id: str | None
    is_active: bool = False
    is_guest: bool = False

    @classmethod
    def from_user(cls, user: _UserLike | None) -> "AccessContext":
        if user is None:
            return cls.anonymous()
        return cls(
            user_id=user.id,
            is_active=bool(user.is_active),
            is_guest=str(getattr(user.role, "value", user.role)) == GUEST_ROLE,
        )

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


def _sql_timestamp(moment: datetime) -> str:
    """The format SQLAlchemy stores UTC datetimes in (naive UTC), comparable as text."""
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")


def access_filter(ctx: AccessContext, *, now: datetime | None = None) -> AccessFilter:
    """The predicate for chunks the caller may read, to be ANDed into a ``WHERE`` clause (the
    chunks table must be aliased ``c``).

    For an account it re-checks, inside the query, that the user row still exists and is active,
    so a user deactivated after the session was loaded is refused too. For a guest it also
    requires, inside the query, a guest account whose access code is not revoked and has not
    expired, and restricts the chunk to that code's module; the module comes from the database,
    never from the request.
    """
    if not ctx.may_read_material:
        return AccessFilter(DENY_ALL, {})
    if ctx.is_guest:
        moment = (now or utcnow()).replace(tzinfo=None)
        return AccessFilter(
            "EXISTS (SELECT 1 FROM users u_access "
            "JOIN guest_accounts ga_access ON ga_access.user_id = u_access.id "
            "JOIN access_codes ac_access ON ac_access.id = ga_access.access_code_id "
            "WHERE u_access.id = :access_user_id AND u_access.is_active = 1 "
            "AND u_access.role = 'guest' "
            "AND ac_access.revoked_at IS NULL AND ac_access.expires_at > :access_now "
            "AND ac_access.module_id = c.module_id)",
            {"access_user_id": ctx.user_id, "access_now": _sql_timestamp(moment)},
        )
    return AccessFilter(
        "EXISTS (SELECT 1 FROM users u_access WHERE u_access.id = :access_user_id "
        "AND u_access.is_active = 1 AND u_access.role != 'guest')",
        {"access_user_id": ctx.user_id},
    )
