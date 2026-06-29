import csv
import threading
import time
import os
import sys
import subprocess
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import serial.tools.list_ports
import json
import statistics # Added import
from pathlib import Path  # Added missing import

import mcconf_set
from pyvesc.VESC import VESC
from pyvesc.messages.getters import GetValues
from pyvesc.messages.setters import SetCurrentBrake, SetRPM
from pyvesc.protocol.interface import decode, encode, encode_request

# Für Testzwecke: Einstellen ob oszilloskop erkannt sein muss für die verschiedenen Messungen:
IS_OPTIONAL_BEMF_OSZI = False
IS_OPTIONAL_DM_OSZI = False

# Import Oscilloscope class
try:
    from oszi_BEMF_measure import OscilloscopeBEMF
except ImportError:
    OscilloscopeBEMF = None
    if not IS_OPTIONAL_BEMF_OSZI:
        print("[X] Error: oszi_BEMF_measure.py not found and IS_OPTIONAL_BEMF_OSZI is False.")
        sys.exit(1)
    print("Warning: oszi_BEMF_measure.py not found or failed to import.")


# Parse command line arguments
parser = argparse.ArgumentParser(description='VESC Testbench')
parser.add_argument('--target_rpm', type=int, default=0, help='Target RPM')
parser.add_argument('--target_load_moment', type=float, default=0.0, help='Target Load Moment')
parser.add_argument('--bemf_rpm', type=int, default=0, help='BEMF RPM')
parser.add_argument('--motorconfig_drive', type=str, default="ERROR", help='Motorconfig-string from Vesc-tool for drive motor')
parser.add_argument('--motorconfig_load', type=str, default="ERROR", help='Motorconfig-string from Vesc-tool for load motor')
parser.add_argument('--motor_name', type=str, default="motor nicht spezifiziert", help='Name of the motor')
parser.add_argument('--polzahl', type=int, default=2, help='Polzahl of the motor')
#parser.add_argument('--K_V', type=float, default=0.0, help='Voltage constant in RPM/V') # noch in Einmessen hinzufügen -> hieraus wird die KV und drehmomentkonstante grob abgeschätzt
parser.add_argument('--K_t', type=float, default=0.5, help='Torque constant in Nm/A') # noch in Einmessen hinzufügen -> hieraus wird die KV und drehmomentkonstante grob abgeschätzt

args, unknown = parser.parse_known_args()

def process_scope_data(csv_path, json_path):
    """
    Reads the Oscilloscope stream CSV and calculates peaks and valleys for each channel.
    Result is saved to json_path.
    """
    if not os.path.exists(csv_path):
        print(f"Warning: CSV file {csv_path} not found.")
        return

    data = {1: [], 2: [], 3: []}
    
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f, delimiter=';')
            # Skip header
            next(reader, None)
            
            for row in reader:
                # Expected: Sample; Ch1; Ch2; Ch3; (maybe empty)
                # Indices: 0=Sample, 1=Ch1, 2=Ch2, 3=Ch3
                if len(row) >= 4:
                    try:
                        if row[1].strip(): data[1].append(float(row[1]))
                        if row[2].strip(): data[2].append(float(row[2]))
                        if row[3].strip(): data[3].append(float(row[3]))
                    except ValueError:
                        continue
                        
        results = {}
        for ch in [1, 2, 3]:
            vals = data[ch]
            if not vals:
                results[f"BEMF_{ch}_P"] = 0.0
                results[f"BEMF_{ch}_V"] = 0.0
                continue
            
            vals.sort()
            n = len(vals)
            # Use top/bottom 5% average for robustness against outliers/noise
            n_avg = max(1, int(n * 0.05))
            
            valley = statistics.mean(vals[:n_avg])
            peak = statistics.mean(vals[-n_avg:])
            
            results[f"BEMF_{ch}_P"] = peak
            results[f"BEMF_{ch}_V"] = valley
            
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=4)
            
        print(f"BEMF results processed and saved to {json_path}")
        
    except Exception as e:
        print(f"Error processing BEMF CSV: {e}")


MCCONF_DRIVE = args.motorconfig_drive
MCCONF_LOAD = args.motorconfig_load

# Scan for ports with VID:PID 0483:5740
found_ports = []
for port in serial.tools.list_ports.comports():
    if port.vid == 0x0483 and port.pid == 0x5740:
        found_ports.append(port.device)

if len(found_ports) < 2:
    print(f"Error: Need at least 2 VESC devices connected (VID:PID 0483:5740). Found: {found_ports}")
    sys.exit(1)

# Sort ports numerically
def get_port_number(port_name):
    # Extract digits from port name (e.g., 'COM3' -> 3)
    nums = ''.join(filter(str.isdigit, port_name))
    return int(nums) if nums else 0

found_ports.sort(key=get_port_number)

# Lower port number -> Drive
DRIVE_SERIAL_PORT = found_ports[0]
# Higher port number -> Load (using the highest one found if >2)
LOAD_SERIAL_PORT = found_ports[-1]

print(f"Detected Ports: Drive={DRIVE_SERIAL_PORT}, Load={LOAD_SERIAL_PORT}")

polpaare = args.polzahl/2  # Pole pairs

READ_TIMEOUT = 0.025
MESSFREQUENZ = 10  # Hz

#BRAKE_WINDOW = 5
#BRAKE_CURRENT_LIMIT = 3.0  # Amps
#RPM_WINDOW = 5

RPM_TOLERANCE = 25 *polpaare # eRPM

OSZI_TIMEOUT = 10.0  # seconds so the while loop doesnt run forever
oszi_done_event = threading.Event()  # Thread-safe flag replacing global OSZI_MESS_FERTIG
BEMF_RPM = args.bemf_rpm *polpaare  # RPM for back-emf measurement
#BEMF_SPEED_REACHED = False
BEMF_RPM_TOLERANCE = 20 *polpaare  # RPM tolerance for considering BEMF speed reached
LOAD_RAMP_DURATION = 3
LOAD_STEADY_HOLD_DURATION = 1
RPM_RAMP_DURATION = 2
RPM_STEADY_HOLD_DURATION = 1
CURRENT_TOLERANCE = 0.01






def clamp(value, low, high):
    return max(low, min(high, value))

def read_measurement(ser, start_ts, timeout):
    # 79 Is the bytelength of GetValues
    EXPECTED_LENGTH = 79
    
    # ser.read() blocks efficiently until data is available or timeout
    data = ser.read(EXPECTED_LENGTH)
    
    if len(data) == EXPECTED_LENGTH:
        try:
            msg, consumed, _ = decode(data, recv=True)
            if msg and msg.__class__.__name__ == "GetValues":
                return msg, data[:consumed], data, time.time() - start_ts
        except (ValueError, TypeError):
            pass
            
    return None, b"", data, time.time() - start_ts

def perform_cycle(motor, lock, command_msg):
    """Flush -> command -> flush -> measure for a single VESC."""
    ser = motor.serial_port
    command_packet = encode(command_msg)

    with lock:
        # flush_serial(ser)

        command_start = time.time()
        ser.write(command_packet)
        ser.flush()
        # command_response = drain_response(ser)
        command_duration = time.time() - command_start
        command_response = b""

        # flush_serial(ser)

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

def main():
    _app_dir = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
    results_dir = _app_dir / "results"
    config_dir  = _app_dir / "config"
    results_dir.mkdir(exist_ok=True)
    config_dir.mkdir(exist_ok=True)

    # Delete stale result files so a failed/partial run can never be mistaken for fresh data
    for _stale in (results_dir / "bemf_result.json", results_dir / "OscilloscopeStream.csv"):
        try:
            _stale.unlink(missing_ok=True)
            print(f"Cleared stale file: {_stale.name}")
        except Exception as _e:
            print(f"Warning: could not delete {_stale}: {_e}")

    # Initialize BEMF Oscilloscope
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

    with VESC(serial_port=DRIVE_SERIAL_PORT, baudrate=115200, start_heartbeat=False) as drive, \
         VESC(serial_port=LOAD_SERIAL_PORT, baudrate=115200, start_heartbeat=False) as load:
        drive_lock = threading.Lock()
        load_lock  = threading.Lock()

        try:
            # Load motor configurations (critical – without this VESCs are unconfigured)
            mcconf_set.load_configuration(drive, MCCONF_DRIVE, "Drive", drive_lock)
            mcconf_set.load_configuration(load,  MCCONF_LOAD,  "Load",  load_lock)

            # Ensure RPM = 0 before starting, with timeout
            time.sleep(0.3)
            rpm_zero = False
            _stop_deadline = time.time() + 20.0
            while not rpm_zero:
                if time.time() > _stop_deadline:
                    raise RuntimeError("Motor konnte nicht angehalten werden (Timeout)")
                rpm_zero = set_rpm_zero(drive, drive_lock)
                set_load_zero(load, load_lock)

            time.sleep(1.0)
            print("Starting BEMF measurement phase")

            # Ramp load motor to BEMF RPM
            print("Ramping Load Motor to BEMF RPM...")
            bemf_ramp_start = time.time()
            while True:
                elapsed = time.time() - bemf_ramp_start
                if elapsed >= RPM_RAMP_DURATION:
                    break
                frac = elapsed / RPM_RAMP_DURATION
                perform_cycle(load, load_lock, SetRPM(int(frac * BEMF_RPM)))

            # Stabilize at BEMF RPM
            print("Stabilizing Load Motor at BEMF RPM...")
            bemf_stable_start   = None
            bemf_stabilize_start = time.time()
            while True:
                if time.time() - bemf_stabilize_start > 20.0:
                    print("[X] BEMF drehzahl konnte nicht erreicht werden.")
                    raise RuntimeError("BEMF drehzahl konnte nicht erreicht werden.")
                res = perform_cycle(load, load_lock, SetRPM(int(BEMF_RPM)))
                val = res["message"].rpm if res["message"] else None
                if val is not None and abs(val - BEMF_RPM) < BEMF_RPM_TOLERANCE:
                    if bemf_stable_start is None:
                        bemf_stable_start = time.time()
                    elif time.time() - bemf_stable_start >= RPM_STEADY_HOLD_DURATION:
                        break
                else:
                    bemf_stable_start = None

            # Reset event before starting (prevents loop from exiting immediately if event was set before)
            oszi_done_event.clear()
            T_start_oszi       = time.time()
            oszi_messung_started = False

            def run_oszi_measurement():
                if oszi:
                    oszi.measure(filename=str(results_dir / "OscilloscopeStream.csv"))
                else:
                    print("Oscilloscope not initialized, skipping measurement.")
                    time.sleep(1)
                oszi_done_event.set()

            while not oszi_done_event.is_set() and (time.time() - T_start_oszi) < OSZI_TIMEOUT:
                if not oszi_messung_started:
                    t = threading.Thread(target=run_oszi_measurement, daemon=True)
                    t.start()
                    oszi_messung_started = True
                perform_cycle(load, load_lock, SetRPM(int(BEMF_RPM)))
                time.sleep(0.1)

            # Stop motors
            set_rpm_zero(drive, drive_lock)
            set_load_zero(load, load_lock)

            if oszi:
                oszi.close()

            # Process oscilloscope data into bemf_result.json
            print("Processing BEMF data...")
            process_scope_data(
                str(results_dir / "OscilloscopeStream.csv"),
                str(results_dir / "bemf_result.json"),
            )

            # Result analysis & baseline check
            try:
                analysis_file = results_dir / "bemf_result.json"
                db_file       = config_dir  / "Motorauswahl.json"

                if analysis_file.exists() and db_file.exists():
                    with open(analysis_file, "r") as f:
                        measured_stats = json.load(f)
                    with open(db_file, "r") as f:
                        db_data = json.load(f)

                    motor_entry = next((m for m in db_data if m["Name"] == args.motor_name), None)

                    if motor_entry:
                        keys_to_check = [
                            "BEMF_1_V", "BEMF_2_V", "BEMF_3_V",
                            "BEMF_1_P", "BEMF_2_P", "BEMF_3_P",
                        ]

                        has_baseline = all(motor_entry.get(k) is not None for k in keys_to_check)

                        if has_baseline:
                            fail = False
                            print("\n--- Result Details ---")
                            print("\nComparison:")
                            for k in keys_to_check:
                                base = motor_entry[k]
                                meas = measured_stats.get(k, 0.0)
                                pct  = abs((meas - base) / base) * 100 if abs(base) >= 0.001 else 0.0
                                parts = k.split('_')
                                name_clean = f"Ph{parts[1]} {'Peak' if parts[2]=='P' else 'Valley'}"
                                status = "OK" if pct < 10.0 else "FAIL"
                                print(f"  {name_clean}: Base={base:.2f}V, Meas={meas:.2f}V, Diff={pct:.1f}% -> {status}")
                                if pct >= 10.0:
                                    fail = True
                            print("RESULT: FAIL" if fail else "RESULT: PASS")
                        else:
                            print("\n--- Updating Baseline ---")
                            for k in keys_to_check:
                                val = measured_stats.get(k, 0.0)
                                motor_entry[k] = val
                                print(f"  Set {k} = {val:.2f}")
                            with open(db_file, "w") as f:
                                json.dump(db_data, f, indent=4)
                            print("RESULT: BASELINE_SET")
                    else:
                        print(f"Error: Motor '{args.motor_name}' not found in database.")
                else:
                    print("Error: bemf_result.json or Motorauswahl.json missing.")

            except Exception as e:
                print(f"Error during result analysis: {e}")

        except KeyboardInterrupt:
            print("\nProgram Interrupted by User!")

        finally:
            print("Shutting down BEMF-aktualisierung...")
            try:
                set_rpm_zero(drive, drive_lock)
                set_load_zero(load, load_lock)
            except Exception:
                pass

if __name__ == "__main__":
    main()

