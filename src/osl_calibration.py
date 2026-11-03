"""Ideal 50-ohm one-port OSL calibration for complex reflection measurements."""

import hashlib
import json
from datetime import datetime, timezone

import numpy as np


class OSLCalibration:
    FORMAT = "siglent-sva1032x-software-osl"
    SCHEMA_VERSION = 1
    STANDARD_NAMES = ("short", "open", "load")

    def __init__(self, frequencies, standards, instrument_id, settings, created_utc=None):
        self.frequencies = np.asarray(frequencies, dtype=float)
        if (self.frequencies.ndim != 1 or self.frequencies.size < 3
                or not np.all(np.isfinite(self.frequencies))
                or np.any(self.frequencies <= 0)
                or np.any(np.diff(self.frequencies) <= 0)):
            raise ValueError("Calibration frequencies must be finite, positive and increasing")
        if not isinstance(instrument_id, str) or not instrument_id.strip():
            raise ValueError("Calibration instrument identity is missing")
        self.instrument_id = instrument_id.strip()
        if not isinstance(settings, dict) or set(settings) != {"start_hz", "stop_hz", "points", "bandwidth_hz"}:
            raise ValueError("Calibration sweep settings are incomplete")
        self.settings = dict(settings)
        if (isinstance(settings['points'], bool) or not isinstance(settings['points'], int)
                or settings['points'] != self.frequencies.size):
            raise ValueError("Calibration point count does not match its frequency axis")
        for key in ("start_hz", "stop_hz", "bandwidth_hz"):
            value = settings[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value <= 0:
                raise ValueError(f"Invalid calibration {key}")
        expected = np.linspace(settings['start_hz'], settings['stop_hz'], settings['points'])
        if not np.allclose(self.frequencies, expected, rtol=1e-10, atol=1e-3):
            raise ValueError("Calibration frequency axis does not match the linear sweep settings")
        self.standards = {}
        for name in self.STANDARD_NAMES:
            if name not in standards:
                raise ValueError(f"Missing {name.upper()} calibration standard")
            data = np.asarray(standards[name], dtype=complex)
            if data.shape != self.frequencies.shape or not np.all(np.isfinite(data)):
                raise ValueError(f"Invalid {name.upper()} data or frequency-grid mismatch")
            self.standards[name] = data.copy()
        self.created_utc = created_utc or datetime.now(timezone.utc).isoformat()
        # m = d + t*g/(1-s*g) = (a+b*g)/(1-c*g)
        # Solve [1, g, m*g] [a,b,c]^T = m independently at each frequency.
        measured = np.stack([self.standards[name] for name in self.STANDARD_NAMES], axis=1)
        ideal = np.broadcast_to(np.array([-1, 1, 0], dtype=complex), measured.shape)
        matrix = np.stack([np.ones_like(ideal), ideal, measured * ideal], axis=2)
        condition = np.linalg.cond(matrix)
        if not np.all(np.isfinite(condition)) or np.any(condition > 1e12):
            raise ValueError("Calibration standards are indistinguishable or ill-conditioned; recapture OSL")
        coefficients = np.linalg.solve(matrix, measured[..., None])[..., 0]
        self.directivity = coefficients[:, 0]
        self._b = coefficients[:, 1]
        self.source_match = coefficients[:, 2]
        self.reflection_tracking = self._b + self.directivity * self.source_match
        if not np.all(np.isfinite(coefficients)) or np.any(np.abs(self.reflection_tracking) < 1e-15):
            raise ValueError("Degenerate calibration; check the standards and instrument trace format")

    @staticmethod
    def _identity(value):
        # Firmware revision (fourth IDN field) may change without changing the device.
        fields = value.strip().split(',')
        return tuple(field.strip() for field in fields[:3])

    def validate_context(self, frequencies, instrument_id, settings):
        frequencies = np.asarray(frequencies, dtype=float)
        if self._identity(instrument_id) != self._identity(self.instrument_id):
            raise ValueError("Calibration belongs to a different instrument")
        if (frequencies.shape != self.frequencies.shape
                or not np.allclose(frequencies, self.frequencies, rtol=1e-10, atol=1e-3)):
            raise ValueError("Sweep changed: calibration frequency grid does not match")
        for key, expected in self.settings.items():
            actual = settings.get(key)
            if actual is None or not np.isclose(actual, expected, rtol=1e-10, atol=1e-6):
                raise ValueError(f"Sweep changed: calibration {key} does not match")

    def apply(self, raw):
        raw = np.asarray(raw, dtype=complex)
        if raw.shape != self.frequencies.shape or not np.all(np.isfinite(raw)):
            raise ValueError("Measurement data does not match the calibration")
        denominator = self._b + self.source_match * raw
        if np.any(np.abs(denominator) < 1e-15):
            raise ValueError("Calibration correction is singular for this measurement")
        result = (raw - self.directivity) / denominator
        if not np.all(np.isfinite(result)):
            raise ValueError("Calibration produced invalid values")
        return result

    def to_dict(self):
        return {
            "format": self.FORMAT,
            "schema_version": self.SCHEMA_VERSION,
            "standard_model": "ideal-50-ohm-osl",
            "reference_impedance_ohm": 50,
            "instrument_id": self.instrument_id,
            "created_utc": self.created_utc,
            "settings": self.settings,
            "frequencies_hz": self.frequencies.tolist(),
            "standards": {
                name: [[float(v.real), float(v.imag)] for v in self.standards[name]]
                for name in self.STANDARD_NAMES
            },
        }

    @classmethod
    def from_dict(cls, document):
        if not isinstance(document, dict) or document.get('format') != cls.FORMAT:
            raise ValueError("Not a Siglent software OSL calibration JSON file")
        if document.get('schema_version') != cls.SCHEMA_VERSION:
            raise ValueError("Unsupported calibration JSON version")
        if document.get('standard_model') != 'ideal-50-ohm-osl' or document.get('reference_impedance_ohm') != 50:
            raise ValueError("Unsupported calibration standard model")
        try:
            standards = {}
            for name in cls.STANDARD_NAMES:
                pairs = np.asarray(document['standards'][name], dtype=float)
                if pairs.ndim != 2 or pairs.shape[1] != 2:
                    raise ValueError(f"Invalid {name.upper()} real/imaginary pairs")
                standards[name] = pairs[:, 0] + 1j * pairs[:, 1]
            return cls(document['frequencies_hz'], standards, document['instrument_id'],
                       document['settings'], document.get('created_utc'))
        except (KeyError, TypeError, OverflowError) as exc:
            raise ValueError("Calibration JSON is incomplete or malformed") from exc

    @property
    def fingerprint(self):
        content = json.dumps(self.to_dict(), sort_keys=True, allow_nan=False).encode('utf-8')
        return hashlib.sha256(content).hexdigest()
