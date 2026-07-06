"""Tests for the BatchEngine save pipeline, scan logic, settings and safety nets.
Each test runs in an isolated tmp cwd so no real config/history files are touched.
No network access: nothing here constructs a Google client."""
import os
import json

import fitz
import pytest

from batch_core import BatchEngine


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # No powershell/ping probes during tests
    monkeypatch.setattr(BatchEngine, "detect_environment", lambda self: None)
    return BatchEngine()


def pdf_bytes(text="Hello", pages=1):
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def write_pdf(path, text="Hello", pages=1):
    with open(path, "wb") as f:
        f.write(pdf_bytes(text, pages))


def drain(engine, wanted_type=None):
    events = engine.poll_events()
    if wanted_type:
        return [e for e in events if e["type"] == wanted_type]
    return events


# ---------------- config & posture ----------------

def test_config_defaults(engine):
    assert engine.config["google_cloud"]["location"] == "europe-west6"
    assert engine.config["app_settings"]["overwrite_policy"] == "skip"
    assert engine.config["app_settings"]["verification_scan"] is True
    assert engine.config["output_options"]["redaction"] is True


def test_posture_levels(engine):
    assert engine.posture()["level"] == "ok"
    engine.config["translation"]["enabled"] = True
    assert engine.posture()["level"] == "warn"
    engine.config["output_options"]["redaction"] = False
    assert engine.posture()["level"] == "danger"


def test_settings_roundtrip_persists_to_disk(engine):
    settings = engine.get_settings()
    assert "performance_metrics" not in settings["app_settings"]
    settings["output_options"]["redaction_iterations"] = 3
    settings["translation"]["enabled"] = True
    engine.save_settings(settings)

    with open("config.json", encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["output_options"]["redaction_iterations"] == 3
    assert on_disk["translation"]["enabled"] is True


def test_settings_validation_rejects_no_outputs(engine):
    settings = engine.get_settings()
    settings["output_options"]["selectable_text_copy"] = False
    settings["output_options"]["non_selectable_text_copy"] = False
    with pytest.raises(ValueError):
        engine.save_settings(settings)


def test_settings_validation_rejects_bad_policy(engine):
    settings = engine.get_settings()
    settings["app_settings"]["overwrite_policy"] = "yolo"
    with pytest.raises(ValueError):
        engine.save_settings(settings)


# ---------------- atomic save pipeline ----------------

def test_atomic_save_and_validation(engine, tmp_path):
    target = str(tmp_path / "anonymized_ok.pdf")
    engine.save_bytes_atomic(target, pdf_bytes())
    assert os.path.exists(target)
    assert not os.path.exists(target + ".part")
    assert engine.is_valid_output(target)


def test_garbage_and_empty_bytes_rejected(engine, tmp_path):
    target = str(tmp_path / "bad.pdf")
    with pytest.raises(Exception):
        engine.save_bytes_atomic(target, b"not a pdf")
    with pytest.raises(ValueError):
        engine.save_bytes_atomic(target, b"")
    assert not os.path.exists(target)


def test_page_count_mismatch_rejected(engine, tmp_path):
    """A PDF with fewer pages than the input must never be saved as done."""
    target = str(tmp_path / "anonymized_short.pdf")
    one_page = pdf_bytes(pages=1)
    with pytest.raises(ValueError, match="expected 2"):
        engine.save_bytes_atomic(target, one_page, expected_pages=2)
    assert not os.path.exists(target)
    # save_output converts the failure into a logged None
    assert engine.save_output(target, one_page, expected_pages=2) is None
    assert engine.save_output(target, one_page, expected_pages=1) == target


def test_corrupt_or_empty_outputs_are_invalid(engine, tmp_path):
    corrupt = tmp_path / "anonymized_corrupt.pdf"
    corrupt.write_bytes(b"%PDF-1.4 truncated")
    assert not engine.is_valid_output(str(corrupt))
    empty = tmp_path / "anonymized_empty.pdf"
    empty.touch()
    assert not engine.is_valid_output(str(empty))


def test_overwrite_policies(engine, tmp_path):
    target = str(tmp_path / "anonymized_doc.pdf")
    engine.save_bytes_atomic(target, pdf_bytes("v1"))

    engine.config["app_settings"]["overwrite_policy"] = "skip"
    assert engine.save_output(target, pdf_bytes("v2")) == target

    engine.config["app_settings"]["overwrite_policy"] = "version"
    versioned = engine.save_output(target, pdf_bytes("v2"))
    assert versioned != target and versioned.endswith("anonymized_doc (1).pdf")

    engine.config["app_settings"]["overwrite_policy"] = "overwrite"
    assert engine.save_output(target, pdf_bytes("v3")) == target


def test_unique_path(engine, tmp_path):
    p = tmp_path / "file.pdf"
    p.touch()
    assert BatchEngine.unique_path(str(p)).endswith("file (1).pdf")


# ---------------- folder scan ----------------

def test_scan_trusts_only_valid_outputs(engine, tmp_path):
    src = tmp_path / "data"
    out = src / "processed"
    out.mkdir(parents=True)
    write_pdf(src / "doc1.pdf")
    write_pdf(src / "doc2.pdf")
    write_pdf(out / "anonymized_doc1.pdf")            # valid -> completed
    (out / "anonymized_doc2.pdf").write_bytes(b"%PD")  # corrupt -> re-process

    engine.poll_events()
    engine.source_folder = str(src)
    engine._scan_folder()

    items = {e["name"]: e["status"] for e in drain(engine, "scan_item")}
    assert items == {"doc1.pdf": "completed", "doc2.pdf": "pending"}
    assert engine.files_to_process == ["doc2.pdf"]


# ---------------- audit / preview / batch controls ----------------

def test_audit_entry_written_as_jsonl(engine, tmp_path):
    engine.source_folder = str(tmp_path)
    engine.write_audit_entry({"timestamp": "t", "status": "success"})
    audit = tmp_path / "processed" / "audit_log.jsonl"
    entry = json.loads(audit.read_text(encoding="utf-8").strip())
    assert entry["status"] == "success"


def test_preview_renders_local_data_uris(engine, tmp_path):
    src = tmp_path / "data"
    out = src / "processed"
    out.mkdir(parents=True)
    write_pdf(out / "anonymized_doc1.pdf", pages=2)
    engine.source_folder = str(src)

    preview = engine.get_preview("doc1.pdf", max_pages=1)
    assert preview["total_pages"] == 2
    assert len(preview["pages"]) == 1
    assert preview["pages"][0].startswith("data:image/png;base64,")


def test_start_batch_guards(engine):
    engine.files_to_process = []
    with pytest.raises(ValueError):
        engine.start_batch({})
    engine.is_processing = True
    engine.files_to_process = ["x.pdf"]
    with pytest.raises(ValueError):
        engine.start_batch({})


def test_retry_failed_requeues(engine):
    engine.failed_files = ["doc2.pdf"]
    engine.files_to_process = []
    assert engine.retry_failed() == ["doc2.pdf"]
    assert engine.files_to_process == ["doc2.pdf"]
    assert engine.failed_files == []


def test_credentials_status_missing_in_clean_dir(engine):
    assert engine.credentials_status()["state"] == "missing"


# ---------------- cloud-sync detection ----------------

@pytest.fixture
def no_sync_env(monkeypatch):
    for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(env, raising=False)


def test_detect_cloud_sync_positive(no_sync_env):
    assert BatchEngine.detect_cloud_sync(r"C:\Users\x\OneDrive\Clinic") == "OneDrive"
    assert BatchEngine.detect_cloud_sync(r"C:\Users\x\Dropbox\docs") == "Dropbox"
    assert BatchEngine.detect_cloud_sync(r"D:\Google Drive\scans") == "Google Drive"
    assert BatchEngine.detect_cloud_sync("/home/x/Nextcloud/data") == "Nextcloud"


def test_detect_cloud_sync_negative(no_sync_env):
    assert BatchEngine.detect_cloud_sync(r"C:\clinical\documents") is None
    assert BatchEngine.detect_cloud_sync(r"C:\tools\toolbox\data") is None
    assert BatchEngine.detect_cloud_sync("") is None
    assert BatchEngine.detect_cloud_sync(None) is None
