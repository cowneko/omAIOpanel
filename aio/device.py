"""Direct USB transport for the Thermalright GrandVision AIO panel (87ad:70db).

Vendor bulk protocol ("USBLCDNew"), as reverse-engineered in GPL-3 TRCC-Linux
by Lexonight1 from USBLCDNew.exe — independent implementation, no TRCC code.

Wire summary:
  * Vendor-specific interface ("winusb"), bulk OUT 0x01 / bulk IN 0x81.
  * Handshake: write 64-byte magic request, read 1024-byte reply.
    PM byte at reply[24], SUB byte at reply[36].
  * Frames: 64-byte header + payload, written in 16 KiB chunks,
    ZLP delimiter when the total length is 512-aligned.
      header[0:4]  magic 12 34 56 78
      header[4:8]  cmd u32 LE   (2 = JPEG, 3 = RGB565)
      header[8:12] width u32 LE
      header[12:16] height u32 LE
      header[56:60] u32 LE = 2
      header[60:64] payload length u32 LE
  * Most PMs take JPEG (cmd 2) sized exactly to the header; PM 32/50 take
    raw RGB565 (cmd 3, PM 50 big-endian).
  * The firmware reverts to its boot logo ~2-3 s after the last frame —
    the caller must resend the last frame about every 150 ms.
"""

from __future__ import annotations

import io
import logging
import struct
import time

import usb.core
import usb.util
from PIL import Image

log = logging.getLogger("aio.device")

VID, PID = 0x87AD, 0x70DB

MAGIC = bytes([0x12, 0x34, 0x56, 0x78])
HANDSHAKE_PAYLOAD = MAGIC + b"\x00" * 52 + struct.pack("<I", 1) + b"\x00" * 4
HANDSHAKE_READ_SIZE = 1024
HANDSHAKE_TIMEOUT_MS = 1000
WRITE_TIMEOUT_MS = 5000
WRITE_CHUNK_SIZE = 16 * 1024

RGB565_PMS = frozenset({32})       # PMs that take raw RGB565 (cmd 3)
BIG_ENDIAN_PMS = frozenset({50})   # RGB565 PMs that want big-endian words

EP_WRITE = 0x01

#: Resend cadence for the last frame — the panel reverts to its boot logo
#: ~2-3 s after the last frame; the Windows app resends every ~150 ms.
KEEPALIVE_S = 0.15


class DeviceError(Exception):
    pass


def _handshake_payload() -> bytes:
    p = bytearray(64)
    p[0:4] = MAGIC
    struct.pack_into("<I", p, 56, 1)
    return bytes(p)


class GrandVisionPanel:
    """One connected panel. Owns the pyusb handle exclusively."""

    def __init__(self, jpeg_quality: int = 90) -> None:
        self.jpeg_quality = jpeg_quality
        self.dev = None
        self.ep_out = None
        self.ep_in = None
        self.connected = False
        # resolved at handshake
        self.pm: int | None = None
        self.sub: int | None = None
        self.native: tuple[int, int] = (960, 540)       # panel scanout (w, h)
        self.canvas: tuple[int, int] = (540, 960)       # design canvas (w, h)
        self.orientation: int = 90                      # user orientation dial
        self.mount_base: int = 0                        # from SUB byte
        self.wire_deg: int = 0
        self.resolution: tuple[int, int] = (960, 540)   # wire header w,h
        self.use_jpeg = True
        self.byte_swap = False

    # ---- USB plumbing ----------------------------------------------------

    def _claim(self) -> None:
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            raise DeviceError(f"no USB device {VID:04x}:{PID:04x} present")
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
                log.info("detached kernel driver from interface 0")
        except usb.core.USBError:
            pass
        try:
            dev.set_configuration()
        except usb.core.USBError as e:
            if "busy" in str(e):
                raise DeviceError(
                    "set_configuration busy — another process holds the panel"
                ) from e
            raise DeviceError(f"set_configuration failed: {e}") from e
        intf = dev.get_active_configuration()[(0, 0)]
        eps = list(intf)
        ep_out = next((e for e in eps
                       if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT), None)
        ep_in = next((e for e in eps
                      if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN), None)
        if ep_out is None or ep_in is None:
            raise DeviceError("panel exposes no bulk endpoint pair")
        for ep in (ep_out, ep_in):
            try:
                dev.clear_halt(ep.bEndpointAddress)
            except usb.core.USBError:
                pass
        self.dev, self.ep_out, self.ep_in = dev, ep_out, ep_in
        log.info("claimed %04x:%04x bus=%03d addr=%03d eps=0x%02x/0x%02x",
                 VID, PID, dev.bus, dev.address,
                 ep_out.bEndpointAddress, ep_in.bEndpointAddress)

    # ---- handshake -------------------------------------------------------

    def _handshake(self) -> None:
        try:
            self.ep_out.write(_handshake_payload(), HANDSHAKE_TIMEOUT_MS)
            resp = bytes(self.ep_in.read(HANDSHAKE_READ_SIZE, HANDSHAKE_TIMEOUT_MS))
        except usb.core.USBError as e:
            raise DeviceError(f"handshake transfer failed: {e}") from e
        if len(resp) < 41 or resp[24] == 0:
            raise DeviceError(
                f"handshake validation failed (len={len(resp)}, "
                f"resp[24]={resp[24] if len(resp) > 24 else 'n/a'})"
            )
        self.pm, self.sub = resp[24], resp[36]
        self.use_jpeg = self.pm not in RGB565_PMS
        self.byte_swap = self.pm in BIG_ENDIAN_PMS
        # Mount/rotation model (C# FormCZTV): the panel's scanout buffer is
        # NATIVE LANDSCAPE (960x540 for this family).  SUB 5/7 carry a 180°
        # mount compensation (encode base).  The wire frame is the design
        # canvas transposed by (base − orientation): portrait 540x960 content
        # at orientation 90 lands as 960x540 on the wire.
        if self.pm in _PM_TO_SIZE:
            self.native = _PM_TO_SIZE[self.pm]          # landscape w, h
        else:
            log.info("PM %d not in size map — assuming 960x540 family", self.pm)
            self.native = (960, 540)
        self.mount_base = 180 if self.sub in (5, 7) else 0
        self._apply_geometry()
        log.info("handshake ok: PM=%d SUB=%d -> %s %s, wire %d° header %dx%d",
                 self.pm, self.sub, "JPEG" if self.use_jpeg else "RGB565",
                 "BE" if self.byte_swap else "LE", self.wire_deg,
                 self.resolution[0], self.resolution[1])

    def _apply_geometry(self) -> None:
        cw, ch = self.canvas
        gw, gh = self.native
        # A canvas shaped like the glass goes to the wire as-is; a canvas of
        # the other shape is transposed by (mount_base − orientation).
        # Any orientation dial value is legal now (0/90/180/270) — the
        # editor's orientation buttons drive it. A canvas that doesn't match
        # the glass shape after the transpose can't scan: that's a layout
        # mistake, reported here and visible as a rejected frame.
        self.wire_deg = (self.mount_base - self.orientation) % 360
        if (cw > ch) != (gw > gh) and self.wire_deg % 180 == 0:
            log.warning("canvas %dx%d can't scan unrotated on %dx%d glass — "
                        "pick an orientation that transposes it", cw, ch, gw, gh)
        # header must match the payload: canvas dims AFTER the wire transpose
        self.resolution = (cw, ch) if self.wire_deg % 180 == 0 else (ch, cw)
        log.info("geometry: canvas=%dx%d orientation=%d mount_base=%d -> wire %d°, header %dx%d",
                 cw, ch, self.orientation, self.mount_base, self.wire_deg,
                 self.resolution[0], self.resolution[1])

    def set_orientation(self, deg: int) -> None:
        self.orientation = deg % 360
        if self.pm is not None:
            self._apply_geometry()

    def connect(self, max_attempts: int = 1) -> None:
        last: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                self._claim()
                self._handshake()
                self.connected = True
                return
            except (DeviceError, usb.core.USBError, ValueError) as e:
                last = e
                self.disconnect()
                if attempt < max_attempts:
                    time.sleep(1.5)
        raise DeviceError(f"connect failed: {last}")

    def disconnect(self) -> None:
        try:
            if self.dev is not None:
                usb.util.dispose_resources(self.dev)
        except Exception:
            pass
        self.dev = self.ep_out = self.ep_in = None
        self.connected = False

    # ---- frames ----------------------------------------------------------

    def encode_frame(self, img: Image.Image) -> bytes:
        """PIL image -> complete wire frame (header + payload)."""
        deg = self.wire_deg % 360
        if deg == 90:
            img = img.transpose(Image.Transpose.ROTATE_270)   # clockwise
        elif deg == 270:
            img = img.transpose(Image.Transpose.ROTATE_90)    # counter-clockwise
        elif deg == 180:
            img = img.transpose(Image.Transpose.ROTATE_180)
        if img.size != self.resolution:
            img = img.resize(self.resolution, Image.LANCZOS)
        if self.use_jpeg:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=self.jpeg_quality)
            payload = buf.getvalue()
            cmd = 2
        else:
            import numpy as np
            arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
            r = (arr[..., 0] >> 3).astype(np.uint16) << 11
            g = (arr[..., 1] >> 2).astype(np.uint16) << 5
            b = (arr[..., 2] >> 3).astype(np.uint16)
            words = r | g | b
            if self.byte_swap:
                words = words.byteswap()
            payload = words.tobytes()
            cmd = 3
        w, h = self.resolution
        header = bytearray(64)
        header[0:4] = MAGIC
        struct.pack_into("<I", header, 4, cmd)
        struct.pack_into("<I", header, 8, w)
        struct.pack_into("<I", header, 12, h)
        struct.pack_into("<I", header, 56, 2)
        struct.pack_into("<I", header, 60, len(payload))
        return bytes(header) + payload

    def send_raw(self, frame: bytes) -> bool:
        """Write a prepared frame (header+payload). Safe for keepalive."""
        if not self.connected:
            return False
        for offset in range(0, len(frame), WRITE_CHUNK_SIZE):
            chunk = frame[offset:offset + WRITE_CHUNK_SIZE]
            try:
                written = self.ep_out.write(chunk, WRITE_TIMEOUT_MS)
            except usb.core.USBError as e:
                log.warning("bulk write failed at %d: %s", offset, e)
                return False
            if written < len(chunk):
                log.warning("SHORT write at %d (%d/%d)", offset, written, len(chunk))
                return False
        if len(frame) % 512 == 0:
            try:
                self.ep_out.write(b"", WRITE_TIMEOUT_MS)   # ZLP delimiter
            except usb.core.USBError as e:
                log.warning("ZLP failed: %s", e)
                return False
        return True

    def send_frame(self, img: Image.Image) -> bool:
        return self.send_raw(self.encode_frame(img))

    def send_black(self) -> bool:
        black = Image.new("RGB", self.resolution, (0, 0, 0))
        return self.send_frame(black)


# PM ladder subset for the 960x540 / 960x320 / 854x480 families (native size).
_PM_TO_SIZE = {
    10: (960, 540),
    11: (960, 540),
    16: (960, 540),
    13: (960, 320),
    17: (960, 320),
    18: (960, 320),
    5: (480, 854),
    6: (480, 854),
}
