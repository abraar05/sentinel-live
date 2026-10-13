"""Device store: import, encrypt, persist.

The credentials in a ``DeviceManager.xml`` dump are the whole security
surface of this app, so they are never written to disk in the clear and
never leave the host except to the device they belong to.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

TRANSPORTS = ("dvrip", "rtsp", "dahua")


def _data_dir() -> str:
    return os.environ.get(
        "SENTINEL_DATA",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"),
    )


class SecretBox:
    """AES-256-GCM envelope for credentials, keyed by a local key file."""

    def __init__(self, data_dir: str | None = None):
        self.dir = data_dir or _data_dir()
        os.makedirs(self.dir, exist_ok=True)
        self.key_path = os.path.join(self.dir, "device.key")
        self._lock = threading.Lock()
        self._aes = AESGCM(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        if os.path.exists(self.key_path):
            with open(self.key_path, "rb") as fh:
                raw = fh.read().strip()
            if len(raw) == 64:
                return bytes.fromhex(raw.decode())
        key = AESGCM.generate_key(bit_length=256)
        with open(self.key_path, "wb") as fh:
            fh.write(key.hex().encode())
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            pass
        return key

    def seal(self, plaintext: str) -> str:
        nonce = secrets.token_bytes(12)
        blob = self._aes.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.b64encode(nonce + blob).decode()

    def open(self, token: str) -> str:
        raw = base64.b64decode(token.encode())
        return self._aes.decrypt(raw[:12], raw[12:], None).decode("utf-8")


@dataclass
class Device:
    id: str
    name: str
    host: str = ""
    port: int = 34567
    username: str = "admin"
    password: str = ""          # sealed token
    transport: str = "dvrip"
    channels: list = field(default_factory=list)
    cloud_id: str = ""          # XMeye P2P identifier, informational
    cloud_port: int = 0
    protocol: str = ""
    connect: str = ""
    rtsp_path: str = ""
    storage_path: str = ""      # NAS/network path for recordings
    enabled: bool = True
    notes: str = ""
    created_at: float = field(default_factory=time.time)

    def redacted(self) -> dict:
        data = asdict(self)
        data["password"] = bool(self.password)
        data["has_password"] = bool(self.password)
        data["storage_path"] = self.storage_path
        return data


class DeviceStore:
    def __init__(self, data_dir: str | None = None):
        self.dir = data_dir or _data_dir()
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "devices.json")
        self.box = SecretBox(self.dir)
        self._lock = threading.RLock()
        self._devices: dict[str, Device] = {}
        self.load()

    # --------------------------------------------------------------- persist

    def load(self) -> None:
        with self._lock:
            if not os.path.exists(self.path):
                self._devices = {}
                return
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (ValueError, OSError):
                self._devices = {}
                return
            devices = {}
            for entry in raw.get("devices", []):
                try:
                    devices[entry["id"]] = Device(**{
                        k: v for k, v in entry.items() if k in Device.__annotations__
                    })
                except TypeError:
                    continue
            self._devices = devices

    def save(self) -> None:
        with self._lock:
            payload = {
                "version": 1,
                "devices": [asdict(d) for d in self._devices.values()],
            }
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)

    # ---------------------------------------------------------------- access

    def list(self) -> list[Device]:
        with self._lock:
            return sorted(self._devices.values(), key=lambda d: d.name.lower())

    def get(self, device_id: str) -> Device | None:
        with self._lock:
            return self._devices.get(device_id)

    def require(self, device_id: str) -> Device:
        device = self.get(device_id)
        if device is None:
            raise KeyError(device_id)
        return device

    def secret(self, device: Device) -> str:
        return self.box.open(device.password) if device.password else ""

    def upsert(self, device: Device) -> Device:
        with self._lock:
            self._devices[device.id] = device
            self.save()
            return device

    def delete(self, device_id: str) -> bool:
        with self._lock:
            if device_id in self._devices:
                del self._devices[device_id]
                self.save()
                return True
            return False

    # ---------------------------------------------------------------- import

    def import_devicemanager_xml(self, xml_text: str, host_map: dict | None = None
                                ) -> tuple[list[Device], list[str]]:
        """Parse an ``xmeye`` DeviceManager export.

        The export only carries P2P cloud identifiers, which cannot be dialled
        directly, so each imported device is created *unreachable* and must be
        given a host. *host_map* maps ``(cloud_id, name)`` or ``cloud_id`` to a
        host:port string to bind one automatically.

        Returns ``(devices, warnings)``.
        """
        try:
            root = ET.fromstring(xml_text.strip())
        except ET.ParseError as exc:
            raise ValueError(f"not valid DeviceManager XML: {exc}") from exc

        host_map = host_map or {}
        devices: list[Device] = []
        warnings: list[str] = []

        for node in root.findall(".//Device"):
            name = (node.get("name") or "Unnamed").strip()
            cloud_id = (node.get("domain") or "").strip()
            cloud_port = int(node.get("port") or 0 or 0)
            username = (node.get("username") or "admin").strip()
            password = node.get("password") or ""

            endpoint = (host_map.get(cloud_id)
                        or host_map.get((cloud_id, name))
                        or host_map.get(name.lower()))
            host, port = "", 34567
            if endpoint:
                if ":" in str(endpoint):
                    host, _, port_s = str(endpoint).partition(":")
                    try:
                        port = int(port_s)
                    except ValueError:
                        port = 34567
                else:
                    host = str(endpoint)

            if not host:
                warnings.append(
                    f"{name}: P2P cloud id {cloud_id or '?'} is not directly "
                    f"dialable - set a host before streaming"
                )

            devices.append(Device(
                id=uuid.uuid4().hex[:12],
                name=name,
                host=host,
                port=port,
                username=username,
                password=self.box.seal(password) if password else "",
                transport="dvrip",
                cloud_id=cloud_id,
                cloud_port=cloud_port,
                protocol=node.get("protocol") or "",
                connect=node.get("connect") or "",
                notes="imported from DeviceManager export",
            ))

        for device in devices:
            self.upsert(device)
        return devices, warnings