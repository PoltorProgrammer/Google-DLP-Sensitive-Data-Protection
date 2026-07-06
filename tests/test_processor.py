"""Tests for dlp_processor internals that don't require Google Cloud access."""
from dlp_processor import ClinicalDocumentProcessor, DEFAULT_INFO_TYPES, MAX_DICT_BYTES


def make_proc(log_sink=None):
    # Bypass __init__ so no Google clients (and no credentials) are needed
    proc = ClinicalDocumentProcessor.__new__(ClinicalDocumentProcessor)
    proc.log_callback = log_sink
    return proc


def test_default_infotypes_complete():
    names = [it["name"] for it in DEFAULT_INFO_TYPES]
    assert "PERSON_NAME" in names
    assert "GERMANY_SCHUFA_ID" in names
    assert "SWITZERLAND_SOCIAL_SECURITY_NUMBER" in names
    assert "AUSTRIA_SOCIAL_SECURITY_NUMBER" in names
    assert len(names) == 17


def test_inspect_config_always_uses_full_infotype_list():
    proc = make_proc()
    with_terms = proc._build_inspect_config(["Maria", "Garcia"])
    without_terms = proc._build_inspect_config(None)
    # Regression guard: translation re-redaction used to run with a reduced list
    assert len(with_terms["info_types"]) == len(DEFAULT_INFO_TYPES)
    assert len(without_terms["info_types"]) == len(DEFAULT_INFO_TYPES)
    assert "custom_info_types" not in without_terms


def test_custom_terms_expand_into_combinations():
    proc = make_proc()
    cfg = proc._build_inspect_config(["Maria", "Garcia"])
    words = cfg["custom_info_types"][0]["dictionary"]["word_list"]["words"]
    assert "Maria" in words
    assert "Maria Garcia" in words
    assert "Garcia Maria" in words
    assert "M.Garcia" in words
    assert "MariaGarcia" in words


def test_numeric_ids_are_not_recombined_into_names():
    proc = make_proc()
    cfg = proc._build_inspect_config(["Maria", "E1606731"])
    words = cfg["custom_info_types"][0]["dictionary"]["word_list"]["words"]
    assert "E1606731" in words
    assert "Maria E1606731" not in words


def test_fuzz_variations_cover_common_ocr_errors():
    proc = make_proc()
    variants = proc._generate_fuzz_variations("Smith")
    assert "Smlth" in variants
    assert "Sm1th" in variants


def test_term_dictionary_respects_size_limit(monkeypatch):
    proc = make_proc()
    originals = ["Maria", "Garcia"]
    huge = originals + [f"generatedvariant{i}" * 20 for i in range(2000)]
    monkeypatch.setattr(proc, "_generate_term_combinations", lambda terms: list(huge))

    cfg = proc._build_inspect_config(originals)
    words = cfg["custom_info_types"][0]["dictionary"]["word_list"]["words"]
    total_bytes = sum(len(w.encode("utf-8")) for w in words)
    assert total_bytes < MAX_DICT_BYTES
    # The user's original terms must always survive truncation
    assert set(originals) <= set(words)


def test_log_passes_structured_metadata():
    received = []
    proc = make_proc(lambda msg, meta=None: received.append((msg, meta)))
    proc.log("hello", metadata={"pages": 3})
    proc.log("plain")
    assert received == [("hello", {"pages": 3}), ("plain", None)]
