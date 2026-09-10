"""
Camera sources for the agent. Each yields JPEG bytes from `.frame()`.

  picamera2  - Raspberry Pi camera module (CSI) via libcamera
  opencv     - USB webcam / HDMI capture / gimbal video via /dev/videoN or RTSP URL
  mock       - synthetic frame with a moving HUD (no hardware)

Select with `camera.source` in config.yaml.
"""
from __future__ import annotations

import io
import logging
import math
import time

log = logging.getLogger("vayuveer.camera")


class MockCamera:
    def __init__(self, width=640, height=360, quality=70, **_):
        from PIL import Image, ImageDraw  # lazy import
        self._Image, self._Draw = Image, ImageDraw
        self.w, self.h, self.q = width, height, quality
        self.telemetry = {}
        self.source_label = "EO"

    def frame(self) -> bytes:
        t = time.time()
        img = self._Image.new("RGB", (self.w, self.h), (28, 44, 32) if self.source_label == "EO" else (40, 40, 40))
        d = self._Draw.draw = self._Draw.Draw(img)
        # rolling "terrain" so movement is visible
        for i in range(0, self.w, 40):
            x = (i + int(t * 60)) % self.w
            d.line([(x, 0), (x, self.h)], fill=(40, 62, 46), width=1)
        for j in range(0, self.h, 40):
            y = (j + int(t * 20)) % self.h
            d.line([(0, y), (self.w, y)], fill=(40, 62, 46), width=1)
        # a target that wanders
        cx = self.w / 2 + 120 * math.sin(t / 3)
        cy = self.h / 2 + 60 * math.cos(t / 4)
        d.rectangle([cx - 18, cy - 12, cx + 18, cy + 12], outline=(255, 90, 60), width=2)
        d.text((cx - 16, cy + 14), "TRK 0.91", fill=(255, 90, 60))
        d.text((self.w / 2 - 44, self.h - 16), f"SIMULATED {self.source_label} FEED", fill=(150, 175, 150))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=self.q)
        return buf.getvalue()

    def close(self):
        pass


class PiCamera:
    def __init__(self, width=1280, height=720, quality=70, **_):
        from picamera2 import Picamera2  # apt: python3-picamera2
        self.cam = Picamera2()
        cfg = self.cam.create_video_configuration(main={"size": (width, height), "format": "RGB888"})
        self.cam.configure(cfg)
        self.cam.start()
        self.q = quality
        self.telemetry = {}
        self.source_label = "EO"
        try:
            import cv2  # noqa: F401
            self._cv2 = cv2
        except Exception:
            self._cv2 = None
        log.info("Pi camera started %dx%d", width, height)

    def frame(self) -> bytes:
        if self._cv2 is not None:
            arr = self.cam.capture_array("main")
            ok, enc = self._cv2.imencode(".jpg", arr[:, :, ::-1], [int(self._cv2.IMWRITE_JPEG_QUALITY), self.q])
            return enc.tobytes()
        buf = io.BytesIO()
        self.cam.capture_file(buf, format="jpeg")
        return buf.getvalue()

    def close(self):
        self.cam.stop()


class OpenCVCamera:
    def __init__(self, device="/dev/video0", width=1280, height=720, quality=70, **_):
        import cv2
        self.cv2 = cv2
        self.cap = cv2.VideoCapture(int(device) if str(device).isdigit() else device)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.q = quality
        self.telemetry = {}
        self.source_label = "EO"
        if not self.cap.isOpened():
            raise RuntimeError(f"cannot open camera {device}")

    def frame(self) -> bytes:
        ok, img = self.cap.read()
        if not ok:
            raise RuntimeError("camera read failed")
        ok, enc = self.cv2.imencode(".jpg", img, [int(self.cv2.IMWRITE_JPEG_QUALITY), self.q])
        return enc.tobytes()

    def close(self):
        self.cap.release()


def make_camera(cfg: dict):
    src = cfg.get("source", "mock")
    try:
        if src == "picamera2":
            return PiCamera(**cfg)
        if src == "opencv":
            return OpenCVCamera(**cfg)
    except Exception as e:  # noqa: BLE001
        log.error("camera '%s' failed (%s); falling back to mock frames", src, e)
    return MockCamera(**{k: v for k, v in cfg.items() if k in ("width", "height", "quality")})
