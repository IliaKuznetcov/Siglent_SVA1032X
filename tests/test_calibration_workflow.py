"""Hardware-free GUI and SCPI integration checks using a simulated VISA instrument."""
import csv
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import siglent_app as app


class Instrument:
    def __init__(self):
        self.start = 1e5
        self.stop = 1e9
        self.points = 601
        self.bandwidth = 1000
        self.identity = 'SIGLENT,SVA1032X,TEST123,1.0'
        self.method = 'SOLT1'
        self.extension = '1'
        self.reflection = .25+.12j
        self.commands = []
        self.closed = False
        self.bad_trace = None

    def write(self, command):
        self.commands.append(command)
        if command == 'CORRection:COLLect:CLEar': self.method = 'NONE'
        elif command == 'CORRection:EXTension:STATe OFF': self.extension = '0'
        elif command.startswith('SENSe:FREQuency:START '): self.start = float(command.split()[-1])
        elif command.startswith('SENSe:FREQuency:STOP '): self.stop = float(command.split()[-1])
        elif command.startswith('SENSe:SWEep:POINts '): self.points = int(command.split()[-1])

    def query(self, command):
        self.commands.append(command)
        answers = {
            'INST:SEL?': 'VNA', '*IDN?': self.identity, '*OPC?': '1',
            'CALCulate:PARameter1:DEFine?': 'S11', 'CALCulate:FORMat?': 'SCOM',
            'SENSe:FREQuency:STARt?': str(self.start),
            'SENSe:FREQuency:STOP?': str(self.stop),
            'SENSe:SWEep:POINts?': str(self.points),
            'SENSe:BWIDth?': str(self.bandwidth),
            'CORRection:COLLect:METHod:TYPE?': self.method,
            'CORRection:EXTension:STATe?': self.extension,
        }
        if command in answers: return answers[command]
        if command == 'CALCulate:DATA:FDATa?':
            if self.bad_trace is not None: return self.bad_trace
            g = self.reflection
            measured = .04+.01j + (.8-.1j)*g/(1-(.08+.03j)*g)
            return ','.join([str(measured.real), str(measured.imag)] * self.points)
        raise AssertionError('Undocumented/unexpected query: ' + command)

    def close(self): self.closed = True


class Manager:
    def __init__(self, instrument): self.instrument=instrument; self.closed=False
    def list_resources(self): return ('USB0::TEST::INSTR',)
    def open_resource(self, address): self.opened_address=address; return self.instrument
    def close(self): self.closed=True


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = app.QtWidgets.QApplication.instance() or app.QtWidgets.QApplication([])

    def setUp(self):
        self.cwd = Path.cwd()
        self.temp = tempfile.TemporaryDirectory()
        os.chdir(self.temp.name)
        # A database written by the previous application must remain readable.
        db = sqlite3.connect('vna_measurements.db')
        db.execute('CREATE TABLE measurements (id INTEGER PRIMARY KEY, ts TEXT, start_hz INTEGER, stop_hz INTEGER, points INTEGER, freqs_json TEXT, s11_json TEXT, note TEXT, auto INTEGER)')
        db.commit();db.close()
        self.instrument = Instrument()
        self.manager = Manager(self.instrument)
        self.patches = [patch.object(app.pyvisa, 'ResourceManager', return_value=self.manager),
                        patch.object(app.time, 'sleep'),
                        patch.object(app.QtWidgets.QMessageBox, 'question', return_value=app.QtWidgets.QMessageBox.Yes)]
        for p in self.patches:p.start()
        self.window = app.MainWindow()
        self.window.on_connect()
        self.assertIsNotNone(self.window.vna, self.window.status.text())

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        app.QtCore.QCoreApplication.sendPostedEvents(None, app.QtCore.QEvent.DeferredDelete)
        self.qt.processEvents()
        for p in reversed(self.patches):p.stop()
        os.chdir(self.cwd)
        self.temp.cleanup()

    def capture_osl(self):
        self.window.start_calibration()
        for name, reflection in [('short', -1), ('open', 1), ('load', 0)]:
            self.instrument.reflection = reflection
            self.window.capture_standard(name)
        self.assertIsNotNone(self.window.calibration, self.window.status.text())

    def test_full_capture_json_restore_measurement_plots_and_csv(self):
        self.capture_osl()
        self.assertEqual(self.instrument.method, 'NONE')
        self.assertEqual(self.instrument.extension, '0')
        path = str(Path(self.temp.name)/'cal.json')
        with patch.object(app.QtWidgets.QFileDialog, 'getSaveFileName', return_value=(path,'')):
            self.window.save_calibration_dialog()
        self.window.use_instrument_calibration()
        with patch.object(app.QtWidgets.QFileDialog, 'getOpenFileName', return_value=(path,'')):
            self.window.load_calibration_dialog()
        self.assertIsNotNone(self.window.calibration, self.window.status.text())
        self.instrument.reflection = .31-.27j
        self.window.desc_edit.setText('calibrated load')
        self.window.trigger_measurement()
        rec = self.window.db.fetch_by_id(self.window.db.query_all()[0][0])
        values = json.loads(rec['s11_json'])
        self.assertAlmostEqual(values[0][0], .31, places=12)
        self.assertAlmostEqual(values[0][1], -.27, places=12)
        self.assertIsNotNone(rec['calibration_json'])
        self.assertNotEqual(rec['raw_s11_json'], rec['s11_json'])
        self.window.table.selectAll()
        for choice in ['Magnitude', 'Phase', 'Real', 'Imag', 'Smith']:
            self.window.plot_type_combo.setCurrentText(choice)
            self.window.plot_selected()
        csv_path=str(Path(self.temp.name)/'export.csv')
        with patch.object(app.QtWidgets.QFileDialog,'getSaveFileName',return_value=(csv_path,'')):
            self.window.export_selected_csv()
        with open(csv_path) as stream:
            rows=list(csv.reader(stream))
        self.assertEqual(len(rows),602)
        self.assertEqual(len(rows[0]),3)
        self.assertTrue(self.window.save_cal_btn.isEnabled())

    def test_incomplete_double_correction_and_changed_sweep_block_storage(self):
        self.window.start_calibration()
        self.window.trigger_measurement()
        self.assertEqual(self.window.db.query_all(),[])
        self.capture_osl()
        for change, reset in [
            (lambda: setattr(self.instrument,'method','SOLT1'), lambda:setattr(self.instrument,'method','NONE')),
            (lambda: setattr(self.instrument,'extension','1'),lambda:setattr(self.instrument,'extension','0')),
            (lambda: setattr(self.instrument,'bandwidth',10),lambda:setattr(self.instrument,'bandwidth',1000)),
            (lambda: setattr(self.instrument,'stop',2e9),lambda:setattr(self.instrument,'stop',1e9)),
        ]:
            change();self.window.timer.start(1000);self.window.trigger_measurement()
            self.assertEqual(self.window.db.query_all(),[])
            self.assertFalse(self.window.timer.isActive())
            reset()

    def test_wrong_device_json_does_not_clear_instrument(self):
        self.capture_osl()
        document=self.window.calibration.to_dict()
        document['instrument_id']='SIGLENT,SVA1032X,OTHER,1.0'
        path=Path(self.temp.name)/'wrong.json';path.write_text(json.dumps(document))
        clears=self.instrument.commands.count('CORRection:COLLect:CLEar')
        with patch.object(app.QtWidgets.QFileDialog,'getOpenFileName',return_value=(str(path),'')):
            self.window.load_calibration_dialog()
        self.assertEqual(self.instrument.commands.count('CORRection:COLLect:CLEar'),clears)
        self.assertIn('different instrument',self.window.status.text())

    def test_invalid_trace_rejected_and_standard_not_marked_captured(self):
        self.window.start_calibration()
        for bad in ['0.2,not-a-number','1,0,2','nan,0']:
            self.instrument.bad_trace=bad
            self.window.capture_standard('short')
            self.assertNotIn('short',self.window.calibration_standards)
        self.assertFalse(self.window.save_cal_btn.isEnabled())

    def test_baseline_failure_blocks_measurements(self):
        original_write=self.instrument.write
        def refuse_clear(command):
            original_write(command)
            if command=='CORRection:COLLect:CLEar':self.instrument.method='SOLT1'
        self.instrument.write=refuse_clear
        self.window.start_calibration()
        self.assertIsNone(self.window.calibration)
        self.assertTrue(self.window.software_calibration_mode)
        self.window.trigger_measurement()
        self.assertEqual(self.window.db.query_all(),[])

    def test_instrument_mode_retains_original_measurement_workflow(self):
        self.window.trigger_measurement()
        records=self.window.db.query_all()
        self.assertEqual(len(records),1)
        rec=self.window.db.fetch_by_id(records[0][0])
        self.assertIsNone(rec['calibration_json'])
        self.assertEqual(rec['s11_json'],rec['raw_s11_json'])
        self.assertEqual(self.instrument.method,'SOLT1')
        self.assertNotIn('CORRection:COLLect:CLEar',self.instrument.commands)

    def test_description_edits_preserve_measurements_and_export_quotes(self):
        self.window.trigger_measurement()
        mid=self.window.db.query_all()[0][0]
        original=self.window.db.fetch_by_id(mid)
        description='sample-2, "open"; next\nline'
        self.window.table.selectRow(0)
        self.window.table.item(0,3).setText(description)
        edited=self.window.db.fetch_by_id(mid)
        self.assertEqual(edited['note'],description)
        for key in original:
            if key!='note':self.assertEqual(edited[key],original[key])
        self.window.load_table()
        self.assertEqual(self.window.table.item(0,0).text(),str(mid))
        self.assertEqual(self.window.table.item(0,3).text(),description)
        for column in [0,1,2,4]:
            self.assertFalse(self.window.table.item(0,column).flags() & app.QtCore.Qt.ItemIsEditable)
        self.window.table.selectAll()
        path=str(Path(self.temp.name)/'quoted.csv')
        with patch.object(app.QtWidgets.QFileDialog,'getSaveFileName',return_value=(path,'')):
            self.window.export_selected_csv()
        with open(path,newline='',encoding='utf-8') as stream:rows=list(csv.reader(stream))
        self.assertEqual(len(rows),602)
        self.assertEqual(len(rows[0]),3)
        self.assertIn(description,rows[0][1])

    def test_failed_edit_restores_database_description(self):
        self.window.trigger_measurement()
        original=self.window.table.item(0,3).text()
        with patch.object(self.window.db,'update_note',side_effect=sqlite3.OperationalError('database locked')):
            self.window.table.item(0,3).setText('failed edit')
        self.assertEqual(self.window.table.item(0,3).text(),original)
        self.assertIn('Could not save',self.window.status.text())

    def test_resource_selection_refresh_disconnect_and_failure(self):
        # Refresh must not close a resource manager with an active device session.
        with patch.object(app.pyvisa,'ResourceManager') as factory:
            self.window.refresh_resources()
            factory.assert_not_called()
        self.capture_osl()
        self.window.timer.start(1000)
        self.window.on_disconnect()
        self.assertIsNone(self.window.vna)
        self.assertIsNone(self.window.calibration)
        self.assertFalse(self.window.timer.isActive())
        self.assertTrue(self.instrument.closed)
        self.assertTrue(self.window.resource_combo.isEnabled())
        self.window.resource_combo.setEditText('TCPIP0::192.0.2.10::INSTR')
        self.window.refresh_resources()
        self.assertEqual(self.window.resource_combo.currentText(),'TCPIP0::192.0.2.10::INSTR')
        self.window.on_connect()
        self.assertEqual(self.manager.opened_address,'TCPIP0::192.0.2.10::INSTR')
        self.assertFalse(self.window.resource_combo.isEnabled())
        self.window.on_disconnect()
        with patch.object(app.pyvisa,'ResourceManager',side_effect=OSError('VISA unavailable')):
            self.window.on_connect()
        self.assertIsNone(self.window.vna)
        self.assertTrue(self.window.connect_btn.isEnabled())
        self.assertIn('Connect failed',self.window.status.text())
        self.window.resource_combo.setEditText('')
        self.window.on_connect()
        self.assertIn('Select or enter',self.window.status.text())

    def test_fingerprint_in_tooltip_and_disable_only_affects_future_data(self):
        self.capture_osl()
        self.assertEqual(self.window.calibration_status.text(),'Software OSL ready — correction enabled')
        fingerprint=self.window.calibration.fingerprint
        self.assertIn(fingerprint,self.window.calibration_status.toolTip())
        self.assertNotIn(fingerprint[:12],self.window.calibration_status.text())
        self.instrument.reflection=.3+.1j
        self.window.trigger_measurement()
        mid=self.window.db.query_all()[0][0]
        previous=self.window.db.fetch_by_id(mid)
        self.window.end_cal_btn.click()
        self.assertFalse(self.window.software_calibration_mode)
        self.assertEqual(self.window.db.fetch_by_id(mid),previous)
        self.window.trigger_measurement()
        newest=self.window.db.fetch_by_id(self.window.db.query_all()[0][0])
        self.assertIsNone(newest['calibration_json'])
        self.assertEqual(newest['s11_json'],newest['raw_s11_json'])

    def test_old_database_records_preserved(self):
        old_path='old_measurements.db'
        conn=sqlite3.connect(old_path)
        conn.execute('CREATE TABLE measurements (id INTEGER PRIMARY KEY, ts TEXT, start_hz INTEGER, stop_hz INTEGER, points INTEGER, freqs_json TEXT, s11_json TEXT, note TEXT, auto INTEGER)')
        conn.execute('INSERT INTO measurements VALUES (1,?,?,?,?,?,?,?,?)',
                     ('2026-01-01',1,3,3,'[1,2,3]','[[0,0],[1,0],[-1,0]]','old record',0))
        conn.commit();conn.close()
        migrated=app.MeasurementDB(old_path)
        rec=migrated.fetch_by_id(1)
        self.assertEqual(rec['note'],'old record')
        self.assertEqual(rec['s11_json'],'[[0,0],[1,0],[-1,0]]')
        self.assertIsNone(rec['calibration_json'])
        self.assertIsNone(rec['raw_s11_json'])
        migrated.conn.close()

    def test_cancel_leaves_existing_instrument_calibration_untouched(self):
        with patch.object(app.QtWidgets.QMessageBox,'question',return_value=app.QtWidgets.QMessageBox.No):
            self.window.start_calibration()
        self.assertFalse(self.window.software_calibration_mode)
        self.assertEqual(self.instrument.method,'SOLT1')
        self.assertNotIn('CORRection:COLLect:CLEar',self.instrument.commands)


if __name__ == '__main__':unittest.main()
