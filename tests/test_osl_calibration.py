"""Run with: python -m unittest discover -s tests -v."""
import json
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from osl_calibration import OSLCalibration


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.freqs = np.linspace(1e5, 1e9, 101)
        self.settings = dict(start_hz=1e5, stop_hz=1e9, points=101, bandwidth_hz=1000.0)
        self.identity = 'SIGLENT,SVA1032X,TEST123,1.0'
        phase = np.linspace(0, 1, self.freqs.size)
        self.d = .03 * np.exp(1j * phase)
        self.t = .8 * np.exp(-1j * phase)
        self.s = .12 * np.exp(2j * phase)
        self.standards = {key: self.distort(np.full(101, value, dtype=complex))
                          for key, value in [('short', -1), ('open', 1), ('load', 0)]}
        self.cal = OSLCalibration(self.freqs, self.standards, self.identity, self.settings)

    def distort(self, reflection):
        return self.d + self.t * reflection / (1 - self.s * reflection)

    def test_recovers_independent_networks_and_error_coefficients(self):
        # Independent forward error model, including zero load and reflections >1.
        for g in [np.zeros(101), np.full(101, -1), np.full(101, 1),
                  .7*np.exp(1j*np.linspace(-3, 3, 101)), np.full(101, 1.2+.1j)]:
            np.testing.assert_allclose(self.cal.apply(self.distort(g)), g, atol=1e-12)
        np.testing.assert_allclose(self.cal.directivity, self.d, atol=1e-12)
        np.testing.assert_allclose(self.cal.source_match, self.s, atol=1e-12)
        np.testing.assert_allclose(self.cal.reflection_tracking, self.t, atol=1e-12)

    def test_json_roundtrip_preserves_correction_and_fingerprint(self):
        loaded = OSLCalibration.from_dict(json.loads(json.dumps(self.cal.to_dict())))
        g = np.full(101, .35-.21j)
        np.testing.assert_allclose(loaded.apply(self.distort(g)), g, atol=1e-12)
        self.assertEqual(loaded.fingerprint, self.cal.fingerprint)

    def test_different_device_sweep_and_bandwidth_rejected(self):
        self.cal.validate_context(self.freqs, self.identity.replace('1.0', '2.0'), self.settings)
        with self.assertRaisesRegex(ValueError, 'different instrument'):
            self.cal.validate_context(self.freqs, 'SIGLENT,SVA1032X,OTHER,1.0', self.settings)
        with self.assertRaisesRegex(ValueError, 'frequency grid'):
            self.cal.validate_context(self.freqs+100, self.identity, self.settings)
        with self.assertRaisesRegex(ValueError, 'bandwidth'):
            self.cal.validate_context(self.freqs, self.identity, dict(self.settings, bandwidth_hz=10))

    def test_incomplete_nonfinite_and_singular_standards_rejected(self):
        for name in self.cal.STANDARD_NAMES:
            missing = dict(self.standards); missing.pop(name)
            with self.assertRaises(ValueError):
                OSLCalibration(self.freqs, missing, self.identity, self.settings)
        singular = {name: np.ones(101) for name in self.cal.STANDARD_NAMES}
        with self.assertRaises(ValueError):
            OSLCalibration(self.freqs, singular, self.identity, self.settings)
        bad = dict(self.standards); bad['short'] = np.full(101, np.nan)
        with self.assertRaises(ValueError):
            OSLCalibration(self.freqs, bad, self.identity, self.settings)

    def test_malformed_json_and_measurements_rejected(self):
        for data in [{}, [], {'format': 'nanovna'}, dict(self.cal.to_dict(), schema_version=9),
                     dict(self.cal.to_dict(), standards={'short': [1, 2]})]:
            with self.assertRaises(ValueError):
                OSLCalibration.from_dict(data)
        for data in [np.ones(100), np.full(101, np.inf)]:
            with self.assertRaises(ValueError):
                self.cal.apply(data)


if __name__ == '__main__':
    unittest.main()
