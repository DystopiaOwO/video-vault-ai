from video_vault.encoding_check import find_encoding_issues, find_mojibake, repository_root, run_encoding_check


def test_repository_encoding_check_passes():
    assert run_encoding_check(repository_root()) == 0


def test_find_mojibake_and_skips_binary(tmp_path):
    clean = tmp_path / "clean.ts"
    clean.write_text("const label = '繁體中文';\n", encoding="utf-8")
    bad = tmp_path / "bad.md"
    bad.write_text("錯誤 token：\ufffd\n", encoding="utf-8")
    binary = tmp_path / "binary.py"
    binary.write_bytes(b"\x00\xef\xbf\xbd")

    assert find_mojibake(tmp_path) == [bad]


def test_missing_root_is_clean(tmp_path):
    assert find_mojibake(tmp_path / "does-not-exist") == []


def test_invalid_utf8_in_text_file_is_reported(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_bytes(b"print('ok')\xff\xfe")
    issues = find_encoding_issues(tmp_path)
    assert issues["invalid_utf8"] == [bad]
    assert run_encoding_check(tmp_path) == 1


def test_binary_with_nul_is_ignored_even_if_not_utf8(tmp_path):
    binary = tmp_path / "binary.py"
    binary.write_bytes(b"\x00\xff\xfe")
    assert find_encoding_issues(tmp_path) == {"mojibake": [], "invalid_utf8": []}
