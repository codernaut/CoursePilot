"""Route table shared by the authorization and CSRF tests."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    body: dict[str, Any] | None = None
    form: bool = False  # True: send as form data (HTML pages); False: JSON
    # Expected status per caller: anon, student, faculty, admin.
    expected: tuple[int, int, int, int] = (401, 403, 403, 200)
    tags: frozenset[str] = field(default_factory=frozenset)

    @property
    def state_changing(self) -> bool:
        return self.method not in ("GET", "HEAD", "OPTIONS")

    @property
    def id(self) -> str:
        return f"{self.method} {self.path}"


def route_table(ids: dict[str, str]) -> list[Route]:
    u, y, b, m = ids["user"], ids["year"], ids["block"], ids["module"]
    d = ids["document"]
    api = "/api/v1"
    anon_page = 303
    return [
        # JSON API
        Route("GET", f"{api}/users", expected=(401, 403, 403, 200)),
        Route(
            "POST",
            f"{api}/users",
            {"email": "new@example.edu", "display_name": "New", "role": "student"},
            expected=(401, 403, 403, 201),
        ),
        Route("GET", f"{api}/users/{u}", expected=(401, 403, 403, 200)),
        Route(
            "PATCH", f"{api}/users/{u}", {"display_name": "Renamed"}, expected=(401, 403, 403, 200)
        ),
        Route("POST", f"{api}/users/{u}/reset-password", expected=(401, 403, 403, 200)),
        Route("GET", f"{api}/structure", expected=(401, 200, 200, 200)),
        Route("PATCH", f"{api}/years/{y}", {"title": "Foundations"}, expected=(401, 403, 403, 200)),
        Route("PATCH", f"{api}/blocks/{b}", {"title": "Basics"}, expected=(401, 403, 403, 200)),
        Route("GET", f"{api}/modules/{m}", expected=(401, 200, 200, 200)),
        Route("PATCH", f"{api}/modules/{m}", {"title": "Algebra"}, expected=(401, 403, 200, 200)),
        Route("GET", f"{api}/modules/{m}/documents", expected=(401, 200, 200, 200)),
        Route("GET", f"{api}/documents/{d}", expected=(401, 200, 200, 200)),
        Route("PATCH", f"{api}/documents/{d}", {"title": "Renamed"}, expected=(401, 403, 200, 403)),
        Route("POST", f"{api}/documents/{d}/reindex", expected=(401, 403, 202, 403)),
        Route("GET", f"{api}/documents/{d}/download", expected=(401, 200, 200, 200)),
        Route("DELETE", f"{api}/documents/{d}", expected=(401, 403, 202, 403)),
        Route("GET", f"{api}/auth/me", expected=(401, 200, 200, 200)),
        Route("GET", f"{api}/openapi.json", expected=(401, 200, 200, 200)),
        # HTML pages (anonymous users are redirected to /login)
        Route("GET", "/", expected=(anon_page, 200, 200, 200)),
        Route("GET", "/structure", expected=(anon_page, 200, 200, 200)),
        Route("GET", "/change-password", expected=(anon_page, 200, 200, 200)),
        Route("GET", "/materials", expected=(anon_page, 200, 200, 200)),
        Route("GET", f"/modules/{m}/materials", expected=(anon_page, 200, 200, 200)),
        Route("GET", f"/modules/{m}/materials/documents", expected=(anon_page, 200, 200, 200)),
        Route(
            "POST",
            f"/modules/{m}/materials/{d}/reindex",
            form=True,
            expected=(anon_page, 403, 303, 403),
        ),
        Route(
            "POST",
            f"/modules/{m}/materials/{d}/delete",
            form=True,
            expected=(anon_page, 403, 303, 403),
        ),
        Route("GET", "/docs", expected=(anon_page, 200, 200, 200)),
        Route("GET", "/admin/users", expected=(anon_page, 403, 403, 200)),
        Route(
            "POST",
            "/admin/users",
            {"email": "page@example.edu", "display_name": "Page", "role": "student"},
            form=True,
            expected=(anon_page, 403, 403, 200),
        ),
        Route(
            "POST",
            f"/admin/users/{u}/role",
            {"role": "faculty"},
            form=True,
            expected=(anon_page, 403, 403, 200),
        ),
        Route(
            "POST",
            f"/admin/users/{u}/active",
            {"active": "0"},
            form=True,
            expected=(anon_page, 403, 403, 200),
        ),
        Route(
            "POST",
            f"/admin/users/{u}/reset-password",
            form=True,
            expected=(anon_page, 403, 403, 200),
        ),
        Route(
            "POST",
            f"/structure/years/{y}",
            {"title": "Y"},
            form=True,
            expected=(anon_page, 403, 403, 303),
        ),
        Route(
            "POST",
            f"/structure/blocks/{b}",
            {"title": "B"},
            form=True,
            expected=(anon_page, 403, 403, 303),
        ),
        Route(
            "POST",
            f"/structure/modules/{m}",
            {"title": "M", "description": "d"},
            form=True,
            expected=(anon_page, 403, 303, 303),
        ),
    ]


# State-changing routes that need an authenticated session (CSRF applies to these).
def csrf_routes(ids: dict[str, str]) -> list[Route]:
    api = "/api/v1"
    extra = [
        Route("POST", f"{api}/auth/logout"),
        Route(
            "POST",
            f"{api}/auth/change-password",
            {"current_password": "x" * 12, "new_password": "y" * 12},
        ),
        Route("POST", "/logout", form=True),
        Route(
            "POST",
            "/change-password",
            {"current_password": "x" * 12, "new_password": "y" * 12, "confirm_password": "y" * 12},
            form=True,
        ),
    ]
    return [r for r in route_table(ids) if r.state_changing] + extra
