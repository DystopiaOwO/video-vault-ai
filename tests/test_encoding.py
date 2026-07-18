from scripts.check_encoding import main


def test_no_mojibake_in_source_files():
    assert main() == 0
