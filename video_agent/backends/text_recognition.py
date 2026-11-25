from __future__ import annotations

import gc
import logging
import multiprocessing

import numpy as np

logger = logging.getLogger(__name__)


def _vision_worker(connection, config):
    """Native Vision state is confined to a disposable child process."""
    backend = VisionOCR(config)
    try:
        while True:
            image = connection.recv()
            if image is None:
                break
            try:
                connection.send(("ok", backend.recognize(image)))
            except Exception as exc:
                connection.send(("error", f"{type(exc).__name__}: {exc}"))
                break
    except (EOFError, BrokenPipeError):
        pass
    finally:
        backend.close()
        connection.close()


class IsolatedVisionOCR:
    """Recycle native state and retry the same image after worker failures."""

    max_requests = 400
    timeout_s = 90

    def __init__(self, config):
        self.config = config
        self.process = self.connection = None
        self.requests = 0

    def _start(self):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=_vision_worker, args=(child, self.config), daemon=True)
        try:
            process.start()
        except Exception:
            parent.close()
            child.close()
            raise
        child.close()
        self.connection, self.process, self.requests = parent, process, 0

    def _request(self, image):
        if self.process is None or self.requests >= self.max_requests:
            self.close()
            self._start()
        self.connection.send(image)
        if not self.connection.poll(self.timeout_s):
            raise RuntimeError(f"Apple Vision worker timed out after {self.timeout_s}s")
        status, result = self.connection.recv()
        if status != "ok":
            raise RuntimeError(result)
        self.requests += 1
        return result

    def recognize(self, image):
        for attempt in range(2):
            try:
                return self._request(image)
            except (RuntimeError, EOFError, BrokenPipeError, OSError):
                self.close()
                if attempt:
                    raise
                logger.warning("Restarting Apple Vision worker; retrying the same frame")

    def close(self):
        process, connection = self.process, self.connection
        self.process = self.connection = None
        self.requests = 0
        if connection is not None:
            try:
                connection.send(None)
            except (OSError, EOFError):
                pass
            connection.close()
        if process is not None:
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)


class VisionOCR:
    def __init__(self, config):
        self.config = config
        self.cpu_only = False

    def recognize(self, image):
        try:
            return self._recognize(image, self.cpu_only)
        except RuntimeError:
            if self.cpu_only:
                raise
            # Apple's accelerator can fail after hundreds of requests (e5rt).
            # Retry the same frame on CPU, then retain that mode for the stage.
            logger.warning("Apple Vision: accelerator failed; retrying this frame on CPU")
            self.cpu_only = True
            return self._recognize(image, True)

    def _recognize(self, image, cpu_only):
        from io import BytesIO

        import objc
        import Quartz
        import Vision
        from Foundation import NSData

        # Bound autoreleased Cocoa objects during long (20h+) indexing jobs.
        with objc.autorelease_pool():
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            raw = buffer.getvalue()
            data = NSData.dataWithBytes_length_(raw, len(raw))
            provider = Quartz.CGDataProviderCreateWithCFData(data)
            cgimage = Quartz.CGImageCreateWithPNGDataProvider(
                provider, None, True, Quartz.kCGRenderingIntentDefault
            )
            request = Vision.VNRecognizeTextRequest.alloc().init()
            if cpu_only:
                request.setUsesCPUOnly_(True)
            request.setRecognitionLevel_(0)  # accurate
            request.setRecognitionLanguages_(["en-US"])
            request.setUsesLanguageCorrection_(False)
            request.setMinimumTextHeight_(0.01)
            handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cgimage, None)
            ok, error = handler.performRequests_error_([request], None)
            if not ok:
                raise RuntimeError(f"Apple Vision OCR failed: {error}")
            lines = []
            for observation in request.results() or []:
                candidates = observation.topCandidates_(1)
                if not candidates:
                    continue
                candidate = candidates[0]
                if candidate.confidence() < self.config.min_confidence:
                    continue
                box = observation.boundingBox()
                lines.append(
                    {
                        "text": str(candidate.string()),
                        "confidence": float(candidate.confidence()),
                        "box": [box.origin.x, box.origin.y, box.size.width, box.size.height],
                        "box_coordinates": "normalized_bottom_left",
                    }
                )
            return lines

    def close(self):
        pass


class EasyOCR:
    def __init__(self, config):
        self.config = config
        self.reader = None

    def recognize(self, image):
        import easyocr

        if self.reader is None:
            self.reader = easyocr.Reader(["en"], gpu=True)
        return [
            {
                "text": text,
                "confidence": float(score),
                "box": np.asarray(box).tolist(),
                "box_coordinates": "pixels_top_left",
            }
            for box, text, score in self.reader.readtext(np.asarray(image))
            if score >= self.config.min_confidence
        ]

    def close(self):
        self.reader = None
        gc.collect()
