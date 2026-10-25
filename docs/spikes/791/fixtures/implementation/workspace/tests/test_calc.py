from calc import mean


def test_mean():
    assert mean([1.0, 2.0, 3.0]) == 2.0


def test_mean_empty():
    assert mean([]) == 0.0
