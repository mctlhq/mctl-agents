from pkg import add


def test_add() -> None:
    if add(1, 2) != 3:
        raise ValueError("bad sum")
