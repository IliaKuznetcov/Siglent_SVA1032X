# Siglent SVA1032X v1.0.0

PySide6 desktop application for Siglent SVA1032X S11 acquisition and logging.

- USB VISA/SCPI communication with resource selection, refresh and disconnect.
- Windows application icon and executable version metadata.
- Manual and timed acquisition.
- Ideal 50-ohm software SHORT/OPEN/LOAD calibration with JSON save/load.
- Instrument/sweep checks and raw-trace/calibration provenance in SQLite.
- SQLite measurement storage with descriptions and UTC timestamps.
- Magnitude, phase, real, imaginary, and Smith plots.
- Editable recorded descriptions, selected-trace comparison and CSV export.

Download `Siglent_SVA1032X_v1.0.0_Windows.zip`, extract it into a writable
folder, and run `Siglent_SVA1032X.exe`. A compatible VISA runtime and instrument
drivers are required. Select or enter the instrument VISA resource address in the application. See README for measurement setup.
