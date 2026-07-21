from pkg.service import format_value


def test_format_value() -> None:
    assert format_value("hello") == "HELLO"
