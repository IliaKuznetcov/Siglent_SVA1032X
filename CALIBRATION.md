# One-port software OSL calibration

The application measures port 1 reflection using ideal 50-ohm SHORT, OPEN, and
LOAD standards. The error model at each frequency is:

`m = d + t * Gamma / (1 - s * Gamma)`

Here `m` is the measured reflection, `Gamma` is the corrected reflection, `d` is
directivity, `s` is source match, and `t` is reflection tracking. The three measured
standards determine these complex coefficients. Singular or ill-conditioned
standard sets are rejected. Correction is applied in Python to subsequent traces.
A zero-valued measured load is valid when the other standards are distinct.

## Instrument commands

The command mapping follows SIGLENT Programming Guide PG0703P_E02B:

| Command | Purpose | Printed pages |
| --- | --- | --- |
| `CORRection:COLLect:CLEar` | Clear native calibration before software OSL | 110 |
| `CORRection:COLLect:METHod:TYPE?` | Require `NONE` for software correction | 110 |
| `CORRection:EXTension:STATe OFF` and query | Disable and verify port extension | 112 |
| `CALCulate:PARameter1:DEFine S11` and `:SELect` | Select S11 on trace 1 | 95-96 |
| `CALCulate:FORMat SCOMplex` | Complex reflection display data | 96 |
| `CALCulate:MATH:FUNCtion OFF`, `TRACe1:HOLD OFF` | Disable trace math/hold | 97-98 |
| `FORMat ASCii` | ASCII trace transfer | 99 |
| `CALCulate:DATA:FDATa?` | Retrieve interleaved complex trace values | 98-99 |
| `SENSe:FREQuency:STARt?`, `:STOP?` | Read actual sweep limits | 91 |
| `SENSe:SWEep:POINts?`, `SENSe:BWIDth?` | Read points and IF bandwidth | 93-94 |
| `INITiate:IMMediate`, `*OPC?` | Trigger and wait for completion | 94; common commands |

The guide also documents native standard collection and `COLLect:SAVE` (end
calibration), but does not document VNA error-coefficient export/import as JSON.
The application JSON workflow therefore uses its own software correction rather
than claiming to export the instrument's native calibration.

## JSON contents

The format identifier is `siglent-sva1032x-software-osl`, schema version 1.
Files contain the ideal standard model, 50-ohm reference impedance, instrument
identity, creation timestamp, start/stop frequencies, points, IF bandwidth,
frequency axis, and complex measured standards as real/imaginary pairs.
Coefficients are recalculated and validated on load. Calibration fingerprints
are SHA-256 hashes of the canonical JSON profile. The identifier and capture time
are shown in the ready indicator's tooltip, rather than in the status text.
**Disable Software Cal** affects future measurements only; stored results retain
their original measurement values and calibration provenance.

Loading checks the instrument manufacturer/model/serial identity (firmware field
excluded), point count, frequency axis, and IF bandwidth. It then clears native
calibration and disables port extension after user confirmation. Files cannot
verify cable movement, standard quality, reference-plane changes, RF settings,
or temperature. Recalibrate when the measurement setup changes.

A three-standard fit reproducing its own standards is not independent validation.
After calibration, measure a separate known termination, attenuator/termination,
or other characterized device across the sweep to evaluate physical accuracy.

## Running checks

From the project root with dependencies installed:

```powershell
python -m unittest discover -s tests -v
```

The numerical tests use an independent forward error model. The GUI tests use
a simulated VISA instrument. Neither replaces validation with the SVA1032X.
