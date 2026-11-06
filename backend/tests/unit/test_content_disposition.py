import pytest

from app.api.documents import content_disposition


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("report.pdf", "inline; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"),
        (
            'a"b\\c.pdf',
            "inline; filename=\"a_b_c.pdf\"; filename*=UTF-8''a%22b%5Cc.pdf",
        ),
        (
            "résumé.pdf",
            "inline; filename=\"r_sum_.pdf\"; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf",
        ),
        ("a b;c.pdf", "inline; filename=\"a b;c.pdf\"; filename*=UTF-8''a%20b%3Bc.pdf"),
    ],
)
def test_content_disposition_is_rfc6266_encoded(filename: str, expected: str) -> None:
    header = content_disposition("inline", filename)
    assert header == expected
    assert header.isascii()
    assert "\r" not in header and "\n" not in header


def test_header_injection_characters_are_neutralised() -> None:
    header = content_disposition("attachment", "x.pdf\r\nSet-Cookie: a=b")
    assert "\r" not in header and "\n" not in header
    assert "Set-Cookie: a=b" not in header.split("filename*=")[0]
