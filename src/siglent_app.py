import sys, json, threading, sqlite3, time, csv
from pathlib import Path
import numpy as np
import pyvisa
from datetime import datetime, timezone
from PySide6 import QtWidgets, QtCore, QtGui
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
import skrf as rf
from osl_calibration import OSLCalibration


# ---------------- Siglent wrapper ----------------
class SiglentVNA:
    def __init__(self, usb_resource):
        self.rm = pyvisa.ResourceManager()
        self.instrument = None
        try:
            self.instrument = self.rm.open_resource(usb_resource)
            self.instrument.timeout = 20000
            self.start_hz = 1e5
            self.stop_hz = 1e9
            self.points = 601
            self.switch_to_vna_mode()
            self.set_frequency(self.start_hz, self.stop_hz, self.points)
            self.instrument.write('INITiate:CONTinuous 0')
            self.instrument_id = self.instrument.query('*IDN?').strip()
            if not self.instrument_id:
                raise RuntimeError('Instrument identification query returned no identity')
            time.sleep(1)
        except Exception:
            self.close()
            raise

    def wait_complete(self):
        if self.instrument.query('*OPC?').strip() != '1':
            raise RuntimeError('Instrument did not confirm operation completion')

    def switch_to_vna_mode(self):
        current_mode = self.instrument.query('INST:SEL?').strip().strip('"').upper()
        if current_mode != 'VNA':
            self.instrument.write('INST:SEL VNA')
            time.sleep(1)
        if self.instrument.query('INST:SEL?').strip().strip('"').upper() != 'VNA':
            raise RuntimeError('Instrument did not enter VNA mode')

    def set_frequency(self, start_hz, stop_hz, points):
        self.start_hz, self.stop_hz, self.points = start_hz, stop_hz, points
        self.instrument.write(f'SENSe:FREQuency:START {start_hz}')
        self.instrument.write(f'SENSe:FREQuency:STOP {stop_hz}')
        self.instrument.write(f'SENSe:SWEep:POINts {points}')
        self.freqs = np.linspace(start_hz, stop_hz, points)

    def read_settings(self):
        settings = {
            'start_hz': float(self.instrument.query('SENSe:FREQuency:STARt?')),
            'stop_hz': float(self.instrument.query('SENSe:FREQuency:STOP?')),
            'points': int(self.instrument.query('SENSe:SWEep:POINts?').strip()),
            'bandwidth_hz': float(self.instrument.query('SENSe:BWIDth?')),
        }
        if (not all(np.isfinite(v) and v > 0 for v in settings.values())
                or settings['stop_hz'] <= settings['start_hz']
                or not 101 <= settings['points'] <= 751):
            raise ValueError('Instrument returned invalid VNA sweep settings')
        self.start_hz = settings['start_hz']
        self.stop_hz = settings['stop_hz']
        self.points = settings['points']
        self.freqs = np.linspace(self.start_hz, self.stop_hz, self.points)
        return settings

    def configure_complex_s11(self):
        # PG0703P_E02B, VNA Trace Subsection (printed pp. 95-99).
        self.instrument.write('CALCulate:PARameter1:DEFine S11')
        self.instrument.write('CALCulate:PARameter1:SELect')
        self.instrument.write('CALCulate:FORMat SCOMplex')
        self.instrument.write('CALCulate:MATH:FUNCtion OFF')
        self.instrument.write('TRACe1:HOLD OFF')
        self.instrument.write('FORMat ASCii')
        parameter = self.instrument.query('CALCulate:PARameter1:DEFine?').strip().strip('"').upper()
        trace_format = self.instrument.query('CALCulate:FORMat?').strip().strip('"').upper()
        if parameter != 'S11' or trace_format not in ('SCOM', 'SCOMPLEX'):
            raise RuntimeError('Instrument did not confirm complex S11 trace format')

    def prepare_host_calibration(self):
        # This deliberately clears existing instrument calibration data.
        self.instrument.write('CORRection:COLLect:CLEar')
        self.instrument.write('CORRection:EXTension:STATe OFF')
        self.wait_complete()
        self.assert_host_baseline()
        self.configure_complex_s11()

    def assert_host_baseline(self):
        method = self.instrument.query('CORRection:COLLect:METHod:TYPE?').strip().strip('"').upper()
        if method != 'NONE':
            raise RuntimeError('Instrument calibration is present; start/load software OSL again to avoid double correction')
        extension = self.instrument.query('CORRection:EXTension:STATe?').strip().strip('"').upper()
        if extension not in ('0', 'OFF'):
            raise RuntimeError('Instrument port extension is enabled; start/load software OSL again')

    def trigger_measurement(self):
        self.instrument.write('INITiate:IMMediate')
        self.wait_complete()
        time.sleep(0.5)

    def readValues(self, channel='S11'):
        if channel != 'S11':
            raise ValueError('This application supports S11 acquisition only')
        self.configure_complex_s11()
        self.trigger_measurement()
        raw = self.instrument.query('CALCulate:DATA:FDATa?')
        try:
            values = np.asarray([float(token) for token in raw.strip().split(',')], dtype=float)
        except ValueError as exc:
            raise ValueError('Instrument returned nonnumeric complex trace data') from exc
        if values.size != 2 * self.points or not np.all(np.isfinite(values)):
            raise ValueError(f'Expected {2 * self.points} finite real/imaginary values, got {values.size}')
        return values[0::2] + 1j * values[1::2]

    def close(self):
        try:
            if self.instrument is not None:
                self.instrument.close()
        finally:
            self.rm.close()


# ---------------- DB helper ----------------
class MeasurementDB:
    def __init__(self, path="vna_measurements.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        self._ensure()

    def _ensure(self):
        cur = self.conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS measurements (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            start_hz INTEGER,
            stop_hz INTEGER,
            points INTEGER,
            freqs_json TEXT,
            s11_json TEXT,
            note TEXT,
            auto INTEGER
        )
        """)
        columns = {row[1] for row in cur.execute("PRAGMA table_info(measurements)")}
        if 'calibration_json' not in columns:
            cur.execute("ALTER TABLE measurements ADD COLUMN calibration_json TEXT")
        if 'raw_s11_json' not in columns:
            cur.execute("ALTER TABLE measurements ADD COLUMN raw_s11_json TEXT")
        self.conn.commit()

    def insert(self, ts, start_hz, stop_hz, points, freqs, s11, note="", auto=0,
               calibration_json=None, raw_s11=None):
        with self.lock:
            s11_serializable = [(c.real, c.imag) for c in s11]
            cur = self.conn.cursor()
            cur.execute(
                "INSERT INTO measurements (ts,start_hz,stop_hz,points,freqs_json,s11_json,note,auto,calibration_json,raw_s11_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, start_hz, stop_hz, points,
                 json.dumps(freqs.tolist()),
                 json.dumps(s11_serializable),
                 note,
                 int(auto), calibration_json,
                 json.dumps([[float(v.real), float(v.imag)] for v in raw_s11])
                 if raw_s11 is not None else None)
            )
            self.conn.commit()
            return cur.lastrowid

    def query_all(self):
        cur = self.conn.cursor()
        cur.execute("SELECT id, ts, points, note, auto FROM measurements ORDER BY ts DESC")
        return cur.fetchall()

    def fetch_by_id(self, mid):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM measurements WHERE id=?", (mid,))
        row = cur.fetchone()
        if row:
            keys = [d[0] for d in cur.description]
            return dict(zip(keys, row))
        return None

    def update_note(self, mid, note):
        with self.lock, self.conn:
            cur = self.conn.execute("UPDATE measurements SET note=? WHERE id=?", (note, mid))
            if cur.rowcount != 1:
                raise ValueError("Measurement no longer exists")

    def delete_by_id(self, mid):
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("DELETE FROM measurements WHERE id=?", (mid,))
            self.conn.commit()


# ---------------- Plot canvas ----------------
class PlotCanvas(FigureCanvas):
    def __init__(self, parent=None, dpi=100):
        self.fig = Figure(dpi=dpi)
        super().__init__(self.fig)

    def plot_s11(self, freqs_list, s11_list, plot_type="Magnitude", labels=None):
        self.fig.clear()

        if labels is None:
            labels = [f"Trace {i+1}" for i in range(len(s11_list))]

        if plot_type.lower() == "smith":
            ax = self.fig.add_subplot(111)
            for idx, (freqs, s11) in enumerate(zip(freqs_list, s11_list)):
                ntw = rf.Network(
                    frequency=rf.Frequency.from_f(freqs, unit="Hz"),
                    s=np.array(s11).reshape(-1, 1, 1)
                )
                ntw.plot_s_smith(m=0, ax=ax, label=labels[idx])
            ax.grid(True)
            ax.legend()
            self.draw()
            return

        ax = self.fig.add_subplot(111)

        for idx, (freqs, s11) in enumerate(zip(freqs_list, s11_list)):
            freqs_mhz = np.array(freqs) / 1e6

            if plot_type.lower() == "magnitude":
                ax.plot(freqs_mhz, 20*np.log10(np.abs(s11)+1e-12), label=labels[idx])
                ax.set_ylabel("Magnitude (dB)")
            elif plot_type.lower() == "phase":
                ax.plot(freqs_mhz, np.angle(s11, deg=True), label=labels[idx])
                ax.set_ylabel("Phase (deg)")
            elif plot_type.lower() == "real":
                ax.plot(freqs_mhz, np.real(s11), label=labels[idx])
                ax.set_ylabel("Real")
            elif plot_type.lower() == "imag":
                ax.plot(freqs_mhz, np.imag(s11), label=labels[idx])
                ax.set_ylabel("Imag")

        ax.set_xlabel("Frequency (MHz)")
        ax.grid(True)
        ax.legend()
        self.draw()


# ---------------- Main Window ----------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Siglent SVA1032X")
        self.setWindowIcon(QtGui.QIcon(str(Path(__file__).with_name("SVA_logo.ico"))))
        self.resize(1300, 750)

        self.db = MeasurementDB()
        self.vna = None
        self.freqs = None
        self.calibration = None
        self.calibration_standards = {}
        self.calibration_settings = None
        self.calibration_freqs = None
        self.software_calibration_mode = False

        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.trigger_measurement)
        self.measure_interval_ms = 5000

        self._build_ui()
        self.refresh_resources()

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        connection_row = QtWidgets.QHBoxLayout()
        connection_row.addWidget(QtWidgets.QLabel("VISA resource:"))
        self.resource_combo = QtWidgets.QComboBox()
        self.resource_combo.setEditable(True)
        self.resource_combo.setMinimumWidth(400)
        self.resource_combo.setToolTip("Select a detected VISA address or enter one manually (USB or LAN).")
        connection_row.addWidget(self.resource_combo, 1)
        self.refresh_resources_btn = QtWidgets.QPushButton("Refresh Resources")
        self.refresh_resources_btn.clicked.connect(self.refresh_resources)
        connection_row.addWidget(self.refresh_resources_btn)
        self.disconnect_btn = QtWidgets.QPushButton("Disconnect")
        self.disconnect_btn.setEnabled(False)
        self.disconnect_btn.clicked.connect(self.on_disconnect)

        # Top controls
        top = QtWidgets.QHBoxLayout()

        self.connect_btn = QtWidgets.QPushButton("Connect")
        self.connect_btn.clicked.connect(self.on_connect)

        self.trigger_btn = QtWidgets.QPushButton("Trigger")
        self.trigger_btn.clicked.connect(self.trigger_measurement)

        self.plot_type_combo = QtWidgets.QComboBox()
        self.plot_type_combo.addItems(["Magnitude","Phase","Real","Imag","Smith"])

        self.interval_edit = QtWidgets.QLineEdit(str(self.measure_interval_ms//1000))
        self.interval_edit.setFixedWidth(60)
        self.interval_edit.setValidator(QtGui.QIntValidator(1, 3600))

        self.timer_btn = QtWidgets.QPushButton("Start Auto")
        self.timer_btn.setCheckable(True)
        self.timer_btn.clicked.connect(self.toggle_timer)

        self.desc_edit = QtWidgets.QLineEdit()
        self.desc_edit.setPlaceholderText("Optional description")
        self.desc_edit.setFixedWidth(200)

        connection_row.addWidget(self.connect_btn)
        connection_row.addWidget(self.disconnect_btn)
        layout.addLayout(connection_row)
        top.addWidget(self.trigger_btn)
        top.addWidget(QtWidgets.QLabel("Plot Type:"))
        top.addWidget(self.plot_type_combo)
        top.addWidget(QtWidgets.QLabel("Interval (s):"))
        top.addWidget(self.interval_edit)
        top.addWidget(self.timer_btn)
        top.addWidget(QtWidgets.QLabel("Description:"))
        top.addWidget(self.desc_edit)

        layout.addLayout(top)

        calibration_row = QtWidgets.QHBoxLayout()
        calibration_row.addWidget(QtWidgets.QLabel("Software OSL (50 Ω):"))
        self.new_cal_btn = QtWidgets.QPushButton("New OSL")
        self.new_cal_btn.clicked.connect(self.start_calibration)
        calibration_row.addWidget(self.new_cal_btn)
        self.standard_buttons = {}
        for name in OSLCalibration.STANDARD_NAMES:
            button = QtWidgets.QPushButton(name.upper())
            button.setEnabled(False)
            button.clicked.connect(lambda checked=False, standard=name: self.capture_standard(standard))
            self.standard_buttons[name] = button
            calibration_row.addWidget(button)
        self.save_cal_btn = QtWidgets.QPushButton("Save Cal JSON")
        self.save_cal_btn.clicked.connect(self.save_calibration_dialog)
        self.save_cal_btn.setEnabled(False)
        calibration_row.addWidget(self.save_cal_btn)
        self.load_cal_btn = QtWidgets.QPushButton("Load Cal JSON")
        self.load_cal_btn.clicked.connect(self.load_calibration_dialog)
        calibration_row.addWidget(self.load_cal_btn)
        self.end_cal_btn = QtWidgets.QPushButton("Disable Software Cal")
        self.end_cal_btn.clicked.connect(self.use_instrument_calibration)
        self.end_cal_btn.setToolTip("Disable software correction for future measurements. Saved results are unchanged. Does not restore cleared instrument calibration.")
        calibration_row.addWidget(self.end_cal_btn)
        calibration_row.addStretch()
        layout.addLayout(calibration_row)
        self.calibration_status = QtWidgets.QLabel("Software calibration off — using instrument output")
        layout.addWidget(self.calibration_status)

        # Plot + Table
        middle = QtWidgets.QHBoxLayout()

        plot_layout = QtWidgets.QVBoxLayout()
        self.canvas = PlotCanvas(self)
        self.toolbar = NavigationToolbar(self.canvas, self)
        plot_layout.addWidget(self.canvas)
        plot_layout.addWidget(self.toolbar)

        middle.addLayout(plot_layout, stretch=3)

        right_layout = QtWidgets.QVBoxLayout()

        self.table = QtWidgets.QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(['ID','Timestamp','Points','Description','Auto'])
        self.table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.MultiSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.DoubleClicked | QtWidgets.QAbstractItemView.EditKeyPressed)
        self.table.itemChanged.connect(self.on_table_item_changed)
        self.table.setToolTip("Double-click a Description cell to edit it; press Enter to save.")

        right_layout.addWidget(self.table)

        self.plot_selected_btn = QtWidgets.QPushButton("Plot Selected")
        self.plot_selected_btn.clicked.connect(self.plot_selected)

        self.delete_selected_btn = QtWidgets.QPushButton("Delete Selected")
        self.delete_selected_btn.clicked.connect(self.delete_selected)

        self.export_selected_btn = QtWidgets.QPushButton("Export Selected to CSV")
        self.export_selected_btn.clicked.connect(self.export_selected_csv)

        right_layout.addWidget(self.plot_selected_btn)
        right_layout.addWidget(self.delete_selected_btn)
        right_layout.addWidget(self.export_selected_btn)

        middle.addLayout(right_layout, stretch=2)
        layout.addLayout(middle)

        self.status = QtWidgets.QLabel("Idle")
        layout.addWidget(self.status)

        self.load_table()

    def toggle_timer(self):
        if self.timer_btn.isChecked():
            if not self.vna or (self.software_calibration_mode and self.calibration is None):
                self._stop_auto()
                self.status.setText("Connect and complete OSL calibration first")
                return
            if not self.interval_edit.hasAcceptableInput():
                self._stop_auto()
                self.status.setText("Enter an interval from 1 to 3600 seconds")
                return
            interval_sec = int(self.interval_edit.text())
            self.measure_interval_ms = interval_sec * 1000
            self.timer.start(self.measure_interval_ms)
            self.timer_btn.setText("Stop Auto")
            self.status.setText(f"Auto measurement every {interval_sec}s")
        else:
            self.timer.stop()
            self.timer_btn.setText("Start Auto")
            self.status.setText("Auto measurement stopped")

    def _stop_auto(self):
        self.timer.stop()
        self.timer_btn.setChecked(False)
        self.timer_btn.setText("Start Auto")

    def _reset_host_calibration(self):
        self.calibration = None
        self.calibration_standards = {}
        self.calibration_settings = None
        self.calibration_freqs = None
        self.software_calibration_mode = False
        self.calibration_status.setToolTip("")
        self.save_cal_btn.setEnabled(False)
        for name, button in self.standard_buttons.items():
            button.setEnabled(False)
            button.setText(name.upper())

    def refresh_resources(self):
        if self.vna is not None:
            self.status.setText("Disconnect before refreshing VISA resources")
            return
        previous = self.resource_combo.currentText().strip()
        manager = None
        try:
            manager = pyvisa.ResourceManager()
            resources = sorted(manager.list_resources())
        except Exception as e:
            self.status.setText(f"Could not list VISA resources: {e}. Enter an address manually or check the VISA runtime.")
            return
        finally:
            if manager is not None:
                manager.close()
        self.resource_combo.clear()
        self.resource_combo.addItems(resources)
        if previous:
            match = self.resource_combo.findText(previous)
            if match >= 0:
                self.resource_combo.setCurrentIndex(match)
            else:
                self.resource_combo.setEditText(previous)
        elif not resources:
            self.resource_combo.setEditText("")
        self.status.setText("Select a VISA resource and click Connect" if resources
                            else "No VISA instruments found. Refresh after connecting, or enter an address manually.")

    def _set_connection_controls(self, connected):
        self.connect_btn.setEnabled(not connected)
        self.resource_combo.setEnabled(not connected)
        self.refresh_resources_btn.setEnabled(not connected)
        self.disconnect_btn.setEnabled(connected)

    def on_disconnect(self):
        self._stop_auto()
        previous = self.vna
        self.vna = None
        self.freqs = None
        self._reset_host_calibration()
        self.calibration_status.setText("Disconnected — software calibration off")
        self._set_connection_controls(False)
        try:
            if previous is not None:
                previous.close()
            self.status.setText("Disconnected. Load a matching calibration JSON or capture OSL after reconnecting.")
        except Exception as e:
            self.status.setText(f"Connection closed with an error: {e}")

    def on_connect(self):
        resource = self.resource_combo.currentText().strip().strip('"')
        if not resource:
            self.status.setText("Select or enter a VISA resource first")
            return
        self._stop_auto()
        self._reset_host_calibration()
        self.calibration_status.setText("Software calibration off — using instrument output")
        try:
            previous = self.vna
            self.vna = None
            if previous is not None:
                previous.close()
            self.vna = SiglentVNA(resource)
            self.freqs = self.vna.freqs
            self._set_connection_controls(True)
            self.status.setText(f"Connected to {self.vna.instrument_id}")
        except Exception as e:
            self._set_connection_controls(False)
            self.status.setText(f"Connect failed: {e}")

    def _show_calibration_ready(self):
        self.calibration_status.setText("Software OSL ready — correction enabled")
        self.calibration_status.setToolTip(
            "Calibration identifier (SHA-256): " + self.calibration.fingerprint
            + "\nCaptured: " + self.calibration.created_utc
        )

    def _confirm_host_baseline(self):
        return QtWidgets.QMessageBox.question(
            self, "Software OSL calibration",
            "This clears the Siglent's existing calibration data and disables port extension. "
            "Correction will be applied by this application using ideal 50-ohm OSL standards. "
            "Keep the same cables and reference plane for all standards and measurements. Continue?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        ) == QtWidgets.QMessageBox.Yes

    def start_calibration(self):
        self._stop_auto()
        if not self.vna:
            self.status.setText("Connect first")
            return
        if not self._confirm_host_baseline():
            return
        self._reset_host_calibration()
        self.software_calibration_mode = True
        self.calibration_status.setText("Software OSL incomplete; measurements blocked")
        try:
            self.vna.prepare_host_calibration()
            self.calibration_settings = self.vna.read_settings()
            self.calibration_freqs = self.vna.freqs.copy()
            for button in self.standard_buttons.values():
                button.setEnabled(True)
            self.status.setText("Connect each standard at port 1 and capture SHORT, OPEN and LOAD")
        except Exception as e:
            self.status.setText(f"Could not start calibration: {e}")

    def _validate_host_context(self, settings):
        if self.calibration is not None:
            self.calibration.validate_context(self.vna.freqs, self.vna.instrument_id, settings)
        elif settings != self.calibration_settings:
            raise ValueError("Sweep settings changed; start a new OSL calibration")
        self.vna.assert_host_baseline()

    def capture_standard(self, name):
        self._stop_auto()
        if not self.vna or not self.software_calibration_mode or self.calibration_settings is None:
            self.status.setText("Connect and click New OSL first")
            return
        if QtWidgets.QMessageBox.question(
            self, "Capture calibration standard",
            f"Connect the {name.upper()} standard at the port 1 reference plane, then press Yes.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        ) != QtWidgets.QMessageBox.Yes:
            return
        try:
            settings = self.vna.read_settings()
            self._validate_host_context(settings)
            raw = self.vna.readValues("S11")
            after = self.vna.read_settings()
            if after != settings:
                raise ValueError("Sweep changed during standard capture; capture again")
            self.vna.assert_host_baseline()
            self.calibration = None
            self.save_cal_btn.setEnabled(False)
            self.calibration_standards[name] = raw.copy()
            self.standard_buttons[name].setText(f"{name.upper()} ✓")
            missing = [key.upper() for key in OSLCalibration.STANDARD_NAMES
                       if key not in self.calibration_standards]
            self.calibration_status.setText("Software OSL incomplete: " + ", ".join(missing))
            if not missing:
                self.calibration = OSLCalibration(
                    self.calibration_freqs, self.calibration_standards,
                    self.vna.instrument_id, self.calibration_settings,
                )
                self.save_cal_btn.setEnabled(True)
                self._show_calibration_ready()
            self.status.setText(f"{name.upper()} captured" + ("; OSL ready" if self.calibration else ""))
        except Exception as e:
            self.status.setText(f"Calibration capture failed: {e}")

    def save_calibration_dialog(self):
        if self.calibration is None:
            self.status.setText("Complete SHORT, OPEN and LOAD first")
            return
        filename, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save software OSL calibration", "siglent_osl_calibration.json", "JSON files (*.json)"
        )
        if not filename:
            return
        try:
            Path(filename).write_text(json.dumps(self.calibration.to_dict(), indent=2, allow_nan=False), encoding='utf-8')
            self.status.setText(f"Calibration saved to {filename}")
        except Exception as e:
            self.status.setText(f"Could not save calibration: {e}")

    def load_calibration_dialog(self):
        self._stop_auto()
        if not self.vna:
            self.status.setText("Connect first")
            return
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load software OSL calibration", "", "JSON files (*.json)"
        )
        if not filename:
            return
        try:
            candidate = OSLCalibration.from_dict(json.loads(Path(filename).read_text(encoding='utf-8')))
            settings = self.vna.read_settings()
            candidate.validate_context(self.vna.freqs, self.vna.instrument_id, settings)
            if not self._confirm_host_baseline():
                return
            self._reset_host_calibration()
            self.software_calibration_mode = True
            self.calibration_status.setText("Software OSL incomplete; measurements blocked")
            self.vna.prepare_host_calibration()
            settings = self.vna.read_settings()
            candidate.validate_context(self.vna.freqs, self.vna.instrument_id, settings)
            self.calibration = candidate
            self.calibration_settings = settings
            self.calibration_freqs = self.vna.freqs.copy()
            self.calibration_standards = {key: value.copy() for key, value in candidate.standards.items()}
            for name, button in self.standard_buttons.items():
                button.setEnabled(True)
                button.setText(f"{name.upper()} ✓")
            self.save_cal_btn.setEnabled(True)
            self._show_calibration_ready()
            self.status.setText(f"Calibration loaded from {filename}")
        except Exception as e:
            self.status.setText(f"Could not load calibration: {e}")

    def use_instrument_calibration(self):
        self._stop_auto()
        self._reset_host_calibration()
        self.calibration_status.setText("Software calibration off — using instrument output")
        self.status.setText("Software correction off. Any previously cleared instrument calibration must be redone on the Siglent.")

    def closeEvent(self, event):
        self._stop_auto()
        try:
            if self.vna is not None:
                self.vna.close()
        finally:
            self.db.conn.close()
        super().closeEvent(event)

    def trigger_measurement(self):
        if self.table.state() == QtWidgets.QAbstractItemView.EditingState:
            self.status.setText("Finish editing the description before recording another measurement")
            return
        if not self.vna:
            self.status.setText("Connect first")
            return

        try:
            settings = self.vna.read_settings()
            if self.software_calibration_mode:
                if self.calibration is None:
                    raise ValueError("Complete SHORT, OPEN and LOAD, or load a matching JSON calibration")
                self._validate_host_context(settings)
            raw = self.vna.readValues("S11")
            after = self.vna.read_settings()
            if after != settings:
                raise ValueError("Sweep changed during acquisition; measurement not saved")
            if self.software_calibration_mode:
                self._validate_host_context(after)
                s11 = self.calibration.apply(raw)
                calibration_json = json.dumps(self.calibration.to_dict(), allow_nan=False)
            else:
                s11 = raw
                calibration_json = None
            self.freqs = self.vna.freqs.copy()
            ts = datetime.now(timezone.utc).isoformat()
            self.last_measurement = s11

            descriptor = self.desc_edit.text().strip()
            is_auto = self.timer.isActive()

            # Build note
            if descriptor:
                note = descriptor
            else:
                note = "auto" if is_auto else "manual"

            new_id = self.db.insert(
                ts,
                self.vna.start_hz,
                self.vna.stop_hz,
                self.vna.points,
                self.freqs,
                s11,
                note=note,
                auto=int(is_auto),
                calibration_json=calibration_json,
                raw_s11=raw,
            )

            # Clear descriptor only for manual trigger
            if not is_auto:
                self.desc_edit.clear()

            # Plot immediately
            self.canvas.plot_s11(
                [self.freqs],
                [s11],
                plot_type=self.plot_type_combo.currentText(),
                labels=[f"ID {new_id} - {note}"]
            )

            self.load_table()
            correction_label = ("software OSL correction enabled"
                                if self.software_calibration_mode else "software correction off")
            self.status.setText(f"Measurement saved (ID {new_id}) | {correction_label}")

        except Exception as e:
            self._stop_auto()
            self.status.setText(f"Measurement failed: {e}")

    def load_table(self):
        rows = self.db.query_all()
        blocker = QtCore.QSignalBlocker(self.table)
        self.table.setRowCount(len(rows))
        for r, (mid, ts, points, note, auto) in enumerate(rows):
            values = [mid, ts, points, note or "", auto]
            for c, val in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(val))
                flags = QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled
                if c == 3:
                    flags |= QtCore.Qt.ItemIsEditable
                    item.setToolTip("Double-click to edit this description; press Enter to save.")
                item.setFlags(flags)
                item.setData(QtCore.Qt.UserRole, mid)
                self.table.setItem(r, c, item)
        del blocker
        self.table.resizeColumnsToContents()

    def on_table_item_changed(self, item):
        if item.column() != 3:
            return
        mid = item.data(QtCore.Qt.UserRole)
        try:
            self.db.update_note(mid, item.text())
            self.status.setText(f"Description saved for measurement ID {mid}")
            selected = self.table.selectionModel().selectedRows()
            if selected and any(row.row() == item.row() for row in selected):
                self.plot_selected()
        except Exception as e:
            previous = self.db.fetch_by_id(mid)
            blocker = QtCore.QSignalBlocker(self.table)
            item.setText(previous['note'] or "" if previous else "")
            del blocker
            self.status.setText(f"Could not save description: {e}")

    @staticmethod
    def _record_label(record):
        note = record['note'] or ""
        return f"ID {record['id']} - {note}" if note else f"ID {record['id']}"

    def plot_selected(self):
        selected_rows = self.table.selectionModel().selectedRows()
        freqs_list, s11_list, labels = [], [], []

        for row in selected_rows:
            mid = self.table.item(row.row(), 0).data(QtCore.Qt.UserRole)
            rec = self.db.fetch_by_id(mid)

            if rec:
                freqs = np.array(json.loads(rec["freqs_json"]))
                s11 = np.array([complex(r[0], r[1]) for r in json.loads(rec["s11_json"])])
                freqs_list.append(freqs)
                s11_list.append(s11)
                labels.append(self._record_label(rec))

        if freqs_list:
            self.canvas.plot_s11(freqs_list, s11_list,
                                 plot_type=self.plot_type_combo.currentText(),
                                 labels=labels)

    def delete_selected(self):
        selected_rows = sorted(
            [r.row() for r in self.table.selectionModel().selectedRows()],
            reverse=True
        )
        for row_idx in selected_rows:
            mid = self.table.item(row_idx, 0).data(QtCore.Qt.UserRole)
            self.db.delete_by_id(mid)
        self.load_table()

    def export_selected_csv(self):
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            return

        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save CSV", "vna_export.csv", "CSV Files (*.csv)"
        )
        if not file_path:
            return

        datasets = []
        for row in selected_rows:
            mid = self.table.item(row.row(), 0).data(QtCore.Qt.UserRole)
            rec = self.db.fetch_by_id(mid)
            if not rec:
                continue

            ts = rec["ts"].replace(":", "-")
            freqs = np.array(json.loads(rec["freqs_json"]))
            s11 = np.array([complex(r[0], r[1]) for r in json.loads(rec["s11_json"])])

            datasets.append((self._record_label(rec), ts, freqs, s11))

        if not datasets:
            return

        base_freqs = datasets[0][2]
        for _, _, frequencies, values in datasets:
            if (frequencies.shape != base_freqs.shape or values.shape != base_freqs.shape
                    or not np.array_equal(frequencies, base_freqs)):
                self.status.setText("CSV export needs matching frequency grids; export these measurements separately.")
                return
        try:
            with open(file_path, "w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                header = ["Frequency_Hz"]
                for label, ts, _, _ in datasets:
                    header.extend([f"S11_Real_{label}_{ts}", f"S11_Imag_{label}_{ts}"])
                writer.writerow(header)
                for i, frequency in enumerate(base_freqs):
                    row_data = [frequency]
                    for _, _, _, s11 in datasets:
                        row_data.extend([s11[i].real, s11[i].imag])
                    writer.writerow(row_data)
            self.status.setText(f"Exported {len(datasets)} measurement(s) to {file_path}")
        except Exception as e:
            self.status.setText(f"CSV export failed: {e}")


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())