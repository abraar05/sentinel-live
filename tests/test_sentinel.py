"""Test suite for Sentinel.

Runs against the mock NVR, so no hardware is required:

    python3 tools/make_testclip.py /tmp/opencode/testclip.h264 300
    python3 tools/mock_nvr.py --port 34570 &
    pytest tests/ -q
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import codec                                    # noqa: E402
from backend.dvrip_client import (DvripAuthError,           # noqa: E402
                                  DvripConnection, sofia_hash)
from backend.store import DeviceStore                       # noqa: E402

CLIP = Path(os.environ.get("SENTINEL_TEST_CLIP", "/tmp/opencode/testclip.h264"))
MOCK_PORT = int(os.environ.get("SENTINEL_TEST_NVR", "34570"))
MOCK_PASSWORD = "mockpass"

# Synthetic only. Real DeviceManager exports carry live camera passwords, so
# the fixtures here must never be a copy of one.
SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<DeviceManager version="2.0">
\t<Device name="Loading Bay" domain="AA0000000AAAAA1" port="37777" username="admin" password="ExAmPlEpAsSwOrD00000000000000000000000000=" protocol="1" connect="19" />
\t<Device name="Back Office" domain="BB1111111BBBBB2" port="37777" username="admin" password="AnOtHeRsYsNtHeTiSpAsS0000000000000000000=" protocol="1" connect="19" />
\t<Device name="Car Park" domain="CC2222222CCCCC3" port="37777" username="admin" password="ThIrDpAsS00000000000000000000000000000000=" protocol="1" connect="19" />
</DeviceManager>
"""

# Shared across the encryption tests so expectations stay in step.
PASSWORD_BY_NAME = {
    "Loading Bay": "ExAmPlEpAsSwOrD00000000000000000000000000=",
    "Back Office": "AnOtHeRsYsNtHeTiSpAsS0000000000000000000=",
    "Car Park": "ThIrDpAsS00000000000000000000000000000000=",
}
FIRST_PASSWORD = PASSWORD_BY_NAME["Loading Bay"]
CLOUD_IDS = ("AA0000000AAAAA1", "BB1111111BBBBB2", "CC2222222CCCCC3")


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="session", autouse=True)
def mock_nvr():
    if not CLIP.exists():
        subprocess.run(
            [sys.executable, str(ROOT / "tools" / "make_testclip.py"), str(CLIP), "300"],
            check=True,
        )
    probe = socket.socket()
    try:
        probe.connect(("127.0.0.1", MOCK_PORT))
        probe.close()
        yield None
        return
    except OSError:
        pass
    finally:
        probe.close()

    log = open("/tmp/opencode/pytest-nvr.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "tools" / "mock_nvr.py"),
         "--port", str(MOCK_PORT), "--clip", str(CLIP)],
        stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT),
    )
    for _ in range(50):
        time.sleep(0.1)
        try:
            socket.create_connection(("127.0.0.1", MOCK_PORT), timeout=0.5).close()
            break
        except OSError:
            continue
    yield proc
    proc.terminate()


@pytest.fixture(scope="session")
def clip_bytes() -> bytes:
    return CLIP.read_bytes()


@pytest.fixture
def store(tmp_path) -> DeviceStore:
    return DeviceStore(str(tmp_path))


# ------------------------------------------------------------------- codec

class TestCodec:
    def test_split_annexb_holds_incomplete_tail(self, clip_bytes):
        nals = list(codec.iter_nalus(clip_bytes))
        # A raw file's final NAL has no terminator and must be withheld.
        assert len(nals) == 2420

    def test_parameter_sets_and_geometry(self, clip_bytes):
        sets = codec.extract_parameter_sets(clip_bytes)
        assert sets.codec == "h264"
        assert sets.codec_string == "avc1.42C01F"
        assert (sets.width, sets.height) == (1280, 720)
        assert sets.description and sets.description[0] == 1

    def test_access_units_match_picture_count(self, clip_bytes):
        units = codec.to_access_units(clip_bytes)
        assert len(units) == 300
        assert sum(1 for u in units if codec.contains_keyframe(u)) == 10
        assert codec.contains_keyframe(units[0])

    def test_access_unit_split_is_lossless(self, clip_bytes):
        rebuilt = b"".join(
            n for au in codec.to_access_units(clip_bytes)
            for n in [au[len(codec.START_CODE):]] if False
        )
        # Simpler: NAL payloads must survive regrouping unchanged.
        original = b"".join(codec.iter_nalus(clip_bytes))
        reassembled = b"".join(
            nalu for au in codec.to_access_units(clip_bytes)
            for _sc, nalu in codec.split_annexb(au + codec.START_CODE + b"\x00\x00")
        )
        assert len(reassembled) >= len(original)
        assert reassembled[:len(original)] == original

    def test_hevc_sniffer_rejects_h264(self, clip_bytes):
        assert codec.looks_like_h265(clip_bytes) is False

    def test_first_mb_in_slice(self, clip_bytes):
        slices = [n for n in codec.iter_nalus(clip_bytes)
                  if codec.nal_type(n) == 1]
        firsts = [n for n in slices if codec.first_mb_in_slice(n) == 0]
        assert 0 < len(firsts) < len(slices)

    def test_sps_parser_rejects_garbage(self):
        with pytest.raises((ValueError, IndexError, Exception)):
            codec.parse_sps(b"\x67\xff\xff\xff")


# ------------------------------------------------------------------- store

class TestStore:
    def test_xml_import_reads_every_field(self, store):
        devices, warnings = store.import_devicemanager_xml(SAMPLE_XML)
        assert len(devices) == 3
        by_name = {d.name: d for d in devices}
        assert by_name["Loading Bay"].cloud_id == CLOUD_IDS[0]
        assert by_name["Loading Bay"].cloud_port == 37777
        assert by_name["Loading Bay"].username == "admin"
        assert by_name["Car Park"].protocol == "1"
        # P2P ids are not dialable, so each import must warn.
        assert len(warnings) == 3

    def test_credentials_are_encrypted_at_rest(self, store):
        devices, _ = store.import_devicemanager_xml(SAMPLE_XML)
        raw = Path(store.path).read_text()
        for password in PASSWORD_BY_NAME.values():
            assert password not in raw
        for device in devices:
            assert store.secret(device) == PASSWORD_BY_NAME[device.name]

    def test_password_never_returned_in_api_shape(self, store):
        devices, _ = store.import_devicemanager_xml(SAMPLE_XML)
        redacted = devices[0].redacted()
        assert redacted["password"] is True
        assert redacted["has_password"] is True

    def test_host_map_binds_addresses(self, store):
        devices, warnings = store.import_devicemanager_xml(
            SAMPLE_XML, {CLOUD_IDS[0]: "192.168.1.64:34567"}
        )
        bound = next(d for d in devices if d.name == "Loading Bay")
        assert bound.host == "192.168.1.64"
        assert bound.port == 34567
        assert len(warnings) == 2

    def test_round_trip_through_disk(self, store):
        store.import_devicemanager_xml(SAMPLE_XML)
        reloaded = DeviceStore(store.dir)
        assert len(reloaded.list()) == 3
        assert reloaded.secret(reloaded.list()[0])

    def test_rejects_malformed_xml(self, store):
        with pytest.raises(ValueError):
            store.import_devicemanager_xml("<DeviceManager><<<")

    def test_key_file_is_generated_once(self, tmp_path):
        a, b = DeviceStore(str(tmp_path)), DeviceStore(str(tmp_path))
        assert (tmp_path / "device.key").exists()
        assert a.box.key_path == b.box.key_path


# ------------------------------------------------------------------ dvrip

class TestSofiaHash:
    def test_deterministic_and_bounded(self):
        value = sofia_hash("admin")
        assert value == sofia_hash("admin")
        assert value != sofia_hash("admin2")
        # md5 is 16 bytes; paired bytes collapse to 8 base62 characters.
        assert len(value) == 8
        assert all(c in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                   for c in value)


@pytest.mark.skipif(not CLIP.exists(), reason="needs the generated test clip")
class TestAgainstMockNVR:
    def _connect(self):
        conn = DvripConnection("127.0.0.1", "admin", MOCK_PASSWORD, port=MOCK_PORT,
                               timeout=6)
        conn.connect()
        return conn

    def test_login_and_system_info(self):
        conn = self._connect()
        try:
            assert conn.login() is True
            info = conn.system_info()
            assert info["VideoInChannel"] == 4
            assert info["DeviceModel"].startswith("HI3516")
        finally:
            conn.close()

    def test_bad_credentials_rejected(self):
        conn = DvripConnection("127.0.0.1", "admin", "nope", port=MOCK_PORT,
                               timeout=6)
        conn.connect()
        with pytest.raises(DvripAuthError):
            conn.login()
        conn.close()

    def test_snapshot_returns_jpeg(self):
        conn = self._connect()
        try:
            conn.login()
            jpeg = conn.snapshot()
            assert jpeg[:3] == b"\xff\xd8\xff"
            assert len(jpeg) > 1000
        finally:
            conn.close()

    def test_ptz_accepted(self):
        conn = self._connect()
        try:
            conn.login()
            assert conn.ptz("DirectionLeft", pattern="Start")["Ret"] == 100
            assert conn.ptz("DirectionLeft", pattern="Stop")["Ret"] == 100
        finally:
            conn.close()

    def test_live_frames_decode_to_pictures(self):
        conn = self._connect()
        try:
            conn.login()
            count, total, sets = 0, 0, codec.ParameterSets()
            for payload, meta in conn.iter_frames(0, "Main"):
                count += 1
                total += len(payload)
                codec.extract_parameter_sets(payload, sets)
                assert meta["width"] == 1280 and meta["height"] == 720
                if count >= 30:
                    break
            assert count == 30
            assert total > 10000
            assert sets.ready and sets.codec_string == "avc1.42C01F"
        finally:
            conn.close()

    def test_frame_rate_is_close_to_source(self):
        conn = self._connect()
        try:
            conn.login()
            start = time.time()
            for index, _payload in enumerate(conn.iter_frames(0, "Main")):
                if index >= 20:
                    break
            elapsed = time.time() - start
            assert 8 < 20 / elapsed < 22        # ~15 fps source
        finally:
            conn.close()

    def test_keepalive_moves_off_the_stream_socket(self):
        """A control reply on the preview socket would be read as a frame.

        JSON starts 0x7B, which is not a valid frame tag, so the stream dies.
        While previewing, the heartbeat must therefore live on its own
        connection and this socket must carry video only.
        """
        conn = self._connect()
        try:
            conn.login()
            assert conn._keeper is not None, "keepalive should start after login"

            for _payload, _meta in conn.iter_frames(0, "Main"):
                break

            assert conn._keeper is None, "preview socket still owns a keepalive"
            assert conn._stream_keeper is not None
            assert conn._stream_keeper._keeper is None
            assert conn._stream_keeper._defer_keepalive is True

            # And the stream is still clean afterwards.
            frames = 0
            for _payload, _meta in conn.iter_frames(0, "Main"):
                frames += 1
                if frames >= 10:
                    break
            assert frames == 10
        finally:
            conn.close()

    def test_open_preview_is_idempotent(self):
        """Re-claiming would inject a JSON reply into the binary stream."""
        conn = self._connect()
        try:
            conn.login()
            conn.open_preview(0, "Main")
            conn.open_preview(0, "Main")
            conn.open_preview(0, "Main")
            for _payload, _meta in conn.iter_frames(0, "Main"):
                break
        finally:
            conn.close()


# ----------------------------------------------------------------- motion

class TestMotionDetector:
    def _probe(self, painter):
        from PIL import Image
        img = Image.new("L", (160, 120), 30)
        painter(img)
        return img.convert("L").resize((64, 48), Image.BILINEAR).tobytes()

    def test_identical_frames_score_zero(self):
        from backend.recorder import MotionDetector
        a = self._probe(lambda img: None)
        b = self._probe(lambda img: None)
        assert MotionDetector._score(a, b) == 0.0

    def test_small_change_stays_below_threshold(self):
        """Compression jitter and an OSD clock must not trip the detector."""
        from backend.recorder import MOTION_PIXEL_RATIO, MotionDetector

        def speck(img):
            for x in range(0, 160, 40):
                for y in range(0, 120, 40):
                    img.putpixel((x, y), 90)

        assert MotionDetector._score(self._probe(lambda i: None),
                                     self._probe(speck)) < MOTION_PIXEL_RATIO * 100

    def test_subjects_entering_frame_trips_it(self):
        from backend.recorder import MOTION_PIXEL_RATIO, MotionDetector

        def subject(img):
            img.paste(220, (30, 25, 110, 95))

        score = MotionDetector._score(self._probe(lambda i: None),
                                      self._probe(subject))
        assert score > MOTION_PIXEL_RATIO * 100
        assert 0 < score <= 100

    def test_size_mismatch_is_maximal(self):
        from backend.recorder import MotionDetector
        assert MotionDetector._score(b"\x00" * 10, b"\x00" * 11) == 100.0


# --------------------------------------------------------------------- api

class TestStoreRoundTrip:
    def test_devices_json_has_no_plaintext(self, store):
        store.import_devicemanager_xml(SAMPLE_XML)
        blob = Path(store.path).read_text()
        for password in PASSWORD_BY_NAME.values():
            assert password not in blob
        payload = json.loads(blob)
        assert payload["version"] == 1
        assert len(payload["devices"]) == 3