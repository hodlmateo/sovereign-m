from sovereign_m.desk.engine import DISCLAIMER
from sovereign_m.prove import prove


def test_prove_passes(capsys):
    assert prove() == 0
    out = capsys.readouterr().out
    assert out.startswith(DISCLAIMER)
    assert "FAIL" not in out
    assert "prove-it-works: all checks passed" in out
    assert "I don't know." in out
    assert "even days" in out
