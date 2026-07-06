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


def test_location_chain_stays_in_jurisdiction():
    proc = make_proc()
    proc.project_id = "p"
    proc.allow_global_fallback = False

    proc.location = "europe-west6"
    assert proc._location_chain() == ["europe-west6", "europe"]
    proc.location = "us-central1"
    assert proc._location_chain() == ["us-central1", "us"]
    proc.location = "northamerica-northeast1"
    assert proc._location_chain() == ["northamerica-northeast1", "us"]
    proc.location = "europe"   # already the multi-region
    assert proc._location_chain() == ["europe"]
    proc.location = "global"
    assert proc._location_chain() == ["global"]

    # global only ever appears when explicitly allowed
    proc.allow_global_fallback = True
    proc.location = "europe-west6"
    assert proc._location_chain() == ["europe-west6", "europe", "global"]


def test_unsupported_location_error_detection():
    proc = make_proc()
    assert proc._is_unsupported_location_error(
        Exception('400 Image inspection is not supported in this location. [reason: "3"]'))
    assert not proc._is_unsupported_location_error(Exception("503 deadline exceeded"))


def test_location_fallback_resolves_and_caches():
    logs = []
    proc = make_proc(lambda msg, meta=None: logs.append(msg))
    proc.project_id = "p"
    proc.location = "europe-west6"
    proc.allow_global_fallback = False
    proc._resolved_parents = {}

    calls = []
    def fake_call(parent):
        calls.append(parent)
        if parent.endswith("europe-west6"):
            raise Exception("400 Image inspection is not supported in this location.")
        return "OK"

    assert proc._with_location_fallback("image", fake_call) == "OK"
    assert proc._resolved_parents["image"].endswith("/locations/europe")
    assert any("multi-region 'europe'" in m for m in logs)

    # second call goes straight to the cached parent
    calls.clear()
    assert proc._with_location_fallback("image", fake_call) == "OK"
    assert calls == ["projects/p/locations/europe"]


def test_location_fallback_exhausted_raises_clear_error():
    proc = make_proc(lambda msg, meta=None: None)
    proc.project_id = "p"
    proc.location = "europe-west6"
    proc.allow_global_fallback = False
    proc._resolved_parents = {}

    def always_unsupported(parent):
        raise Exception("400 Image inspection is not supported in this location.")

    try:
        proc._with_location_fallback("image", always_unsupported)
        raise AssertionError("should have raised")
    except RuntimeError as e:
        assert "allow_global_fallback" in str(e)


def test_log_passes_structured_metadata():
    received = []
    proc = make_proc(lambda msg, meta=None: received.append((msg, meta)))
    proc.log("hello", metadata={"pages": 3})
    proc.log("plain")
    assert received == [("hello", {"pages": 3}), ("plain", None)]
