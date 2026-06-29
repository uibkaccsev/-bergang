import csv
import threading
import time
import os
import sys
import subprocess
import argparse
import tkinter as tk
from tkinter import simpledialog
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import serial.tools.list_ports
import json
from pathlib import Path  # Added missing import

# ---------------------------------------------------------------------------
#  Frozen-exe helpers  (PyInstaller compatibility)
# ---------------------------------------------------------------------------
def _is_frozen():
    return getattr(sys, 'frozen', False)

def _app_dir() -> Path:
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

def _script_cmd(script_path: str, *extra_args) -> list:
    if _is_frozen():
        return [sys.executable, "--run-script", script_path] + list(extra_args)
    return [sys.executable, script_path] + list(extra_args)

import mcconf_set
from pyvesc.VESC import VESC
from pyvesc.messages.getters import GetValues
from pyvesc.messages.setters import SetCurrentBrake, SetRPM
from pyvesc.protocol.interface import decode, encode, encode_request

# Für Testzwecke: Einstellen ob oszilloskop erkannt sein muss für die verschiedenen Messungen:
IS_OPTIONAL_BEMF_OSZI = False
IS_OPTIONAL_DM_OSZI = False

# Oszi importieren
try:
    from oszi_BEMF_measure import OscilloscopeBEMF
except ImportError:
    OscilloscopeBEMF = None
    if not IS_OPTIONAL_BEMF_OSZI:
        print("[X] Fehler: oszi_BEMF_measure.py nicht gefunden und Oszilloskop ist verpflichtend fuer die Messung.")
        sys.exit(1)
    print("Warnung: oszi_BEMF_measure.py nicht gefunden oder konnte nicht importiert werden.")

try:
    from oszi_dm_measure import OscilloscopeDM
except ImportError:
    OscilloscopeDM = None
    if not IS_OPTIONAL_DM_OSZI:
        print("[X] Fehler: oszi_dm_measure.py nicht gefunden und Oszilloskop ist verpflichtend fuer die Messung.")
        sys.exit(1)
    print("Warnung: oszi_dm_measure.py nicht gefunden oder konnte nicht importiert werden.")


# Argumente parsen
parser = argparse.ArgumentParser(description='VESC Testbench')
parser.add_argument('--target_rpm', type=int, default=0, help='Ziel-Drehzahl')
parser.add_argument('--target_load_moment', type=float, default=0.0, help='Ziel-Lastmoment')
parser.add_argument('--bemf_rpm', type=int, default=0, help='BEMF-Drehzahl')
parser.add_argument('--motorconfig_drive', type=str, default="ERROR", help='Motorconfig-string des Testmotors')
parser.add_argument('--motorconfig_load', type=str, default="ERROR", help='Motorconfig-string des Lastmotors')
parser.add_argument('--motor_name', type=str, default="motor nicht spezifiziert", help='Name des motor')
parser.add_argument('--mat_nr', type=str, default="", help='Materialnummer des Motors')
parser.add_argument('--polzahl', type=int, default=0, help='Polzahl des motor')
parser.add_argument('--K_t', type=float, default=0.5, help='Drehmomentkonstante in Nm/A')

args, unknown = parser.parse_known_args()



MCCONF_DRIVE = args.motorconfig_drive
MCCONF_LOAD = args.motorconfig_load

# Nach ports mit VID:PID 0483:5740 suchen --> Vesc
found_ports = []
for port in serial.tools.list_ports.comports():
    if port.vid == 0x0483 and port.pid == 0x5740:
        found_ports.append(port.device)

if len(found_ports) < 2:
    print(f"Fehler: Mindestens 2 VESC-Geräte müssen verbunden sein (VID:PID 0483:5740). Gefunden: {found_ports}")
    sys.exit(1)

# Ports numerisch sortieren --> Netzteile werden zeitversetzt eingeschaltet --> niedriger Port ist Testmotor (Einschaltzeitpunkt der Netzteile ausschalggebend)- Motorcontroller
def get_port_number(port_name):
    # Extrahiere Ziffern aus dem Portnamen (z.B. 'COM3' -> 3)
    nums = ''.join(filter(str.isdigit, port_name))
    return int(nums) if nums else 0

found_ports.sort(key=get_port_number)

# kleinere Portnummer -> Drive
DRIVE_SERIAL_PORT = found_ports[0]
# Höhere Portnummer -> Load (verwenden Sie die höchste gefundene, wenn >2)
LOAD_SERIAL_PORT = found_ports[-1]

print(f"Erkannte Ports: Test={DRIVE_SERIAL_PORT}, Last={LOAD_SERIAL_PORT}")

polpaare = args.polzahl/2  # Polpaare
K_t = args.K_t

# hier Formel nochmal überprüfen und Rechnungen anpassen!!!!
ESTIMATED_BRAKE_CURRENT = args.target_load_moment / K_t  # I = M / K_T  with K_T = BEMF_V / omega

ITERATIONS = 100
TARGET_RPM = args.target_rpm*polpaare  # von rpm auf erpm  umrechnen
TARGET_LOAD_MOMENT = args.target_load_moment  # Newton Meter --> Nm = Bremsstrom (in A) M = I_brake * K_T (Drehmomentkonstante)


READ_TIMEOUT = 0.025
_AD = _app_dir()
(_AD / "results").mkdir(exist_ok=True)
CSV_PATH = str(_AD / "results" / "measurement.csv")
DM_AVG_CSV_PATH = str(_AD / "results" / "dm_avg.csv")  # CSV path
MESSFREQUENZ = 10  # Hz

RPM_TOLERANCE = 25 *polpaare # eRPM

OSZI_TIMEOUT = 20.0  # Timeout für die Oszilloskopmessung
oszi_done_event = threading.Event()  
BEMF_RPM = args.bemf_rpm *polpaare  # RPM für die BEMF-Messung
#BEMF_SPEED_REACHED = False
BEMF_RPM_TOLERANCE = 20 *polpaare  # RPM-Toleranz für die Berücksichtigung der erreichten BEMF-Geschwindigkeit
LOAD_RAMP_DURATION = 3
LOAD_STEADY_HOLD_DURATION = 1
RPM_RAMP_DURATION = 2
RPM_STEADY_HOLD_DURATION = 1
CURRENT_TOLERANCE = 0.01






def clamp(value, low, high):
    return max(low, min(high, value))

def read_measurement(ser, start_ts, timeout):
    # 79 ist die erwartete bytelänge für das GetValues
    EXPECTED_LENGTH = 79
    
    # ser.read() blockiert effizient, bis Daten verfügbar sind oder ein Timeout eintritt
    data = ser.read(EXPECTED_LENGTH)
    
    if len(data) == EXPECTED_LENGTH:
        try:
            msg, consumed, _ = decode(data, recv=True)
            if msg and msg.__class__.__name__ == "GetValues":
                return msg, data[:consumed], data, time.time() - start_ts
        except (ValueError, TypeError):
            # [3a] Lesefehler ausgeben - erscheint im GUI-Fehler-Popup bei unerwartetem Skript-Abbruch
            print("Fehler ist beim Auslesen der Messungen aufgetreten")
            
    return None, b"", data, time.time() - start_ts

def perform_cycle(motor, lock, command_msg):
    """Serieller Kommunikationszyklus"""
    ser = motor.serial_port
    command_packet = encode(command_msg)

    with lock:
        command_start = time.time()
        ser.write(command_packet)
        ser.flush()
        command_duration = time.time() - command_start
        command_response = b""

        request_packet = encode_request(GetValues())
        ser.write(request_packet)
        ser.flush()
        measurement_start = time.time()
        msg, consumed_bytes, buffer_bytes, measurement_duration = read_measurement(
            ser, measurement_start, READ_TIMEOUT
        )

    return {
        "measurement_start": measurement_start,
        "measurement_duration": measurement_duration,
        "message": msg,
        "buffer": buffer_bytes,
        "consumed": consumed_bytes,
        "command_duration": command_duration,
        "command_response": command_response,       
    }


def set_rpm_zero(drive, drive_lock):
    drive_result = perform_cycle(
        drive,
        drive_lock,
        SetRPM(0),
    )
    if drive_result["message"] is not None:
        if abs(drive_result["message"].rpm) < 10:
            return True
    return False

def set_load_zero(load, load_lock):
    load_result = perform_cycle(
        load,
        load_lock,
        SetCurrentBrake(0),
    )

def ramp_and_stabilize(drive, drive_lock, load, load_lock, target_rpm, target_current, target_torque, ramp_param, ramp_duration, hold_duration, tolerance, timeout=10.0, oszi_dm=None):
    print(f"Ramping {ramp_param}...")
    start_time = time.time()
    print(start_time)
    current_brake_cmd = target_current

    # erstes hochrampen zu target (drehzahl oder Bremsstrom egal)
    while True:
        elapsed = time.time() - start_time
        if elapsed >= ramp_duration:
            break
        frac = elapsed / ramp_duration
        r = frac * target_rpm if ramp_param == 'rpm' else target_rpm
        c = target_current if ramp_param == 'rpm' else frac * target_current
        perform_cycle(drive, drive_lock, SetRPM(int(r)))
        perform_cycle(load, load_lock, SetCurrentBrake(c))
        time.sleep(1.0 / MESSFREQUENZ)
        print(elapsed, "ramp and stabilize oben")

    stable_start = None
    rpm_in_tolerance = False
    curr_in_tolerance = False


    # wenn RPM nicht in Toleranz, dann mit P-regler Werte Anpassen, bis RPM in Toleranzbereich und dann Stabil hatlen für kurze Zeit
    # Special handling for Brake Current stabilization using DM Voltage
    if ramp_param == 'rpm':
        print("Stabilizing rpm...")
        rpm_cmd = target_rpm  # Start with the Zielwert
        elapsed_rpm = time.time()
        latest_meas = None        
        def meas_loop_rpm(rmp_cmd):
            latest_meas_rpm = perform_cycle(drive, drive_lock, SetRPM(int(rpm_cmd)))
            latest_meas_rpm = latest_meas_rpm["message"].rpm
            return latest_meas_rpm
        
        try:
            while True:
                if time.time() - elapsed_rpm > timeout:
                    print(f"Warning: Timeout reached while stabilizing {ramp_param} via DM!")
                    break
                rpm_measured = meas_loop_rpm(rpm_cmd)
                error_rpm = target_rpm - rpm_measured

                if abs(error_rpm) < tolerance:
                    if rpm_in_tolerance == False: 
                        stable_start = time.time()
                        rpm_in_tolerance = True
                    elif time.time() - stable_start <= hold_duration:
                        print(f"RPM stabil für {time.time() - stable_start} Sekunden.")
                        time.sleep(0.1) # kurze Pause, um Prüffrequenz einzuschränken
                    else:
                        print(f"Stabilization achieved at RPM: {rpm_measured} RPM")
                        rpm_cmd = rpm_cmd
                        break

                        
                elif abs(error_rpm) >= tolerance:
                    rpm_in_tolerance = False
                    stable_start = None
                    print("RPM nicht in Toleranz, anpassung läuft...")     
                    # 1. Reglerupdate basierend auf aktueller Messung
                    
                    if rpm_measured is not None:
                        # Simpler P-regler
                        error = target_rpm - rpm_measured
                        kp = 1.0 
                        step = error * kp
                        step = clamp(step, -100*polpaare, 100*polpaare) # Stufen begrenzen
                        
                        rpm_cmd += step

                    # 2. Signale an Vesc --> Keep Alive
                    perform_cycle(drive, drive_lock, SetRPM(int(rpm_cmd)))
                    perform_cycle(load, load_lock, SetCurrentBrake(0))
                    time.sleep(0.1)           
        finally:               
            pass

        return rpm_cmd
            

    # Bremsstrom und RPM seperat stabilisieren
    if ramp_param == 'current' and oszi_dm is not None:
        step = 0
        print("Bremsstrom anhand von Spannung DM-Messwelle stabilisieren...")
        
        current_brake_cmd = target_current 
        elapsed_curr = time.time()
        
        measure_active = True
        latest_meas = None
        meas_lock = threading.Lock()

        # --- DM-Verbindungsprüfung Konfiguration ---
        DM_HISTORY_SIZE        = 10    # Anzahl der Messwerte im gleitenden Fenster
        DM_CHANGE_THRESHOLD    = 0.1   # V  – weniger Änderung → kein Signal
        DM_TARGET_TOLERANCE    = 0.5   # V  – erlaubte Abweichung vom Sollwert
        DM_MIN_TORQUE_FOR_CHECK = 0.2  # Nm – unter diesem Sollwert wird nicht geprüft
        dm_recent = deque(maxlen=DM_HISTORY_SIZE)
        
        def meas_loop():
            nonlocal latest_meas 
            while measure_active:
                try:
                    # Messbereich des Oszilloskops einstellen --> Berechneter Bremsstrom +1A dass auch größere Werte als erwartet korrekt aufgezeichnet werden könnne.
                    oszi_dm.measure(filename=str(_app_dir() / "results" / "dm_stabilization.csv"), v_range=(target_torque + 1))
                    with meas_lock:
                        latest_meas = abs(oszi_dm.last_average)
                except Exception:
                    time.sleep(0.1)
        
        t = threading.Thread(target=meas_loop, daemon=True)
        t.start()
        
        try:
            while True:
                if time.time() - elapsed_curr > timeout:
                    print(f"Warning: Timeout reached while stabilizing {ramp_param} via DM!")
                    break
                
                # 1. Reglerupdate basierend auf aktueller Messung
                
                meas_val = None
                with meas_lock:
                    if latest_meas is not None:
                        meas_val = latest_meas
                        latest_meas = None 
                
                
                if meas_val is not None:
                    # --- DM-Verbindungsprüfung ---
                    dm_recent.append(meas_val)
                    if (len(dm_recent) == DM_HISTORY_SIZE
                            and target_torque >= DM_MIN_TORQUE_FOR_CHECK):
                        dm_range = max(dm_recent) - min(dm_recent)
                        dm_mean  = sum(dm_recent) / len(dm_recent)
                        not_changing     = dm_range < DM_CHANGE_THRESHOLD
                        off_target       = abs(dm_mean - target_torque) > DM_TARGET_TOLERANCE
                        if not_changing and off_target:
                            measure_active = False
                            print("[X] Fehlerhafte Verbindung zu Drehmomentmesswelle vermutet. Bitte Verbindung pruefen.")
                            raise RuntimeError(
                                "Fehlerhafte Verbindung zu Drehmomentmesswelle vermutet. "
                                f"Messspanne={dm_range:.3f}V, Mittelwert={dm_mean:.3f}V, Sollwert={target_torque:.3f}Nm. "
                                "Bitte Verbindung prüfen."
                            )
                    # Simpler P-regler
                    error = (target_torque - meas_val)
                    
                    if abs(error) < tolerance:
                        if curr_in_tolerance == False:
                            stable_start = time.time()
                            curr_in_tolerance = True
                        elif time.time() - stable_start < hold_duration:
                            print(f"Current stabil für {time.time() - stable_start} Sekunden.")
                            time.sleep(0.05) # kurze Pause, um Prüffrequenz einzuschränken
                        else: 
                            print(f"Stabilization achieved. Cmd: {current_brake_cmd:.2f}A")
                            break
                    elif abs(error) >= tolerance:
                        # adaptiver gain, um schnell in die Nähe zu kommen und dann feiner zu regeln
                        kp = 1.0 if abs(error) > 0.3 else 0.6
                        step = (error/K_t) * kp
                        
                        step = clamp(step, -1, 1) # Schittgröße begrenzen
                        
                        current_brake_cmd += step
                        # Bremsstrom limitieren auf 100A --> Bauteilschutz
                        current_brake_cmd = clamp(current_brake_cmd, 0.0, 100.0)

                # 2. Signale an Vesc --> Keep Alive
                perform_cycle(drive, drive_lock, SetRPM(int(target_rpm)))
                perform_cycle(load, load_lock, SetCurrentBrake(current_brake_cmd))

                
                time.sleep(0.05)

        except RuntimeError:
            # [3d] Motoren sofort anhalten wenn RuntimeError ausgelöst wird (z.B. DM-Verbindungsfehler)
            try:
                perform_cycle(drive, drive_lock, SetRPM(0))
                perform_cycle(load, load_lock, SetCurrentBrake(0))
            except Exception:
                pass
            raise
        finally:
            measure_active = False
            t.join(timeout=1.0)
            
        return current_brake_cmd

def main():
    # DM-Oszilloskopmessung inititalisieren
    oszi_dm = None
    if OscilloscopeDM:
        try:
            oszi_dm = OscilloscopeDM()
        except Exception as e:
            print(f"Warning: Could not initialize OscilloscopeDM: {e}")
            oszi_dm = None
            if not IS_OPTIONAL_DM_OSZI:
                print("[X] Error: DM Oscilloscope required but failed to initialize.")
                sys.exit(1)
    elif not IS_OPTIONAL_DM_OSZI:
         print("[X] Error: DM Oscilloscope module missing and required.")
         sys.exit(1)

    with VESC(serial_port=DRIVE_SERIAL_PORT, baudrate=115200, start_heartbeat=False) as drive, \
         VESC(serial_port=LOAD_SERIAL_PORT, baudrate=115200, start_heartbeat=False) as load, \
         open(CSV_PATH, "w", newline="") as csv_file, \
         open(DM_AVG_CSV_PATH, "w", newline="") as dm_csv_file:
        drive_lock = threading.Lock()
        load_lock = threading.Lock()

        try:
            writer = csv.writer(csv_file)
            dm_writer = csv.writer(dm_csv_file) 

            writer.writerow([
                "iteration",
                "timestamp_drive_s",
                "duration_drive_ms",
                "rpm_drive",
                "iq_drive_A",
                "id_drive_A",
                "i_in_drive_A",
                "i_motor_drive_A",
                "timestamp_load_s",
                "duration_load_ms",
                "rpm_load",
                "iq_load_A",
                "id_load_A",
                "i_in_load_A",
                "i_motor_load_A",
                "brake_command_A",
                "rpm_command",
                "dm_voltage_V",
                "v_in",
                "temp_fet"

            ])
            
            dm_writer.writerow(["iteration", "timestamp_s", "Ch4"])
            
            csv_file.flush()
            dm_csv_file.flush()

            rpm_command = TARGET_RPM

            mcconf_set.load_configuration(drive, MCCONF_DRIVE, "Drive", drive_lock)
            mcconf_set.load_configuration(load, MCCONF_LOAD, "Load", load_lock)

            session_start = time.time()

            # 1. RPM rampe mit Bremsstrom = 0
            rpm_stabilized = ramp_and_stabilize(drive, drive_lock, load, load_lock, 
                               TARGET_RPM,0, 0, 'rpm', 
                               RPM_RAMP_DURATION, RPM_STEADY_HOLD_DURATION, RPM_TOLERANCE, timeout = 10)

            # 2. Bremsstrom regeln mit DM-Messung bis Lastmoment stabilisiert ist
            brake_command = ramp_and_stabilize(drive, drive_lock, load, load_lock, 
                               rpm_stabilized, ESTIMATED_BRAKE_CURRENT, TARGET_LOAD_MOMENT, 'current', 
                               LOAD_RAMP_DURATION, LOAD_STEADY_HOLD_DURATION, CURRENT_TOLERANCE, 
                               timeout=20.0, oszi_dm=oszi_dm)

            print(f"Stabilized Brake Command: {brake_command:.2f} A")

            print("Starting measurement loop.")
            time.sleep(0.1) # Zeit geben dass sich die Drehzahlen stabilisiern nach Drehmomenteinstellung.
            
            executor = ThreadPoolExecutor(max_workers=2)
            dm_thread = None

            for idx in range(ITERATIONS):
                loop_start_time = time.time()
                
                # Parallel execution
                future_drive = executor.submit(perform_cycle, drive, drive_lock, SetRPM(int(rpm_command)))
                future_load = executor.submit(perform_cycle, load, load_lock, SetCurrentBrake(brake_command))
                
                drive_result = future_drive.result()
                load_result = future_load.result()

                drive_msg = drive_result["message"]
                load_msg = load_result["message"]

                drive_timestamp = drive_result["measurement_start"] - session_start
                load_timestamp = load_result["measurement_start"] - session_start

                drive_duration_ms = drive_result["measurement_duration"] * 1000
                load_duration_ms = load_result["measurement_duration"] * 1000
                
                writer.writerow([
                    idx,
                    drive_timestamp,
                    drive_duration_ms,
                    getattr(drive_msg, "rpm", None),
                    getattr(drive_msg, "avg_iq", None),
                    getattr(drive_msg, "avg_id", None),
                    getattr(drive_msg, "avg_input_current", None),
                    getattr(drive_msg, "avg_motor_current", None),
                    load_timestamp,
                    load_duration_ms,
                    getattr(load_msg, "rpm", None),
                    getattr(load_msg, "avg_iq", None),
                    getattr(load_msg, "avg_id", None),
                    getattr(load_msg, "avg_input_current", None),
                    getattr(load_msg, "avg_motor_current", None),
                    brake_command,
                    rpm_command,
                    oszi_dm.last_average if oszi_dm and oszi_dm.last_average is not None else "",
                    getattr(load_msg, "v_in", None),
                    getattr(load_msg, "temp_fet", None),
                ])
                
                elapsed = time.time() - loop_start_time
                sleep_time = (1.0 / MESSFREQUENZ) - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
                
                # --- Jeden 10. Durchlauf Drehmoment messen ---
                if idx % 10 == 0 and oszi_dm:
                    # Prüfen ob vorherige DM-Messung abgeschlossen ist
                    if dm_thread and dm_thread.is_alive():
                        print(f"Cycle {idx}: DM Measurement not yet finished.")
                    else:
                        if oszi_dm.last_average is not None:
                            dm_writer.writerow([idx, time.time() - session_start, oszi_dm.last_average])
                            dm_csv_file.flush()
                            pass

                        dm_thread = threading.Thread(
                            target=oszi_dm.measure,
                            kwargs={
                                "v_range": (TARGET_LOAD_MOMENT + 1)
                            },
                            daemon=True
                        )
                        dm_thread.start()
            
            executor.shutdown()
            
            if dm_thread and dm_thread.is_alive():
                dm_thread.join(timeout=1.0)

            if oszi_dm and oszi_dm.last_average is not None:
                dm_writer.writerow([ITERATIONS, time.time() - session_start, oszi_dm.last_average])
                dm_csv_file.flush()

            if oszi_dm:
                oszi_dm.close()
                oszi_dm = None

            # drehzahl muss = 0 bevor mit bemf messung begonnen wird
            time.sleep(0.3)
            rpm_zero = False
            # [3b] Timeout damit die Schleife nicht endlos läuft wenn der Motor sich nicht anhalten lässt
            _rpm_stop_deadline = time.time() + 20.0
            while not rpm_zero:
                if time.time() > _rpm_stop_deadline:
                    raise RuntimeError("Motor konnte nicht angehalten werden (Timeout)")
                rpm_zero = set_rpm_zero(drive, drive_lock)
                set_load_zero(load, load_lock)

            if rpm_zero:
                time.sleep(1.0)  # sicherheitshalber noch 1s warten
                print("Starting BEMF measurement phase")
                
                print("Ramping Load Motor to BEMF RPM...")
                bemf_ramp_start = time.time()
                while True:
                    elapsed = time.time() - bemf_ramp_start
                    if elapsed >= RPM_RAMP_DURATION:
                        break
                    frac = elapsed / RPM_RAMP_DURATION
                    r = frac * BEMF_RPM
                    perform_cycle(load, load_lock, SetRPM(int(r)))
                    
                print("Stabilizing Load Motor at BEMF RPM...")
                bemf_stable_start = None
                bemf_stabilize_start = time.time()
                while True:
                    if time.time() - bemf_stabilize_start > 20.0:
                        # [3c] Fehlermeldung wenn BEMF-Drehzahl nicht erreicht - erscheint im GUI-Fehler-Popup
                        print("[X] BEMF drehzahl konnte nicht erreicht werden.")
                        raise RuntimeError("BEMF drehzahl konnte nicht erreicht werden.")
                        
                    res = perform_cycle(load, load_lock, SetRPM(int(BEMF_RPM)))
                    val = res["message"].rpm if res["message"] else None
                    
                    if val is not None and abs(val - BEMF_RPM) < BEMF_RPM_TOLERANCE:
                        if bemf_stable_start is None: bemf_stable_start = time.time()
                        elif time.time() - bemf_stable_start >= RPM_STEADY_HOLD_DURATION: break
                    else:
                        bemf_stable_start = None

                # Oszilloskop für BEMF-Messung starten
                oszi = None
                if OscilloscopeBEMF:
                    for i in range(3):
                        try:
                            oszi = OscilloscopeBEMF(e_rpm=BEMF_RPM)
                            break
                        except Exception as e:
                            print(f"Warning: Could not initialize BEMF Oscilloscope (Attempt {i+1}/3): {e}")
                            if i < 2:
                                time.sleep(1.0)
                            else:
                                oszi = None
                                if not IS_OPTIONAL_BEMF_OSZI:
                                    print("[X] Error: BEMF Oscilloscope required but failed to initialize.")
                                    sys.exit(1)
                elif not IS_OPTIONAL_BEMF_OSZI:
                     print("[X] Error: BEMF Oscilloscope module missing and required.")
                     sys.exit(1)

                # [2.5] Event zurücksetzen damit die BEMF-Messung sauber starten kann (verhindert sofortiges Beenden)
                oszi_done_event.clear()
                T_start_oszi = time.time()
                oszi_messung_started = False
            
                def run_oszi_measurement():
                    if oszi:
                        oszi.measure(filename=str(_AD / "results" / "OscilloscopeStream.csv"))
                    else:
                        print("Oscilloscope not initialized, skipping measurement.")
                        time.sleep(1)  
                    oszi_done_event.set() 

                while not oszi_done_event.is_set() and (time.time() - T_start_oszi) < OSZI_TIMEOUT:
                    if not oszi_messung_started:
                        t = threading.Thread(target=run_oszi_measurement, daemon=True)
                        t.start()
                        oszi_messung_started = True

                    bemf_speed_result = perform_cycle(
                            load,
                            load_lock,
                            SetRPM(int(BEMF_RPM)),
                        )
                    time.sleep(0.1)

                # Motoren anhalten
                set_rpm_zero(drive, drive_lock)
                set_load_zero(load, load_lock)
                
                if oszi:
                    oszi.close()

                # Get user comment via popup
                print("Prompting user for comment...")
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                user_comment = simpledialog.askstring("Messprotokoll", "Messung beendet.\nGeben Sie einen Kommentar für den Bericht ein (Leer lassen oder Abbrechen zum Überspringen):", parent=root)
                if user_comment is None:
                    user_comment = ""
                root.destroy()

                # Messberichterstellung starten
                print("Generating report...")
                report_path = str(_app_dir() / "messprotokoll_V3.py")
                # BEMF_RPM is in eRPM internally -> convert back to user-facing RPM
                bemf_rpm_user = int(args.bemf_rpm / max(1, polpaare))
                report_cmd = _script_cmd(
                    report_path,
                    "--motor_name", args.motor_name,
                    "--mat_nr", args.mat_nr,
                    "--bemf_rpm", str(bemf_rpm_user),
                    "--user_comment", user_comment,
                )
                subprocess.run(report_cmd)
                print("Report generation finished.")
                
                # --- Ergebnisse direkt anzeigen ---
                try:
                    analysis_file = _app_dir() / "results" / "analysis_result.json"
                    db_file = _app_dir() / "config" / "Motorauswahl.json"
                    
                    if analysis_file.exists() and db_file.exists():
                        with open(analysis_file, "r") as f:
                            measured_stats = json.load(f)
                        with open(db_file, "r") as f:
                            db_data = json.load(f)
                        
                        # aktuelle Werte des Motors aus der Datenbank holen
                        motor_entry = next((m for m in db_data if m["Name"] == args.motor_name), None)
                        
                        
                        if motor_entry:
                            keys_to_check = [
                                "BEMF_1_V", "BEMF_2_V", "BEMF_3_V",
                                "BEMF_1_P", "BEMF_2_P", "BEMF_3_P",
                                "current_under_load"
                            ]
                            
                            # Prüfen, ob bereits werte für diesen Motor in der Datenbank existieren
                            has_baseline = all(motor_entry.get(k) is not None for k in keys_to_check)
                            
                            if has_baseline:
                                # vergleichen
                                fail = False
                                
                                print("--- Ergebniss Details (Popup) ---")
                                
                                # 1. Phasentiming wird nicht gespeichert --> unnötig. Muss nur konstant sein
                                print("Phase Timing:")
                                print(f"  P1->P2: {measured_stats.get('delta_p1_p2', 0.0)*1000:.2f} ms")
                                print(f"  P2->P3: {measured_stats.get('delta_p2_p3', 0.0)*1000:.2f} ms")
                                print(f"  P3->P1: {measured_stats.get('delta_p3_p1', 0.0)*1000:.2f} ms")
                                print(f"  V1->V2: {measured_stats.get('delta_v1_v2', 0.0)*1000:.2f} ms")
                                print(f"  V2->V3: {measured_stats.get('delta_v2_v3', 0.0)*1000:.2f} ms")
                                print(f"  V3->V1: {measured_stats.get('delta_v3_v1', 0.0)*1000:.2f} ms")
                                
                                # 2. Vergleichstabelle mit bestehenden werten
                                print("\nComparison:")
                                for k in keys_to_check:
                                    base = motor_entry[k]
                                    meas = measured_stats.get(k, 0.0)
                                    
                                    if abs(base) < 0.001:
                                        diff = abs(meas - base)
                                        pct = 0.0 
                                    else:
                                        pct = abs((meas - base) / base) * 100
                                    
                                    # Use simpler names
                                    name_map = {
                                        "Strom unter Last": "Durchschnittlicher Strom",
                                        "BEMF_1_P": "Ph1 Hoch", "BEMF_1_V": "Ph1 Tief",
                                        "BEMF_2_P": "Ph2 Hoch", "BEMF_2_V": "Ph2 Tief",
                                        "BEMF_3_P": "Ph3 Hoch", "BEMF_3_V": "Ph3 Tief",
                                    }
                                    display_name = name_map.get(k, k)
                                    
                                    status = "OK" if pct < 10.0 else "FAIL"
                                    if 'current' in k:
                                         print(f"  Current: Base={base:.2f}A, Meas={meas:.2f}A, Diff={pct:.1f}% -> {status}")
                                    elif 'BEMF' in k:
                                         parts = k.split('_')
                                         name_clean = f"Ph{parts[1]} {'Hoch' if parts[2]=='P' else 'Tief'}"
                                         print(f"  {name_clean}: Base={base:.2f}V, Meas={meas:.2f}V, Diff={pct:.1f}% -> {status}")
                                    # TOLERANZ IN VARIABLE
                                    if pct >= 10.0:
                                        fail = True
                                
                                if fail:
                                    print("Ergebniss: FEHLGESCHLAGEN")
                                else:
                                    print("Ergebniss: BESTANDEN")
                            else:
                                # Baseline aktualisieren, da keine Werte vorhanden sind
                                print("--- Baseline aktualisieren ---")
                                for k in keys_to_check:
                                    val = measured_stats.get(k, 0.0)
                                    motor_entry[k] = val
                                    print(f"  Set {k} = {val:.2f}")
                                
                                # In Datenbank speichern
                                with open(db_file, "w") as f:
                                    json.dump(db_data, f, indent=4)
                                
                                print("Ergebniss: BASISLINIE GESPEICHERT")
                        else:
                            print(f"Fehler: Motor '{args.motor_name}' nicht in der Datenbank für Update gefunden.")
                    else:
                        print("Fehler: analysis_result.json oder Motorauswahl.json fehlt.")

                except Exception as e:
                    print(f"Fehler während der Ergebnisanalyse: {e}")

        except KeyboardInterrupt:
            print("\n🛑 Programm vom Benutzer unterbrochen!")
            
        finally:
            print("Beende messablauf.py...")
            
            # Redundant safety stop
            try:
                set_rpm_zero(drive, drive_lock)
                set_load_zero(load, load_lock)
            except Exception:
                pass

if __name__ == "__main__":
    main()
