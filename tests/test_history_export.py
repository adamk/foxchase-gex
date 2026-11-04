"""Synthetic-only tests for local historical publication."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_directory_fsync_is_skipped_when_windows_cannot_open_directories(tmp_path, monkeypatch):
    from gex_client import history_export

    opened = []

    def windows_directory_open(path, flags):
        opened.append(path)
        raise PermissionError("Windows CRT cannot open directory handles this way")

    monkeypatch.setattr(history_export.os, "name", "nt")
    monkeypatch.setattr(history_export.os, "open", windows_directory_open)

    history_export._fsync_directory(tmp_path)

    assert opened == []


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def make_frame(symbol="SPX", stamp="2026-10-01T09:30:00-04:00", index=0, *, synthetic=True):
    causal_input = {
        "symbol": symbol,
        "spot": 5100.0,
        "synthetic_fixture": synthetic,
        "account_number": "SYNTHETIC_ACCOUNT_SENTINEL",
        "contracts": [{"option_type": "CALL", "strike": 5100.0, "gamma": 0.01}],
    }
    record = {
        "schema_version": 2,
        "captured_at": stamp,
        "request_timestamp": stamp,
        "response_timestamp": stamp,
        "symbol": symbol,
        "frame_index": index,
        "causal_input": causal_input,
        "provenance": {
            "provider": "synthetic_fixture" if synthetic else "test_non_synthetic_fixture",
            "data_class": "synthetic" if synthetic else "test_non_synthetic",
            "input_content_hash": canonical_hash(causal_input),
            "access_token": "SYNTHETIC_TOKEN_SENTINEL",
        },
        "result": {
            "display_symbol": symbol,
            "spot": 5100.0 + index,
            "updated_at": stamp,
            "strikes": [{
                "strike": 5100.0 + index,
                "gex": 1.25,
                "call_gex": 1.5,
                "put_gex": 0.25,
                "secret_field": "SYNTHETIC_STRIKE_SENTINEL",
            }],
            "structure": {
                "net_gex": 1.25,
                "gamma_flip": 5090.0,
                "call_wall": 5120.0,
                "put_wall": 5070.0,
                "method_version": "SYNTHETIC_METHOD_SENTINEL",
            },
            "patterns": {"primary": "SYNTHETIC_PRIVATE_LABEL_SENTINEL"},
            "api_key": "SYNTHETIC_RESULT_SECRET_SENTINEL",
        },
    }
    record["frame_content_hash"] = canonical_hash(record)
    return record


def write_archive(root, records=None, *, synthetic=True):
    directory = root / "SPX"
    directory.mkdir(parents=True, exist_ok=True)
    if records is None:
        records = [
            make_frame(stamp="2026-10-01T09:30:00-04:00", index=0, synthetic=synthetic),
            make_frame(stamp="2026-10-01T09:31:00-04:00", index=1, synthetic=synthetic),
        ]
    path = directory / "2026-10-01.jsonl"
    path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in records), encoding="utf-8")
    return path


def run_export(root, output, *args, allow_synthetic=True):
    command = [sys.executable, "-m", "gex_client.history_export", "--archive-dir", str(root),
               "--output-dir", str(output), "--symbol", "SPX", "--format", "json"]
    if allow_synthetic:
        command.append("--allow-synthetic-fixtures")
    command.extend(args)
    return subprocess.run(
        command,
        cwd=REPO,
        capture_output=True,
        text=True,
    )


def test_synthetic_export_is_deterministic_and_excludes_restricted_fields(tmp_path):
    archive = tmp_path / "archive"
    output = tmp_path / "publication"
    write_archive(archive)

    first = run_export(archive, output)

    assert first.returncode == 0, first.stderr
    report = json.loads(first.stdout)
    assert report["status"] == "published"
    assert report["redistribution_status"] == "synthetic_claim_unverified"
    current_bytes = (output / "current.json").read_bytes()
    pointer = json.loads(current_bytes)
    generation = output / "generations" / pointer["generation_id"]
    index_path = output / pointer["index_path"]
    index = json.loads(index_path.read_text(encoding="utf-8"))
    timeline_path = index_path.parent / index["sessions"][0]["timeline_path"]
    timeline_bytes = timeline_path.read_bytes()
    rendered = (index_path.read_bytes() + timeline_bytes + current_bytes).decode("utf-8")
    for forbidden in (
        "causal_input", "access_token", "SYNTHETIC_ACCOUNT_SENTINEL",
        "SYNTHETIC_TOKEN_SENTINEL", "SYNTHETIC_PRIVATE_LABEL_SENTINEL",
        "SYNTHETIC_RESULT_SECRET_SENTINEL", "SYNTHETIC_METHOD_SENTINEL",
        "SYNTHETIC_STRIKE_SENTINEL", "provenance",
    ):
        assert forbidden not in rendered
    assert len(timeline_bytes.splitlines()) == 2

    second = run_export(archive, output)

    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["reused_generation"] is True
    assert (output / "current.json").read_bytes() == current_bytes
    assert (output / pointer["index_path"]).read_bytes() == index_path.read_bytes()
    assert len([p for p in (output / "generations").iterdir() if p.name != ".staging"]) == 1
    assert generation.is_dir()


def test_export_dry_run_does_not_create_output(tmp_path):
    archive = tmp_path / "archive"
    output = tmp_path / "not-created"
    write_archive(archive)

    result = run_export(archive, output, "--dry-run")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "ready"
    assert json.loads(result.stdout)["redistribution_status"] == "synthetic_claim_unverified"
    assert not output.exists()


def test_synthetic_markers_require_explicit_fixture_opt_in(tmp_path):
    archive = tmp_path / "archive"
    output = tmp_path / "publication"
    write_archive(archive)

    result = run_export(archive, output, allow_synthetic=False)

    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "blocked"
    assert "unauthenticated" in json.loads(result.stdout)["reason"]
    assert not output.exists()


def test_non_synthetic_export_requires_an_explicit_nonlegal_operator_attestation(tmp_path):
    archive = tmp_path / "archive"
    output = tmp_path / "publication"
    write_archive(archive, synthetic=False)

    refused = run_export(archive, output)

    assert refused.returncode == 1
    assert json.loads(refused.stdout)["status"] == "blocked"
    assert not output.exists()

    policy = tmp_path / "rights-policy.json"
    policy.write_text(json.dumps({
        "policy_version": 1,
        "redistribution_review": "approved",
        "result_fields": ["spot", "strikes", "structure"],
    }), encoding="utf-8")
    ambiguous = run_export(archive, output, "--rights-policy", str(policy))
    assert ambiguous.returncode == 1
    assert json.loads(ambiguous.stdout)["status"] == "blocked"
    assert "not a legal rights determination" in json.loads(ambiguous.stdout)["reason"]

    policy.write_text(json.dumps({
        "policy_version": 1,
        "redistribution_review": "operator_attested_not_legal_clearance",
        "result_fields": ["spot", "strikes", "structure"],
    }), encoding="utf-8")
    allowed = run_export(archive, output, "--rights-policy", str(policy))

    assert allowed.returncode == 0, allowed.stderr
    assert json.loads(allowed.stdout)["redistribution_status"] == "operator_attested_not_legal_clearance"
    pointer = json.loads((output / "current.json").read_text(encoding="utf-8"))
    index = json.loads((output / pointer["index_path"]).read_text(encoding="utf-8"))
    assert index["redistribution_status"] == "operator_attested_not_legal_clearance"
    timeline_path = (output / pointer["index_path"]).parent / index["sessions"][0]["timeline_path"]
    entry = json.loads(timeline_path.read_text(encoding="utf-8").splitlines()[0])
    assert "spot" in entry["result"]
    assert "display_symbol" not in entry["result"]


def test_export_reuses_complete_orphan_generation_after_pointer_interruption(tmp_path):
    archive = tmp_path / "archive"
    output = tmp_path / "publication"
    write_archive(archive)

    first = run_export(archive, output)
    assert first.returncode == 0, first.stderr
    pointer = json.loads((output / "current.json").read_text(encoding="utf-8"))
    (output / "current.json").unlink()

    recovered = run_export(archive, output)

    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout)["reused_generation"] is True
    assert json.loads((output / "current.json").read_text(encoding="utf-8")) == pointer
    assert len([p for p in (output / "generations").iterdir() if p.name != ".staging"]) == 1


def test_export_refuses_corrupt_source_without_replacing_current_generation(tmp_path):
    archive = tmp_path / "archive"
    output = tmp_path / "publication"
    source = write_archive(archive)
    good = run_export(archive, output)
    assert good.returncode == 0, good.stderr
    before = (output / "current.json").read_bytes()

    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
    rows[0]["result"]["spot"] = -1
    source.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
    blocked = run_export(archive, output)

    assert blocked.returncode == 1
    assert json.loads(blocked.stdout)["status"] == "blocked"
    assert (output / "current.json").read_bytes() == before


def test_export_rejects_symlinked_or_overlapping_paths(tmp_path):
    archive = tmp_path / "archive"
    write_archive(archive)
    external = tmp_path / "external"
    external.mkdir()
    output_link = tmp_path / "linked-output"
    output_link.symlink_to(external, target_is_directory=True)

    linked = run_export(archive, output_link)
    overlap = run_export(archive, archive / "publication")

    assert linked.returncode == 2
    assert not list(external.iterdir())
    assert overlap.returncode == 2
    assert not (archive / "publication").exists()


def test_export_can_publish_a_stable_prefix_during_a_concurrent_append(tmp_path, monkeypatch):
    from gex_client import history_export

    archive = tmp_path / "archive"
    output = tmp_path / "publication"
    first = make_frame(stamp="2026-10-01T09:30:00-04:00", index=0)
    source = write_archive(archive, [first])
    second = make_frame(stamp="2026-10-01T09:31:00-04:00", index=1)
    original_verify = history_export.verify_archive
    verify_calls = 0
    appended = False

    def append_during_second_scan(*args, **kwargs):
        nonlocal verify_calls, appended
        verify_calls += 1
        callback = kwargs["on_record"]

        def wrapped_callback(symbol, day, line, record):
            nonlocal appended
            callback(symbol, day, line, record)
            if verify_calls == 2 and not appended:
                with source.open("ab") as stream:
                    stream.write((json.dumps(second, separators=(",", ":")) + "\n").encode())
                appended = True

        kwargs["on_record"] = wrapped_callback
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(history_export, "verify_archive", append_during_second_scan)
    exported = history_export.export_archive(
        archive, output, symbol="SPX", allow_synthetic_fixtures=True,
    )

    assert exported["status"] == "published"
    pointer = json.loads((output / "current.json").read_text(encoding="utf-8"))
    index = json.loads((output / pointer["index_path"]).read_text(encoding="utf-8"))
    timeline = (output / pointer["index_path"]).parent / index["sessions"][0]["timeline_path"]
    assert len(timeline.read_text(encoding="utf-8").splitlines()) == 1
    assert source.read_text(encoding="utf-8").count("\n") == 2


def test_export_requires_explicit_opt_in_for_capture_gaps(tmp_path):
    archive = tmp_path / "archive"
    output = tmp_path / "publication"
    write_archive(archive, [
        make_frame(stamp="2026-10-01T09:30:00-04:00", index=0),
        make_frame(stamp="2026-10-01T09:33:00-04:00", index=3),
    ])

    refused = run_export(archive, output)
    allowed = run_export(archive, output, "--allow-capture-gaps")

    assert refused.returncode == 1
    assert json.loads(refused.stdout)["status"] == "blocked"
    assert allowed.returncode == 0, allowed.stderr
    pointer = json.loads((output / "current.json").read_text(encoding="utf-8"))
    index = json.loads((output / pointer["index_path"]).read_text(encoding="utf-8"))
    assert index["integrity_status"] == "verified_with_gaps"
    assert index["sessions"][0]["gap_count"] == 1
