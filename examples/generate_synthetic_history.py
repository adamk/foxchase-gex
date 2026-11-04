"""Regenerate the repository's credential-free synthetic history examples."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from datetime import datetime, timedelta
from pathlib import Path


SCRIPT_DIR = Path(os.path.abspath(os.fspath(Path(__file__).parent)))
ROOT = SCRIPT_DIR / "synthetic_archive"
SYMBOLS = {"SPX": 5100.0, "NDX": 24500.0}
TRADING_DAYS = ("2026-10-01", "2026-10-02", "2026-10-05")
CAPTURE_TIMES = ((9, 30), (9, 31), (9, 32))
NY_OFFSET = "-04:00"


def canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_session(symbol: str, day: str, base_spot: float) -> list[dict]:
    rows = []
    day_number = TRADING_DAYS.index(day)
    for index, (hour, minute) in enumerate(CAPTURE_TIMES):
        stamp = f"{day}T{hour:02d}:{minute:02d}:00{NY_OFFSET}"
        spot = base_spot + day_number * 3.0 + index * 1.25
        strikes = []
        for offset, weight in ((-2, 0.4), (-1, 0.8), (0, 1.2), (1, 0.7), (2, 0.3)):
            strike = spot + offset * (5.0 if symbol == "SPX" else 25.0)
            strikes.append({
                "strike": strike,
                "gex": round(weight * (1 if offset >= 0 else -1), 4),
                "call_gex": round(weight * 0.7, 4),
                "put_gex": round(weight * 0.3, 4),
            })
        causal_input = {
            "symbol": symbol,
            "spot": spot,
            "expiration_date": day,
            "synthetic_fixture": True,
            "contracts": [
                {
                    "option_type": "CALL" if i % 2 == 0 else "PUT",
                    "strike": strikes[i]["strike"],
                    "open_interest": 100 + i * 7 + index,
                    "gamma": round(0.005 + i * 0.001, 6),
                    "volatility": round(0.18 + i * 0.01, 4),
                    "multiplier": 100,
                }
                for i in range(len(strikes))
            ],
        }
        request_stamp = (datetime.fromisoformat(stamp) - timedelta(seconds=1)).isoformat(timespec="seconds")
        result = {
            "display_symbol": symbol,
            "spot": spot,
            "updated_at": stamp,
            "strikes": strikes,
            "structure": {
                "net_gex": round(sum(row["gex"] for row in strikes), 4),
                "gamma_flip": round(spot - 12.5, 2),
                "call_wall": round(spot + (10.0 if symbol == "SPX" else 50.0), 2),
                "put_wall": round(spot - (10.0 if symbol == "SPX" else 50.0), 2),
                "gamma_regime": "Synthetic Positive",
            },
            "patterns": {
                "primary": "Synthetic Read",
                "read_title": "Synthetic positive gamma",
                "signals": ["fixture only"],
            },
        }
        record = {
            "schema_version": 2,
            "captured_at": stamp,
            "request_timestamp": request_stamp,
            "response_timestamp": stamp,
            "symbol": symbol,
            "frame_index": index,
            "causal_input": causal_input,
            "provenance": {
                "provider": "synthetic_fixture",
                "data_class": "synthetic",
                "fixture_version": 1,
                "input_content_hash": canonical_hash(causal_input),
            },
            "result": result,
        }
        record["frame_content_hash"] = canonical_hash(record)
        rows.append(record)
    return rows


def _check_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError("synthetic fixture path must not be a symbolic link")
    if path.exists() and not stat.S_ISDIR(os.lstat(path).st_mode):
        raise RuntimeError("synthetic fixture path must be a directory")


def generate() -> list[Path]:
    for component in reversed((SCRIPT_DIR, *SCRIPT_DIR.parents)):
        try:
            if stat.S_ISLNK(os.lstat(component).st_mode):
                raise RuntimeError("synthetic fixture parent path must not contain symbolic links")
        except FileNotFoundError:
            continue
    _check_directory(ROOT)
    ROOT.mkdir(parents=True, exist_ok=True)
    expected = {f"{symbol}/{day}.jsonl" for symbol in SYMBOLS for day in TRADING_DAYS}
    for entry in ROOT.iterdir():
        if entry.is_symlink():
            raise RuntimeError("synthetic archive contains a symbolic link")
        if entry.name not in SYMBOLS or not entry.is_dir():
            raise RuntimeError("synthetic archive contains an unexpected entry")
        for item in entry.iterdir():
            relative = item.relative_to(ROOT).as_posix()
            if relative not in expected or item.is_symlink() or not item.is_file():
                raise RuntimeError("synthetic archive contains an unexpected file")

    written = []
    for symbol, base_spot in SYMBOLS.items():
        directory = ROOT / symbol
        _check_directory(directory)
        directory.mkdir(exist_ok=True)
        for day in TRADING_DAYS:
            target = directory / f"{day}.jsonl"
            if target.is_symlink():
                raise RuntimeError("synthetic session path must not be a symbolic link")
            payload = "".join(
                json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n"
                for row in build_session(symbol, day, base_spot)
            ).encode("utf-8")
            fd, temporary = tempfile.mkstemp(prefix=".synthetic-", dir=directory)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            written.append(target)
    return written


if __name__ == "__main__":
    for output in generate():
        print(output.relative_to(ROOT.parent))
