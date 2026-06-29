import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import json
import subprocess
import sys
import os
import time
import threading
import libtiepie
from pathlib import Path

# ---------------------------------------------------------------------------
#  Frozen-exe helpers  (PyInstaller compatibility)
# ---------------------------------------------------------------------------
def _is_frozen():
    """Return True when running inside a PyInstaller bundle."""
    return getattr(sys, 'frozen', False)

def _app_dir() -> Path:
    """Directory that contains the .exe (or the script when not frozen)."""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

def _script_cmd(script_path: str, *extra_args) -> list:
    """Build a subprocess command list that works both frozen and unfrozen.

    When frozen the .exe acts as its own interpreter via the
    ``--run-script`` dispatcher (see exe_entry.py).
    """
    if _is_frozen():
        return [sys.executable, "--run-script", script_path] + list(extra_args)
    return [sys.executable, script_path] + list(extra_args)

# Import PSU control
try:
    from lan_PSU import IT6005C
    PSU_AVAILABLE = True
except ImportError:
    PSU_AVAILABLE = False
    print("Warning: lan_PSU.py not found. PSU control disabled.")

# Pfad der Motordaten-files  (resolved relative to app directory)
_BASE = _app_dir()
CONFIG_DIR  = _BASE / "config"
RESULTS_DIR = _BASE / "results"
REPORTS_DIR = _BASE.parent / "Messberichte"   # one level up: Motorprüfstand/Messberichte
CONFIG_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)
JSON_PATH = str(CONFIG_DIR / "Motorauswahl.json")
LASTMOTOREN_PATH = str(CONFIG_DIR / "Lastmotor.json")
# Andere verwendete Scripts
TEST_SCRIPT_PATH = str(_BASE / "messablauf.py")
DETECT_SCRIPT_PATH = str(_BASE / "Motordaten_detektion.py")
BEMF_SCRIPT_PATH = str(_BASE / "BEMF-aktualisierung.py")

# IP-Adressen der Netzteile für Lan-Steuerung
PSU1_IP = "192.168.200.100"  # Drive Motor PSU
PSU2_IP = "192.168.200.101"  # Load Motor PSU

# PSU-selbsttest Parameter
PSU_VOLTAGE_TOLERANCE = 0.05  # V - Toleranz für Spannungsüberprüfung
PSU_VERIFICATION_TIMEOUT = 5.0  # Stabilisationszeit PSU

class TestBenchLauncher:
    def __init__(self, root):
        self.root = root
        self.root.title("VESC Testbench Launcher")
        self.root.geometry("500x600")

        self.motor_data = self.load_json_data(JSON_PATH)
        self.lastmotoren_data = self.load_json_data(LASTMOTOREN_PATH)
        
        self.motor_names = [m["Name"] for m in self.motor_data] if self.motor_data else []

        # PSU instances
        self.psu1 = None
        
        self.psu2 = None
        
        # Status label reference
        self.status_label = None
        
        # Test running flag
        self.test_running = False

        self.create_widgets()

    def load_json_data(self, filepath):
        if not os.path.exists(filepath):
            if filepath == JSON_PATH:
                messagebox.showerror("Error", f"File not found: {filepath}")
            else:
                print(f"Warning: {filepath} not found. Load motor selection may fail.")
            return []
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            messagebox.showerror("Error", f"Failed to parse JSON {filepath}: {e}")
            return []
        except Exception as e:
            messagebox.showerror("Error", f"An error occurred loading {filepath}: {e}")
            return []

    def create_widgets(self):
        # Dropdown Testmotor
        label_drive = ttk.Label(self.root, text="Testmotor auswählen:")
        label_drive.pack(pady=5)
        self.combo_drive = ttk.Combobox(self.root, values=self.motor_names, state="readonly", width=30)
        self.combo_drive.pack(pady=5)
        if self.motor_names:
            self.combo_drive.current(0)

        # Status Label
        self.status_label = ttk.Label(self.root, text="Status: Bereit", foreground="black")
        self.status_label.pack(pady=10)
        
        # PSU Status Label
        self.psu_status_label = ttk.Label(self.root, text="PSU: Nicht verbunden", foreground="gray")
        self.psu_status_label.pack(pady=5)

        # Test Buttons Frame (Lastmessung + BEMF-Messung side by side)
        test_btn_frame = ttk.Frame(self.root)
        test_btn_frame.pack(pady=20)

        self.start_btn = ttk.Button(test_btn_frame, text="Lastmessung", command=self.start_test)
        self.start_btn.pack(side=tk.LEFT, padx=10)

        self.bemf_btn = ttk.Button(test_btn_frame, text="BEMF-Messung", command=self.start_bemf_messung)
        self.bemf_btn.pack(side=tk.LEFT, padx=10)

        # Add Motor Button
        self.add_motor_btn = ttk.Button(self.root, text="Neuen Motor hinzufügen", command=self.open_add_motor_window)
        self.add_motor_btn.pack(pady=5)

        # Update Motor Button
        self.update_motor_btn = ttk.Button(self.root, text="Motordaten aktualisieren", command=self.open_update_motor_window)
        self.update_motor_btn.pack(pady=5)

        # Delete Motor Button
        self.del_motor_btn = ttk.Button(self.root, text="Motor löschen", command=self.open_delete_motor_window)
        self.del_motor_btn.pack(pady=5)

    def check_password(self):
        """Passwort überprüfen."""
        pwd = simpledialog.askstring("Passwort", "Bitte Passwort eingeben:", show='*', parent=self.root)
        # HIER WIRD DAS PASSWORT GESETZT - AKTUELL "0000"
        # Änderungsmöglichkeit --> Passwort verändern
        if pwd == "0000":
            return True
        elif pwd is None: # Abgebrochen
            return False
        else:
            messagebox.showerror("Fehler", "Falsches Passwort!")
            return False

    def open_add_motor_window(self):
        """Open a window to add a new motor configuration."""
        if not self.check_password():
            return

        win = tk.Toplevel(self.root)
        win.title("Neuen Motor hinzufügen")
        
        # Änderungsmöglichkeit -> Lastmotor hinzufügen
        # Lastmotor Dropdown: Um Namen zu änern oder einen weiteren Lastmotor hinzuzufügen, muss:
        # 1. der Name hier
        # 2. der neue Lastmotor muss als neuer Motor eingelesen werden --> Motorname muss dem hier hinzugefügtem Namen entsprechen
        # 3. Dann muss der Erstellte Teil aus der Motorauswahl.JSON entfernt werden und in die Lastmotor.JSON hinzugefügt werden
        # Motorauswahl.JSON und Lastmotor.JSON liegen beide: "D"okumente --> Motorprüfstand --> Programm --> config" ab. 

        ttk.Label(win, text="Lastmotorgröße").grid(row=0, column=0, padx=10, pady=5, sticky="e")
        motortype_values = [
            "Gross",
            "Mittel",
            "Klein"
        ] # hier hinzufügen: "," -> nächste Zeile -> "name" 
        motortype_combo = ttk.Combobox(win, values=motortype_values, state="readonly", width=25)
        motortype_combo.grid(row=0, column=1, padx=10, pady=5)
        motortype_combo.current(0)
        
        # Define fields: (Label Text, JSON Key, Type Conversion)
        # Modified list allows us to treat PSU Voltage separately if needed, 
        # but for Entry-based fields we keep this structure.
        # We will handle PSU Voltage differently below.
        fields = [
            ("Polzahl", "Polzahl", int),
            ("Name", "Name", str),
            ("Mat.Nr.", "Mat.Nr.", str),
            ("Test-Drehzahl", "Drive_Drehzahl", int),
            ("Lastmoment (Nm)", "Load_Drehmoment", float), 
            ("BEMF-Drehzahl", "BEMF_Drehzahl", int),
        ]
        
        entries = {}
        # shift rows by +1 because motortype combobox occupies row 0
        current_row = 1
        for idx, (lbl, key, caster) in enumerate(fields):
            ttk.Label(win, text=lbl).grid(row=current_row, column=0, padx=10, pady=5, sticky="e")
            ent = ttk.Entry(win, width=25)
            ent.grid(row=current_row, column=1, padx=5, pady=5)
            entries[key] = (ent, caster)
            current_row += 1
        
        # Änderungsmöglichkeit -> Netzteilspannung Dropdown verändern
        ttk.Label(win, text="Spannung Testmotor (V)").grid(row=current_row, column=0, padx=10, pady=5, sticky="e")
        # Hier Können Standartwerte für das Spannungs-Dropdown gesetzt werden. 
        # Limit Motorcontroller: 75V; Limit Netzteile 80V. -> Nicht Größer als 75V hinzufügen.
        psu_volt_combo = ttk.Combobox(win, values=["26", "28", "52", "72"], state="readonly", width=23) # Hier hinzufügen
        psu_volt_combo.grid(row=current_row, column=1, padx=10, pady=5)
        psu_volt_combo.current(0) 
        current_row += 1

        def on_save():
            
            motor_data = {}
            try:
                # Lastmotorgröße (capitalize to ensure consistent casing with Lastmotor.json)
                motor_data["Motortype"] = motortype_combo.get().capitalize()
                
                # PSU Voltage
                try:
                    motor_data["PSU_Voltage"] = float(psu_volt_combo.get())
                except ValueError:
                    raise ValueError("Ungültige PSU Voltage Auswahl.")

                for key, (ent, caster) in entries.items():
                    val_str = ent.get().strip()
                    if not val_str:
                        raise ValueError(f"Feld '{key}' darf nicht leer sein.")
                    try:
                        val = caster(val_str)
                    except ValueError:
                         raise ValueError(f"Falsches Format für '{key}'.")
                    
                    # Sanity checks --> Keine schweren Fehlentscheidungen werden zugelassen
                    if key == "Polzahl":
                        # [2.7] Polzahl muss mindestens 2 betragen (1 Polpaar minimum)
                        if val < 2:
                            raise ValueError("Polzahl muss mindestens 2 betragen (mind. 1 Polpaar).")
                        if val % 2 != 0:
                            raise ValueError("Polzahl muss durch 2 teilbar sein (Polpaare).")
                    
                    if key == "Load_Drehmoment":
                        if not (0 <= val <= 10):
                            raise ValueError("Lastmoment muss zwischen 0 und 10 Nm liegen.")

                    motor_data[key] = val

                # Netzteillimits hoch wählen, dass keine Leistungsbegrenzung durch falsche Paremter auftreten kann
                motor_data["PSU_Current_lim"] = 100.0
                motor_data["PSU_SINK_Lim"] = -100.0
                
                # Vergleichsparameter für Messergebnisse
                motor_data["BEMF_1_V"] = None
                motor_data["BEMF_2_V"] = None
                motor_data["BEMF_3_V"] = None
                motor_data["BEMF_1_P"] = None
                motor_data["BEMF_2_P"] = None
                motor_data["BEMF_3_P"] = None
                motor_data["current_under_load"] = None
                
            except ValueError as e:
                messagebox.showerror("Eingabefehler", str(e), parent=win)
                return

            # 2. Kontroll-Popup
            msg = "Bitte befestigen Sie den Motor an der Adapterplatte 1. Stellen Sie sicher, dass die Drehmomentmesswelle NICHT angeschlossen ist."
            if not messagebox.askokcancel("Motor befestigen", msg, parent=win, icon='info'):
                return

            # 3. Popup Start der Messung und PSU-Initialisierung
            wait_win = tk.Toplevel(win)
            wait_win.title("Messung läuft")
            ttk.Label(wait_win, text="Initialisiere Netzteil und warte auf Controller...").pack(padx=20, pady=20)
            wait_win.update() # Sicherstellen, dass Popup angezeigt wird
            
            psu_drive = None
            try:
                # 3a. Netzteil Testmotor initiailisieren --> für Motordaten wird nur ein Netzteil benötigt.
                if PSU_AVAILABLE:
                    try:
                        print(f"Verbindungsaufbau mit Netzteil des Testmotors {PSU1_IP}...")
                        psu_drive = IT6005C(PSU1_IP)
                        if not psu_drive.connect():
                            raise Exception("Verbindung zu Netzteil des Testmotors konnte nicht hergestellt werden.")
                        
                        v_set = motor_data["PSU_Voltage"]
                        c_lim = motor_data["PSU_Current_lim"]
                        sink_lim = motor_data["PSU_SINK_Lim"]
                        
                        if not self.setup_psu(psu_drive, "Netzteil des Testmotors", v_set, c_lim, sink_lim):
                            raise Exception("Konfiguration der PSU-Parameter des Testmotors fehlgeschlagen.")
                        
                        psu_drive.output_on()
                        print("Netzteil des Testmotors einschalten.")
                        
                        # 3b. Warte 10 Sekunden auf Boot-Vorgang des Motorcontrollers
                        print("Warte 10s für Boot-Vorgang des Motorcontrollers...")
                        time.sleep(10)
                        
                    except Exception as psu_err:
                        raise Exception(f"Netzteil-Initialisierung fehlgeschlagen: {psu_err}")
                else:
                    print("Netzteil konnte nicht gefunden werden. Bitte prüfen Sie, ob das Netzteil eingeschalten ist.")

                # Update Wait Window
                for child in wait_win.winfo_children():
                    child.config(text="Motorparameter werden ermittelt...")
                wait_win.update()

                # 4. Motordetektion durchführen mit den eingegebenen Paramteren
                cmd = _script_cmd(
                    DETECT_SCRIPT_PATH, 
                    motor_data["Motortype"],
                    "--poles", str(motor_data["Polzahl"]),
                    "--voltage", str(motor_data["PSU_Voltage"])
                )
                
                print(f"Motorparameter werden ermittelt: {' '.join(cmd)}")
                # Fehlerbehandlung, wenn Parameterermittlung scheitert
                process = subprocess.run(cmd, capture_output=True, text=True)
                
                if process.returncode != 0:
                    raise Exception(f"Detection script failed (Code {process.returncode}):\n{process.stderr}\nOutput:\n{process.stdout}")
                
                # 4b. ermittelte und berechnete Parameter auslesen.
                kv_kt_file = _app_dir() / "results" / "Motordaten_detektion_ergebnis.json"
                if kv_kt_file.exists():
                    try:
                        with open(kv_kt_file, "r") as f:
                            kv_kt_data = json.load(f)
                            motor_data["K_t"] = kv_kt_data.get("K_t", 0.0)
                            motor_data["Flux_Linkage"] = kv_kt_data.get("Flux_Linkage", 0.0)
                            motor_data["Phasenwiderstand"] = kv_kt_data.get("Phase_Resistance", 0.0)
                            motor_data["Induktivität"] = kv_kt_data.get("Inductance", 0.0)
                            
                            print(f"Ermittelte Parameter: K_t={motor_data['K_t']}, Phasenwiderstand={motor_data['Phasenwiderstand']}, Induktivität={motor_data['Induktivität']}")
                    except Exception as e:
                        print(f"Warnung: Motordaten_detektion_ergebnis.json konnte nicht gelesen werden: {e}")
                        motor_data["K_t"] = 0.0
                        motor_data["Phasenwiderstand"] = 0.0
                        motor_data["Induktivität"] = 0.0
                else:
                    print("Warnung: Motordaten_detektion_ergebnis.json nicht gefunden. Werte werden auf 0 gesetzt.")
                    motor_data["K_t"] = 0.0
                    motor_data["Phasenwiderstand"] = 0.0
                    motor_data["Induktivität"] = 0.0
                
                # 5. Motorkonfiguration auslesen
                hex_path = Path("mcconf_out/mcconf.hex")
                if hex_path.exists():
                    motor_config = hex_path.read_text("utf-8").strip()
                else:
                    raise Exception("Motorkonfiguration (mcconf.hex) nicht gefunden. --> Die Motordatenerkennung war nicht erfolgreich.")
                if not motor_config:
                    raise Exception("Motorkonfiguration konnte nicht richtig ermittelt werden.")

                motor_data["Motorconfig"] = motor_config
                
                # 6. in Parameterdatei speichern
                self.motor_data.append(motor_data)
                with open(JSON_PATH, "w", encoding="utf-8") as f:
                    json.dump(self.motor_data, f, indent=4)
                
                # Update UI lists
                self.motor_names = [m["Name"] for m in self.motor_data]
                self.combo_drive['values'] = self.motor_names
                
                wait_win.destroy()
                win.destroy()
                messagebox.showinfo("Erfolg", f"Motor '{motor_data['Name']}' wurde gespeichert und zur Liste hinzugefügt.")
                
            except Exception as e:
                wait_win.destroy()
                err_text = str(e)
                messagebox.showerror("Fehler", f"Ein Fehler ist aufgetreten:\n{err_text}", parent=win)
            
            finally:
                # 7. Netzteiloutput ausschalten
                if psu_drive:
                    try:
                        print("Netzteil Testmotor wird ausgeschaltet...")
                        psu_drive.output_off()
                        psu_drive.disconnect()
                    except Exception as px:
                        print(f"Fehler beim Ausschalten des Netzteils: {px}")

        # save button row moved down
        ttk.Button(win, text="Speichern", command=on_save).grid(row=current_row + 1, column=0, columnspan=2, pady=20)



    def open_update_motor_window(self):
        """bestehende Motordaten sollen aktualisiert werden --> z.B. um Daten einer späteren Charge einpflegen zu können"""
        if not self.check_password():
            return

        win = tk.Toplevel(self.root)
        win.title("Motordaten aktualisieren")
        win.geometry("400x200")

        ttk.Label(win, text="Wählen Sie einen Motor, bei dem die Motordaten aktualisiert werden sollen:").pack(pady=10)
        
        motor_combo = ttk.Combobox(win, values=self.motor_names, state="readonly", width=30)
        motor_combo.pack(pady=5)
        if self.motor_names:
            motor_combo.current(0)

        # Motordaten aktualisieren Button
        def on_update_motordaten():
            motor_name = motor_combo.get()
            if not motor_name:
                return
            
            # Bestehende Motordaten laden
            motor_entry = next((m for m in self.motor_data if m["Name"] == motor_name), None)
            if not motor_entry:
                messagebox.showerror("Fehler", "Motordaten konnten nicht geladen werden.", parent=win)
                return

            msg = "Bitte befestigen Sie den Motor an der Adapterplatte 1. Stellen Sie sicher, dass die Drehmomentmesswelle NICHT angeschlossen ist."
            if not messagebox.askokcancel("Motor anschließen", msg, parent=win, icon='info'):
                return

            # Start Detection Process similar to Add Motor but with existing params
            wait_win = tk.Toplevel(win)
            wait_win.title("Messung läuft")
            ttk.Label(wait_win, text="Initialisiere Netzteil und starte Messung...").pack(padx=20, pady=20)
            wait_win.update()

            psu_drive = None
            try:
                # 1. PSU Init
                if PSU_AVAILABLE:
                    try:
                        print(f"Connecting to PSU1 at {PSU1_IP}...")
                        psu_drive = IT6005C(PSU1_IP)
                        if not psu_drive.connect():
                            raise Exception("Could not connect to PSU1")
                        
                        v_set = motor_entry.get("PSU_Voltage")
                        c_lim = motor_entry.get("PSU_Current_lim", 100.0)
                        sink_lim = motor_entry.get("PSU_SINK_Lim", -100.0)

                        if not self.setup_psu(psu_drive, "PSU1 (Drive)", v_set, c_lim, sink_lim):
                             raise Exception("Failed to configure PSU1 parameters")
                        
                        psu_drive.output_on()
                        print("PSU1 Output ON. Waiting for boot-up...")
                        time.sleep(10)
                    except Exception as psu_err:
                        raise Exception(f"PSU Initialization failed: {psu_err}")

                # 2. Run Motordaten_detektion.py
                motortype = motor_entry.get("Motortype") 
                poles = motor_entry.get("Polzahl")
                voltage = motor_entry.get("PSU_Voltage")

                cmd = _script_cmd(
                    DETECT_SCRIPT_PATH, 
                    motortype,
                    "--poles", str(poles),
                    "--voltage", str(voltage)
                )
                
                print(f"Running detection update: {' '.join(cmd)}")
                process = subprocess.run(cmd, capture_output=True, text=True)
                
                if process.returncode != 0:
                    raise Exception(f"Detection script failed:\n{process.stderr}")

                # 3. Read Results
                motordaten_detektion_ergebnis_file = _app_dir() / "results" / "Motordaten_detektion_ergebnis.json"
                if not motordaten_detektion_ergebnis_file.exists():
                    raise Exception("Motordaten_detektion_ergebnis.json not found.")
                
                with open(motordaten_detektion_ergebnis_file, "r") as f:
                    new_results = json.load(f)

                # Get new mcconf
                hex_path = Path("mcconf_out/mcconf.hex")
                if hex_path.exists():
                    new_mcconf = hex_path.read_text("utf-8").strip()
                else:
                    raise Exception("mcconf.hex not found.")

                wait_win.destroy()

                # 4. Compare and Confirm
                old_R = motor_entry.get("Phasenwiderstand", 0.0)
                old_L = motor_entry.get("Induktivität", 0.0)
                old_Flux = motor_entry.get("Flux_Linkage", 0.0) 
                old_Kt = motor_entry.get("K_t", 0.0)

                new_R = new_results.get("Phase_Resistance", 0.0)
                new_L = new_results.get("Inductance", 0.0)
                new_Flux = new_results.get("Flux_Linkage", 0.0)
                new_Kt = new_results.get("K_t", 0.0)

                comp_msg = (
                    f"Vergleich der Messwerte für '{motor_name}':\n\n"
                    f"Parameter\t\tAlt\t\tNeu\n"
                    f"----------------------------------------------------\n"
                    f"Widerstand (mOhm):\t{old_R:.2f}\t->\t{new_R:.2f}\n"
                    f"Induktivität (uH):\t{old_L:.2f}\t->\t{new_L:.2f}\n"
                    f"Flux Linkage (mWb):\t{old_Flux:.2f}\t->\t{new_Flux:.2f}\n"
                    f"K_t (Nm/A):\t\t{old_Kt:.4f}\t->\t{new_Kt:.4f}\n\n"
                    f"Möchten Sie die Werte mitteln und speichern?"
                )

                confirm_win = tk.Toplevel(win)
                confirm_win.title("Werte übernehmen?")
                ttk.Label(confirm_win, text=comp_msg, justify=tk.LEFT).pack(padx=20, pady=20)

                def on_accept():
                    # Mittelwert berechnen und aktualisieren
                    motor_entry["Phasenwiderstand"] = (old_R + new_R) / 2
                    motor_entry["Induktivität"] = (old_L + new_L) / 2
                    motor_entry["Flux_Linkage"] = (old_Flux + new_Flux) / 2 # Add this key if it didn't exist
                    motor_entry["K_t"] = (old_Kt + new_Kt) / 2
                    motor_entry["Motorconfig"] = new_mcconf # New config replaces old one entirely
                    
                    # Save JSON
                    try:
                        with open(JSON_PATH, "w", encoding="utf-8") as f:
                            json.dump(self.motor_data, f, indent=4)
                        messagebox.showinfo("Erfolg", "Motordaten wurden aktualisiert.", parent=confirm_win)
                    except Exception as e:
                        messagebox.showerror("Fehler", f"Speichern fehlgeschlagen: {e}", parent=confirm_win)
                    
                    confirm_win.destroy()
                    win.destroy()

                def on_replace():
                    # Alte werte mit neuen ersetzten
                    motor_entry["Phasenwiderstand"] = new_R
                    motor_entry["Induktivität"] = new_L
                    motor_entry["Flux_Linkage"] = new_Flux
                    motor_entry["K_t"] = new_Kt
                    motor_entry["Motorconfig"] = new_mcconf
                    
                    # Save JSON
                    try:
                        with open(JSON_PATH, "w", encoding="utf-8") as f:
                            json.dump(self.motor_data, f, indent=4)
                        messagebox.showinfo("Erfolg", "Motordaten wurden ersetzt.", parent=confirm_win)
                    except Exception as e:
                        messagebox.showerror("Fehler", f"Speichern fehlgeschlagen: {e}", parent=confirm_win)
                    
                    confirm_win.destroy()
                    win.destroy()

                btn_frame = ttk.Frame(confirm_win)
                btn_frame.pack(pady=10)
                ttk.Button(btn_frame, text="Mitteln", command=on_accept).pack(side=tk.LEFT, padx=10)
                ttk.Button(btn_frame, text="Ersetzen", command=on_replace).pack(side=tk.LEFT, padx=10)
                ttk.Button(btn_frame, text="Abbrechen", command=confirm_win.destroy).pack(side=tk.LEFT, padx=10)

            except Exception as e:
                if wait_win.winfo_exists():
                    wait_win.destroy()
                messagebox.showerror("Fehler", f"Fehler beim Update: {e}", parent=win)
                
            finally:
                if psu_drive:
                    try:
                        psu_drive.output_off()
                        psu_drive.disconnect()
                    except:
                        pass

        # BEMF-Daten aktualisieren Button
        def on_bemf_update():
            motor_name = motor_combo.get()
            if not motor_name:
                return

            # Motordaten und Lastmotoren aus JSON neu laden, um veraltete In-Memory-Daten zu vermeiden
            self.motor_data = self.load_json_data(JSON_PATH)

            # Find motor data
            motor_entry = next((m for m in self.motor_data if m["Name"] == motor_name), None)
            if not motor_entry:
                messagebox.showerror("Fehler", "Motordaten konnten nicht geladen werden.", parent=win)
                return
            
            # Prüfen, ob Lastmotor definiert ist
            motortype = motor_entry.get("Motortype")
            if not motortype:
                messagebox.showerror("Fehler", "Motortyp nicht definiert.", parent=win)
                return
            
            # Lastmotor laden (case-insensitive Motortype comparison)
            self.lastmotoren_data = self.load_json_data(LASTMOTOREN_PATH)
            load_motor = next((m for m in self.lastmotoren_data if m.get("Motortype", "").lower() == motortype.lower()), None)

            if not load_motor:
                messagebox.showerror("Fehler", f"Kein Lastmotor für Typ '{motortype}' gefunden.", parent=win)
                return
            print(f"[on_bemf_update] Motortype='{motortype}' → Lastmotor: '{load_motor.get('Name')}' (Motortype: '{load_motor.get('Motortype')}')")

            msg = "Bitte verbinden Sie den Motor mit Platte 1 und stellen Sie die Kopplung zur Lastmaschine her (Welle verbinden)."
            if not messagebox.askokcancel("Setup", msg, parent=win, icon='info'):
                return

            # Oszilloskop-Vorabpruefung entfernt: libtiepie im Elternprozess blockiert USB-Zugriff im Subprocess.
            # BEMF-aktualisierung.py prueft das Oszilloskop selbst mit Retries.

            # Informationsfeld 
            wait_win = tk.Toplevel(win)
            wait_win.title("BEMF Messung")
            ttk.Label(wait_win, text="Initialisiere Netzteile und starte BEMF Messung...\n(Das kann einige Sekunden dauern)").pack(padx=20, pady=20)
            wait_win.update()

            psu_drive = None
            psu_load = None

            try:
                # --- PSU Initialization ---
                if PSU_AVAILABLE:
                    try:
                        print("Initializing PSUs for BEMF measurement...")
                        
                        # Drive Motor Params
                        d_volt = float(motor_entry.get("PSU_Voltage", 0.0))
                        d_curr = float(motor_entry.get("PSU_Current_lim", 100.0))
                        d_sink = float(motor_entry.get("PSU_SINK_Lim", -100.0))
                        
                        # Load Motor Params
                        l_volt = float(load_motor.get("PSU_Voltage", 0.0))
                        l_curr = float(load_motor.get("PSU_Current_lim", 100.0))
                        l_sink = float(load_motor.get("PSU_SINK_Lim", -100.0))

                        # Connect
                        print(f"Connecting to PSU1 (Drive) at {PSU1_IP}...")
                        psu_drive = IT6005C(PSU1_IP)
                        if not psu_drive.connect():
                            raise Exception("Failed to connect to PSU1 (Drive)")

                        print(f"Connecting to PSU2 (Load) at {PSU2_IP}...")
                        psu_load = IT6005C(PSU2_IP)
                        if not psu_load.connect():
                            raise Exception("Failed to connect to PSU2 (Load)")

                        # Configure
                        if not self.setup_psu(psu_drive, "PSU1 (Drive)", d_volt, d_curr, d_sink):
                            raise Exception("Failed to configure PSU1")
                        if not self.setup_psu(psu_load, "PSU2 (Load)", l_volt, l_curr, l_sink):
                            raise Exception("Failed to configure PSU2")

                        # Power On
                        psu_drive.output_on()
                        time.sleep(0.5) 
                        psu_load.output_on()
                        
                        print("PSUs active. Waiting 10s for controller boot...")
                        time.sleep(10.0)

                    except Exception as psu_err:
                        raise Exception(f"PSU Setup failed: {psu_err}")
                else:
                    print("Warning: PSU control unavailable.")

                # Delete stale result files before launching so we never read old data on failure
                for _stale in ("bemf_result.json", "OscilloscopeStream.csv"):
                    _p = _app_dir() / "results" / _stale
                    try:
                        _p.unlink(missing_ok=True)
                    except Exception:
                        pass

                # Parameter für BEMF-Messung übergeben und Script starten
                cmd = _script_cmd(
                    BEMF_SCRIPT_PATH,
                    "--motorconfig_drive", str(motor_entry["Motorconfig"]),
                    "--motorconfig_load", str(load_motor["Motorconfig"]),
                    "--bemf_rpm", str(motor_entry["BEMF_Drehzahl"]),
                    "--polzahl", str(motor_entry["Polzahl"]),
                    "--motor_name", motor_name,
                )

                print(f"Running BEMF update: {' '.join(cmd)}")
                process = subprocess.run(cmd, capture_output=True, text=True)

                wait_win.destroy()

                if process.returncode != 0:
                    raise Exception(f"Script failed:\n{process.stderr}\nOutput:\n{process.stdout}")

                # Ergebnisse speichern und auslesen
                res_file = _app_dir() / "results" / "bemf_result.json"
                if not res_file.exists():
                    raise Exception("bemf_result.json was not generated.")
                
                with open(res_file, "r") as f:
                    new_bemf = json.load(f)

                # Get user comment via popup (already on main thread, direct call)
                from tkinter import simpledialog
                win.attributes("-topmost", True)
                _res = simpledialog.askstring("BEMF-Protokoll", "Messung beendet.\nGeben Sie einen Kommentar für den Bericht ein (Leer lassen oder Abbrechen zum Überspringen):", parent=win)
                win.attributes("-topmost", False)
                user_comment = _res if _res is not None else ""
                print("Nutzer auffordern einen Kommentar für den BEMF Bericht einzugeben...")

                # BEMF-Protokoll PDF erzeugen
                try:
                    bemf_report_path = str(_app_dir() / "bemf_protokoll.py")
                    bemf_report_cmd = _script_cmd(
                        bemf_report_path,
                        "--motor_name", motor_name,
                        "--mat_nr", str(motor_entry.get("Mat.Nr.", "---")),
                        "--bemf_rpm", str(motor_entry.get("BEMF_Drehzahl", 0)),
                        "--user_comment", user_comment,
                    )
                    subprocess.run(bemf_report_cmd, capture_output=True, text=True)
                    print("BEMF-Protokoll.pdf erzeugt.")
                except Exception as rpt_err:
                    print(f"Warnung: Konnte BEMF-Protokoll nicht erzeugen: {rpt_err}")

                # Prepare Comparison
                comp_msg = f"BEMF Vergleich für '{motor_name}':\n\nParameter         Alt        Neu        % Diff\n" + "-"*60 + "\n"
                
                keys = ["BEMF_1_P", "BEMF_1_V", "BEMF_2_P", "BEMF_2_V", "BEMF_3_P", "BEMF_3_V"]
                
                # Ausgabe aufbereiten
                for k in keys:
                    old_val = motor_entry.get(k)
                    new_val = new_bemf.get(k, 0.0)
                    
                    if old_val is not None:
                        old_str = f"{old_val:.2f}"
                        if abs(old_val) > 0.001:
                            diff_pct = abs((new_val - old_val) / old_val) * 100.0
                            diff_str = f"{diff_pct:.1f}%"
                        else:
                            diff_str = "0.0%"
                    else:
                        old_str = "N/A"
                        diff_str = "N/A"
                        
                    comp_msg += f"{k:12s} {old_str:>8s} -> {new_val:>8.2f}    {diff_str:>8s}\n"

                comp_msg += "\nMöchten Sie diese Werte mitteln und speichern?"

                confirm_win = tk.Toplevel(win)
                confirm_win.title("BEMF Übernahme")
                
                lbl = ttk.Label(confirm_win, text=comp_msg, justify=tk.LEFT, font=("Consolas", 10))
                lbl.pack(padx=20, pady=20)

                def on_bemf_accept():
                    for k in keys:
                        old_val = motor_entry.get(k)
                        new_val = new_bemf.get(k, 0.0)
                        
                        if old_val is None:
                            motor_entry[k] = new_val
                        else:
                            motor_entry[k] = (old_val + new_val) / 2.0
                    
                    # Save
                    try:
                        with open(JSON_PATH, "w", encoding="utf-8") as f:
                            json.dump(self.motor_data, f, indent=4)
                        messagebox.showinfo("Erfolg", "BEMF Daten aktualisiert.", parent=confirm_win)
                    except Exception as e:
                        messagebox.showerror("Fehler", f"Speichern fehlgeschlagen: {e}", parent=confirm_win)
                    
                    confirm_win.destroy()
                    win.destroy()

                def on_bemf_replace():
                    for k in keys:
                        motor_entry[k] = new_bemf.get(k, 0.0)
                    
                    # Save
                    try:
                        with open(JSON_PATH, "w", encoding="utf-8") as f:
                            json.dump(self.motor_data, f, indent=4)
                        messagebox.showinfo("Erfolg", "BEMF Daten ersetzt.", parent=confirm_win)
                    except Exception as e:
                        messagebox.showerror("Fehler", f"Speichern fehlgeschlagen: {e}", parent=confirm_win)
                    
                    confirm_win.destroy()
                    win.destroy()

                btn_f = ttk.Frame(confirm_win)
                btn_f.pack(pady=10)
                ttk.Button(btn_f, text="Mitteln", command=on_bemf_accept).pack(side=tk.LEFT, padx=10)
                ttk.Button(btn_f, text="Ersetzen", command=on_bemf_replace).pack(side=tk.LEFT, padx=10)
                ttk.Button(btn_f, text="Abbrechen", command=confirm_win.destroy).pack(side=tk.LEFT, padx=10)

            except Exception as e:
                if wait_win.winfo_exists():
                    wait_win.destroy()
                messagebox.showerror("Fehler", f"BEMF Messung fehlgeschlagen:\n{e}", parent=win)

            finally:
                if psu_drive:
                    try:
                        psu_drive.output_off()
                        psu_drive.disconnect()
                    except Exception:
                        pass
                if psu_load:
                    try:
                        psu_load.output_off()
                        psu_load.disconnect()
                    except Exception:
                        pass


        btn_frame_main = ttk.Frame(win)
        btn_frame_main.pack(pady=20)
        
        ttk.Button(btn_frame_main, text="Motordaten", command=on_update_motordaten).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame_main, text="BEMF-Daten", command=on_bemf_update).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame_main, text="Schließen", command=win.destroy).pack(side=tk.LEFT, padx=10)

    def open_delete_motor_window(self):
        """Möglichkeit alte Motoren oder falsch ermittelte Motorparameter zu löschen."""
        if not self.check_password():
            return

        win = tk.Toplevel(self.root)
        win.title("Motor löschen")
        win.geometry("300x400")

        ttk.Label(win, text="Wählen Sie die zu löschenden Motoren:").pack(pady=10)

        # Dropdown mit Mehrfachauswahl erstellen
        frame = ttk.Frame(win)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        scrollbar = ttk.Scrollbar(frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Mehrfachauswahl erlauben
        lb = tk.Listbox(frame, selectmode=tk.EXTENDED, yscrollcommand=scrollbar.set)
        lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=lb.yview)

        # Liste füllen
        for m in self.motor_data:
            lb.insert(tk.END, m.get("Name", "Unnamed"))

        def on_delete():
            selection = lb.curselection()
            if not selection:
                messagebox.showwarning("Keine Auswahl", "Bitte wählen Sie mindestens einen Motor aus.", parent=win)
                return

            selected_names = [lb.get(i) for i in selection]
            msg = f"Möchten Sie folgende {len(selected_names)} Motoren wirklich löschen?\n\n" + "\n".join(selected_names)
            
            if messagebox.askyesno("Bestätigung", msg, parent=win):
                new_data = [m for i, m in enumerate(self.motor_data) if i not in selection]
                self.motor_data = new_data
                
                # Änderungen in JSON speichern
                try:
                    with open(JSON_PATH, "w", encoding="utf-8") as f:
                        json.dump(self.motor_data, f, indent=4)
                    
                    self.motor_names = [m["Name"] for m in self.motor_data]
                    self.combo_drive['values'] = self.motor_names
                    
                    if self.motor_names:
                        self.combo_drive.current(0)
                    else:
                        self.combo_drive.set('')

                    messagebox.showinfo("Erfolg", "Motoren wurden gelöscht.", parent=win)
                    win.destroy()
                except Exception as e:
                    messagebox.showerror("Fehler", f"Fehler beim Speichern: {e}", parent=win)

        btn_frame = ttk.Frame(win)
        btn_frame.pack(pady=20)
        
        ttk.Button(btn_frame, text="Löschen", command=on_delete).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text="Abbrechen", command=win.destroy).pack(side=tk.LEFT, padx=10)

    def update_status(self, message, color="black"):
        """Update the status label."""
        self.status_label.config(text=f"Status: {message}", foreground=color)
        self.root.update()
        
    def update_psu_status(self, message, color="gray"):
        """Update the PSU status label."""
        self.psu_status_label.config(text=f"PSU: {message}", foreground=color)
        self.root.update()

    def _verify_oscilloscope(self):
        """
        Prüft ob ein Oszilloskop per USB erreichbar ist.
        3 Versuche mit je 1s Pause. Prüft nur ob Gerät vorhanden ist (can_open),
        öffnet es NICHT -- damit der USB-Handle frei bleibt für den Subprocess.
        Gibt True zurück bei Erfolg, wirft Exception bei Misserfolg.
        """
        
        # Oszilloskop ist per USB angeschlossen -> Netzwerk-Suche deaktivieren
        libtiepie.network.auto_detect_enabled = False

        for attempt in range(3):
            libtiepie.device_list.update()
            print(f"Oszilloskop-Suche Versuch {attempt+1}/3: {len(libtiepie.device_list)} Geraet(e) erkannt.")
            for item in libtiepie.device_list:
                time.sleep(0.1)  # Kurze Pause zwischen Geräteprüfungen
                if item.can_open(libtiepie.DEVICETYPE_OSCILLOSCOPE):
                    print(f"Oszilloskop erkannt (Versuch {attempt+1}).")
                    return True

            if attempt < 2:
                print(f"Oszilloskop nicht erkannt. Erneuter Versuch in 1s... ({attempt+1}/3)")
                time.sleep(1.0)

        raise Exception("Kein Oszilloskop nach 3 Versuchen gefunden!\nBitte USB-Verbindung pruefen und erneut versuchen.")

    def _check_oscilloscope_available(self):
        # [1] Vorabprüfung mit Messagebox-Feedback (für Aufrufe aus dem Hauptthread).
        try:
            return self._verify_oscilloscope()
        except ImportError:
            messagebox.showerror(
                "Fehler",
                "libtiepie ist nicht installiert. Oszilloskop-Steuerung nicht verfügbar."
            )
            return False
        except Exception as e:
            messagebox.showerror(
                "Kein Oszilloskop gefunden",
                str(e)
            )
            return False

    def setup_psu(self, psu, name, voltage, current_limit, sink_current):
        """
        Configure a single PSU with the given parameters.
        Returns True if successful, False otherwise.
        """
        try:
            print(f"Configuring {name}...")
            psu.set_voltage(voltage)
            time.sleep(0.1)  # Small delay between commands
            psu.set_current_limit(current_limit)
            time.sleep(0.1)
            psu.set_sink_current_limit(sink_current)
            time.sleep(0.1)
            
            # Verify settings were applied
            actual_voltage = psu.get_voltage_setpoint()
            actual_current = psu.get_current_setpoint()
            
            print(f"{name} configured: {voltage}V (read: {actual_voltage}V), {current_limit}A limit (read: {actual_current}A), {sink_current}A sink")
            
            # Check if setpoints are close enough
            if abs(actual_voltage - voltage) > 0.1:
                print(f"Warning: {name} voltage setpoint mismatch!")
                return False
            if abs(actual_current - current_limit) > 0.1:
                print(f"Warning: {name} current setpoint mismatch!")
                return False
                
            return True
        except Exception as e:
            print(f"Error configuring {name}: {e}")
            return False

    def verify_psu_output(self, psu, name, expected_voltage, timeout=PSU_VERIFICATION_TIMEOUT):
        """
        Turn on PSU and verify the output voltage is within tolerance.
        Returns True if verified, False otherwise.
        """
        psu.output_on()
        
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                measured_voltage = psu.get_voltage_measured()
                print(f"{name} measured voltage: {measured_voltage:.2f}V (expected: {expected_voltage:.2f}V)")
                
                if abs(measured_voltage - expected_voltage) < PSU_VOLTAGE_TOLERANCE:
                    print(f"{name} output verified!")
                    return True
            except Exception as e:
                print(f"{name} measurement error: {e}")
            
            time.sleep(0.5)
        
        print(f"Warning: {name} voltage verification timeout!")
        return False

    def shutdown_psus(self):
        """Turn off and disconnect both PSUs."""
        print("\n--- Shutting down PSUs ---")
        try:
            if self.psu1:
                self.psu1.output_off()
                self.psu1.disconnect()
                self.psu1 = None
        except Exception as e:
            print(f"Error shutting down PSU1: {e}")
        
        try:
            if self.psu2:
                self.psu2.output_off()
                self.psu2.disconnect()
                self.psu2 = None
        except Exception as e:
            print(f"Error shutting down PSU2: {e}")
        
        self.update_psu_status("Disconnected", "gray")
        print("PSUs shut down.")

    def run_test_sequence(self, drive_motor, load_motor, cmd):
        """Run the complete test sequence with PSU control."""
        try:
            self.test_running = True
            
            print("\n--- Reading Motor Configuration ---")
            
            # Doppelchecken, ob alle nötigen Parameter vorhanden sind
            def get_param(motor_config, key, name):
                if key not in motor_config:
                    raise ValueError(f"Missing '{key}' in {name} configuration!")
                return float(motor_config[key])

            # Drive Motor PSU
            psu1_voltage = get_param(drive_motor, "PSU_Voltage", "Drive Motor")
            psu1_current = get_param(drive_motor, "PSU_Current_lim", "Drive Motor")
            psu1_sink = get_param(drive_motor, "PSU_SINK_Lim", "Drive Motor")
            
            # Load Motor PSU
            psu2_voltage = get_param(load_motor, "PSU_Voltage", "Load Motor")
            psu2_current = get_param(load_motor, "PSU_Current_lim", "Load Motor")
            psu2_sink = get_param(load_motor, "PSU_SINK_Lim", "Load Motor")
            
            print(f"PSU1 (Drive): {psu1_voltage}V, {psu1_current}A, Sink {psu1_sink}A")
            print(f"PSU2 (Load):  {psu2_voltage}V, {psu2_current}A, Sink {psu2_sink}A")

            # 2. Connect
            self.update_status("Mit Netzteil verbinden...", "blue")
            self.update_psu_status("Verbinden...", "orange")
            print("\n--- Mit Netzteilen verbinden ---")
            
            self.psu1 = IT6005C(PSU1_IP)
            if not self.psu1.connect():
                raise Exception("Fehler beim Verbinden mit PSU1 (Drive)")
            
            self.psu2 = IT6005C(PSU2_IP)
            if not self.psu2.connect():
                raise Exception("Fehler beim Verbinden mit PSU2 (Load)")
            
            self.update_psu_status("Verbunden", "green")
            
            # 3. Configure
            self.update_status("Netzteile konfigurieren...", "blue")
            print("\n--- Netzteile Konfigurieren ---")
            
            # Configure PSU1
            if not self.setup_psu(self.psu1, "PSU1 (Testmotor)", psu1_voltage, psu1_current, psu1_sink):
                raise Exception("Fehler beim Konfigurieren von PSU1 (Testmotor)")
            
            # Configure PSU2
            if not self.setup_psu(self.psu2, "PSU2 (Lastmotor)", psu2_voltage, psu2_current, psu2_sink):
                raise Exception("Fehler beim Konfigurieren von PSU2 (Lastmotor)")
            
            # 4. Turn ON and Verify Output
            self.update_status("Starting PSU1 (Testmotor)...", "blue")
            self.update_psu_status("PSU1 starting...", "orange")
            print("\n--- PSU1 Starten (Testmotor) ---")
            if not self.verify_psu_output(self.psu1, "PSU1 (Testmotor)", psu1_voltage):
                raise Exception("Fehler bei der Spannungsüberprüfung von PSU1 (Testmotor)!")
            
            time.sleep(0.5) #Einschaltverzögerung des 2. Netzteils --> stellt sicher dass drive und load comport richtig vergeben werden

            self.update_status("Starting PSU2 (Lastmotor)...", "blue")
            self.update_psu_status("PSU2 starting...", "orange")
            print("\n--- PSU2 Starten (Lastmotor) ---")
            if not self.verify_psu_output(self.psu2, "PSU2 (Lastmotor)", psu2_voltage):
                raise Exception("Fehler bei der Spannungsüberprüfung von PSU2 (Lastmotor)!")
            
            self.update_psu_status("Beide Netzteile sind eingeschaltet", "green")
            
            # 5. Delay before starting test
            print("\n--- Warten 10 Sekunden bevor der Test startet ---")
            for i in range(10, 0, -1):
                self.update_status(f"Test startet in {i} Sekunden...", "blue")
                time.sleep(1)
            
            # 6. Run Test
            self.update_status("Test wird gestartet...", "green")
            print("\n--- Testskript starten ---")
            print(f"Starte: {' '.join(cmd)}")
            
            # Prüfskript starten und auf Abschluss warten.
            # Netzteile bleiben während des gesamten Tests eingeschaltet
            # stdout erfassen, um Pass/Fail-Ergebnis zu analysieren
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            
            final_status = None
            all_lines = []      # every line printed by messablauf.py
            detail_lines = []
            capturing_details = False
            
            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                if line:
                    sys.stdout.write(line)
                    all_lines.append(line.rstrip())

                    stripped = line.strip()

                    # --- Trigger-strings must match exactly what messablauf.py prints ---
                    if "--- Ergebniss Details (Popup) ---" in line:
                        capturing_details = True
                        detail_lines = []
                        detail_lines.append("Ergebnisse:")
                    elif "--- Aktualisiere Basislinie ---" in line:
                        capturing_details = True
                        detail_lines.append("\nDatenbank aktualisieren:")
                    elif capturing_details and ("Ergebniss:" in line):
                        # Stop capturing when the final verdict line appears
                        pass
                    elif capturing_details and stripped:
                        detail_lines.append(stripped)

                    if "Ergebniss: BESTANDEN" in line:
                        final_status = "Pass"
                    elif "Ergebniss: FEHLGESCHLAGEN" in line:
                        final_status = "Fail"
                    elif "Ergebniss: BASISLINIE GESPEICHERT" in line:
                        final_status = "Baseline Set"
                        
            return_code = process.poll()
            
            print(f"\n--- Test mit dem Returncode absgeschlossen: {return_code} ---")
            
            details_str = "\n".join(detail_lines)
             
            if final_status == "Pass":
                self.update_status("Motor i.O.", "green")
                self.root.after(0, lambda: messagebox.showinfo("Ergebnis: Erfolgreich", f"Motorprüfung erfolgreich!\n\n{details_str}"))
            elif final_status == "Fail":
                self.update_status("Fehlerhafter Motor", "red")
                self.root.after(0, lambda: messagebox.showerror("Ergebnis: Fehlgeschlagen", f"Motorprüfung fehlgeschlagen!\n\n{details_str}"))
            elif final_status == "Baseline Set":
                self.update_status("Baseline values saved.", "blue")
                self.root.after(0, lambda: messagebox.showinfo("Baseline Set", f"Referenzwerte wurden gespeichert.\n\n{details_str}"))
                self.root.after(0, self.reload_data_safe)
            else:
                self.update_status("Test fehlgeschlagen.", "red")
                if return_code != 0:
                    # Extract the most useful lines for the popup:
                    # 1. Lines with explicit error keywords
                    ERROR_KEYWORDS = ("[X]", "Error", "Fehler", "Traceback", "Exception",
                                      "sys.exit", "failed", "nicht gefunden", "not found")
                    error_lines = [l for l in all_lines if any(k.lower() in l.lower() for k in ERROR_KEYWORDS)]
                    # 2. Fall back to last 15 lines if nothing was matched
                    if not error_lines:
                        error_lines = all_lines[-15:]
                    error_text = "\n".join(error_lines)
                    self.root.after(0, lambda t=error_text: messagebox.showerror(
                        "Fehler: Testskript abgebrochen",
                        f"Das Testskript wurde mit einem Fehler beendet (Code {return_code}).\n\n{t}"
                    ))
            
        except Exception as e:
            print(f"Fehler während der Mess-sequenz: {e}")
            self.update_status(f"Fehler: {e}", "red")
            self.update_psu_status("Fehler", "red")
            err_text = str(e)
            self.root.after(0, lambda: messagebox.showerror("Fehler", f"Mess-sequenz fehlgeschlagen: {err_text}\n\nMessung wurde NICHT gestartet."))
        
        finally:
            # Always shut down PSUs after test completes or on error
            self.shutdown_psus()
            self.update_status("Bereit", "black")
            self.test_running = False
            # Re-enable all buttons
            self.start_btn.config(state="normal")
            self.bemf_btn.config(state="normal")
            self.add_motor_btn.config(state="normal")
            self.update_motor_btn.config(state="normal")
            self.del_motor_btn.config(state="normal")

    def reload_data_safe(self):

        self.motor_data = self.load_json_data(JSON_PATH)
        self.lastmotoren_data = self.load_json_data(LASTMOTOREN_PATH)
        # [2.8] Motor-Namen und Combobox nach Datenreload aktualisieren
        self.motor_names = [m["Name"] for m in self.motor_data]
        self.combo_drive["values"] = self.motor_names

    def start_test(self):
        if self.test_running:
            messagebox.showwarning("Warnung", "Test läuft bereits!")
            return
    
        # Motordaten aus JSON neu laden, um veraltete In-Memory-Daten zu vermeiden
        self.motor_data = self.load_json_data(JSON_PATH)
        self.motor_names = [m["Name"] for m in self.motor_data]

        drive_name = self.combo_drive.get()
        # load_name selection removed
        
        if not drive_name:
            messagebox.showwarning("Warnung", "Bitte wählen Sie einen Antriebsmotor aus.")
            return

        drive_motor = next((m for m in self.motor_data if m["Name"] == drive_name), None)
        
        if not drive_motor:
            messagebox.showerror("Fehler", "Ausgewählte Motor-Konfiguration nicht gefunden.")
            return

        # Automatic Load Motor Selection
        motortype = drive_motor.get("Motortype")
        if not motortype:
             messagebox.showerror("Fehler", f"Testmotor '{drive_name}' hat keinen 'Motortype' definiert.")
             return
        
        # Reload lastmotoren to ensure freshness
        self.lastmotoren_data = self.load_json_data(LASTMOTOREN_PATH)
        
        # Find matching load motor (case-insensitive Motortype comparison)
        load_motor = next((m for m in self.lastmotoren_data if m.get("Motortype", "").lower() == motortype.lower()), None)
        
        if not load_motor:
            messagebox.showerror("Fehler", f"Kein Lastmotor in {LASTMOTOREN_PATH} gefunden, der zum Motortyp '{motortype}' passt.")
            return
            
        load_name = load_motor.get("Name", "Unbekannter Lastmotor")
        print(f"[start_test] Drive: {drive_name} (Motortype: {motortype}), Lastmotor: {load_name} (Motortype: {load_motor.get('Motortype')})")

        # Extract parameters
        try:
            target_rpm = str(drive_motor["Drive_Drehzahl"])
            target_load_moment = str(drive_motor["Load_Drehmoment"])
            bemf_rpm = str(drive_motor["BEMF_Drehzahl"])
            val_polzahl = str(drive_motor["Polzahl"])
            
            mcconf_drive = str(drive_motor["Motorconfig"])
            mcconf_load = str(load_motor["Motorconfig"])
            
            # Retrieve K_t (default to 0.5 if missing in older JSONs), KV not used anymore
            val_kv = "0.0" # Placeholder as script might expect argument position
            val_kt = str(drive_motor.get("K_t", 0.0))
            
        except KeyError as e:
            messagebox.showerror("Fehler", f"Fehlender Schlüssel in der JSON-Konfiguration: {e}")
            return


        # Construct command for testing script
        cmd = _script_cmd(
            TEST_SCRIPT_PATH,
            "--target_rpm", target_rpm,
            "--target_load_moment", target_load_moment,
            "--bemf_rpm", bemf_rpm,
            "--motorconfig_drive", mcconf_drive,
            "--motorconfig_load", mcconf_load,
            "--motor_name", drive_name,
            "--mat_nr", str(drive_motor.get("Mat.Nr.", "")),
            "--K_V", val_kv,
            "--K_t", val_kt,
            "--polzahl", val_polzahl
        )

        # Check if PSU control is available - REQUIRED for test to run
        if not PSU_AVAILABLE:
            messagebox.showerror("Fehler", "PSU-Steuerung ist nicht verfügbar!\n\nBitte stellen Sie sicher, dass lan_PSU.py vorhanden ist und pyvisa installiert ist.\n\nTest wird NICHT gestartet.")
            return

        # Oszilloskop-Vorabpruefung entfernt: libtiepie im Elternprozess blockiert USB-Zugriff im Subprocess.
        # messablauf.py prueft das Oszilloskop selbst mit 3 Retries.

        # Disable all buttons during test
        self.start_btn.config(state="disabled")
        self.bemf_btn.config(state="disabled")
        self.add_motor_btn.config(state="disabled")
        self.update_motor_btn.config(state="disabled")
        self.del_motor_btn.config(state="disabled")
        
        # Run test sequence in a separate thread to keep GUI responsive
        test_thread = threading.Thread(
            target=self.run_test_sequence,
            args=(drive_motor, load_motor, cmd),
            daemon=True
        )
        test_thread.start()

    def start_bemf_messung(self):
        """Start a BEMF-only measurement (read-only, no database update)."""
        if self.test_running:
            messagebox.showwarning("Warnung", "Ein Test läuft bereits!")
            return

        drive_name = self.combo_drive.get()
        if not drive_name:
            messagebox.showwarning("Warnung", "Bitte wählen Sie einen Motor aus.")
            return

        # Motordaten aus JSON neu laden, um veraltete In-Memory-Daten zu vermeiden
        self.motor_data = self.load_json_data(JSON_PATH)
        self.lastmotoren_data = self.load_json_data(LASTMOTOREN_PATH)
        self.motor_names = [m["Name"] for m in self.motor_data]

        drive_motor = next((m for m in self.motor_data if m["Name"] == drive_name), None)
        if not drive_motor:
            messagebox.showerror("Fehler", "Ausgewählte Motor-Konfiguration nicht gefunden.")
            return

        motortype = drive_motor.get("Motortype")
        if not motortype:
            messagebox.showerror("Fehler", f"Motor '{drive_name}' hat keinen 'Motortype' definiert.")
            return

        load_motor = next((m for m in self.lastmotoren_data if m.get("Motortype", "").lower() == motortype.lower()), None)
        if not load_motor:
            messagebox.showerror("Fehler", f"Kein Lastmotor für Typ '{motortype}' gefunden.")
            return

        print(f"[start_bemf_messung] Drive: {drive_name} (Motortype: {motortype}), Lastmotor: {load_motor.get('Name')} (Motortype: {load_motor.get('Motortype')})")

        msg = "Bitte verbinden Sie den Motor mit Platte 1 und stellen Sie die Kopplung zur Lastmaschine her (Welle verbinden)."
        if not messagebox.askokcancel("Setup", msg, icon='info'):
            return

        if not PSU_AVAILABLE:
            messagebox.showerror("Fehler", "PSU-Steuerung ist nicht verfügbar!\n\nTest wird NICHT gestartet.")
            return

        # Disable all buttons during measurement
        self.start_btn.config(state="disabled")
        self.bemf_btn.config(state="disabled")
        self.add_motor_btn.config(state="disabled")
        self.update_motor_btn.config(state="disabled")
        self.del_motor_btn.config(state="disabled")

        thread = threading.Thread(
            target=self.run_bemf_sequence,
            args=(drive_motor, load_motor),
            daemon=True
        )
        thread.start()

    def run_bemf_sequence(self, motor_entry, load_motor):
        """Run BEMF measurement without updating the database."""
        psu_drive = None
        psu_load = None
        try:
            self.test_running = True
            motor_name = motor_entry["Name"]

            # --- PSU Initialization ---
            if PSU_AVAILABLE:
                try:
                    self.update_status("Netzteile initialisieren...", "blue")
                    self.update_psu_status("Verbinden...", "orange")
                    print("Initializing PSUs for BEMF measurement...")

                    d_volt = float(motor_entry.get("PSU_Voltage", 0.0))
                    d_curr = float(motor_entry.get("PSU_Current_lim", 100.0))
                    d_sink = float(motor_entry.get("PSU_SINK_Lim", -100.0))

                    l_volt = float(load_motor.get("PSU_Voltage", 0.0))
                    l_curr = float(load_motor.get("PSU_Current_lim", 100.0))
                    l_sink = float(load_motor.get("PSU_SINK_Lim", -100.0))

                    print(f"Connecting to PSU1 (Drive) at {PSU1_IP}...")
                    psu_drive = IT6005C(PSU1_IP)
                    if not psu_drive.connect():
                        raise Exception("Failed to connect to PSU1 (Drive)")

                    print(f"Connecting to PSU2 (Load) at {PSU2_IP}...")
                    psu_load = IT6005C(PSU2_IP)
                    if not psu_load.connect():
                        raise Exception("Failed to connect to PSU2 (Load)")

                    if not self.setup_psu(psu_drive, "PSU1 (Drive)", d_volt, d_curr, d_sink):
                        raise Exception("Failed to configure PSU1")
                    if not self.setup_psu(psu_load, "PSU2 (Load)", l_volt, l_curr, l_sink):
                        raise Exception("Failed to configure PSU2")

                    psu_drive.output_on()
                    time.sleep(0.5)
                    psu_load.output_on()

                    self.update_psu_status("Beide Netzteile eingeschaltet", "green")
                    print("PSUs active. Waiting 10s for controller boot...")
                    time.sleep(10.0)

                except Exception as psu_err:
                    raise Exception(f"PSU Setup failed: {psu_err}")
            else:
                print("Warning: PSU control unavailable.")

            # Run BEMF script
            cmd = _script_cmd(
                BEMF_SCRIPT_PATH,
                "--motorconfig_drive", str(motor_entry["Motorconfig"]),
                "--motorconfig_load", str(load_motor["Motorconfig"]),
                "--bemf_rpm", str(motor_entry["BEMF_Drehzahl"]),
                "--polzahl", str(motor_entry["Polzahl"]),
                "--motor_name", motor_name,
                "--mat_nr", str(motor_entry.get("Mat.Nr.", "")),
                "--K_t", str(motor_entry.get("K_t", 0.5)),
            )

            # Delete stale result files before launching so we never read old data on failure
            for _stale in ("bemf_result.json", "OscilloscopeStream.csv"):
                _p = _app_dir() / "results" / _stale
                try:
                    _p.unlink(missing_ok=True)
                except Exception:
                    pass

            self.update_status("BEMF-Messung läuft...", "blue")
            print(f"Running BEMF measurement: {' '.join(cmd)}")
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

            all_lines = []
            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                if line:
                    sys.stdout.write(line)
                    all_lines.append(line.rstrip())

            return_code = process.poll()
            if return_code != 0:
                ERROR_KEYWORDS = ("[X]", "Error", "Fehler", "Traceback", "Exception", "failed", "not found")
                error_lines = [l for l in all_lines if any(k.lower() in l.lower() for k in ERROR_KEYWORDS)]
                if not error_lines:
                    error_lines = all_lines[-20:]
                raise Exception(f"Script failed (code {return_code}):\n" + "\n".join(error_lines))

            # Read results
            res_file = _app_dir() / "results" / "bemf_result.json"
            if not res_file.exists():
                raise Exception("bemf_result.json was not generated.")

            with open(res_file, "r") as f:
                new_bemf = json.load(f)

            # Get user comment via popup in main thread
            import queue
            from tkinter import simpledialog
            comment_queue = queue.Queue()

            def _ask_cmnt_meas():
                self.root.attributes("-topmost", True)
                res = simpledialog.askstring("BEMF-Protokoll", "Messung beendet.\nGeben Sie einen Kommentar für den Bericht ein (Leer lassen oder Abbrechen zum Überspringen):", parent=self.root)
                self.root.attributes("-topmost", False)
                comment_queue.put(res if res is not None else "")

            self.root.after(0, _ask_cmnt_meas)
            print("Nutzer auffordern einen Kommentar für den BEMF Bericht einzugeben...")
            user_comment = comment_queue.get()

            # Generate BEMF-Protokoll PDF
            try:
                bemf_report_path = str(_app_dir() / "bemf_protokoll.py")
                bemf_report_cmd = _script_cmd(
                    bemf_report_path,
                    "--motor_name", motor_name,
                    "--mat_nr", str(motor_entry.get("Mat.Nr.", "---")),
                    "--bemf_rpm", str(motor_entry.get("BEMF_Drehzahl", 0)),
                    "--user_comment", user_comment,
                )
                subprocess.run(bemf_report_cmd, capture_output=True, text=True)
                print("BEMF-Protokoll erzeugt.")
            except Exception as rpt_err:
                print(f"Warnung: Konnte BEMF-Protokoll nicht erzeugen: {rpt_err}")

            # Show comparison (read-only — no database update)
            keys = ["BEMF_1_P", "BEMF_1_V", "BEMF_2_P", "BEMF_2_V", "BEMF_3_P", "BEMF_3_V"]
            comp_msg = f"BEMF Messergebnisse für '{motor_name}':\n\n"
            comp_msg += "Parameter        Baseline      Gemessen       % Diff\n" + "-" * 60 + "\n"
            for k in keys:
                old_val = motor_entry.get(k)
                new_val = new_bemf.get(k, 0.0)
                
                if old_val is not None:
                    old_str = f"{old_val:.2f}"
                    if abs(old_val) > 0.001:
                        diff_pct = abs((new_val - old_val) / old_val) * 100.0
                        diff_str = f"{diff_pct:.1f}%"
                    else:
                        diff_str = "0.0%"
                else:
                    old_str = "N/A"
                    diff_str = "N/A"
                    
                comp_msg += f"{k:12s} {old_str:>8s}      {new_val:>8.2f}     {diff_str:>8s}\n"

            comp_msg += "\n(Nur-Lese-Modus – Werte werden NICHT in die Datenbank übernommen.)"

            self.update_status("BEMF-Messung abgeschlossen", "green")
            
            def show_results():
                res_win = tk.Toplevel(self.root)
                res_win.title("BEMF-Messung Ergebnis")
                lbl = ttk.Label(res_win, text=comp_msg, justify=tk.LEFT, font=("Consolas", 10))
                lbl.pack(padx=20, pady=20)
                ttk.Button(res_win, text="OK", command=res_win.destroy).pack(pady=(0, 20))
                
            self.root.after(0, show_results)

        except Exception as e:
            print(f"BEMF-Messung Fehler: {e}")
            self.update_status(f"Fehler: {e}", "red")
            self.update_psu_status("Fehler", "red")
            err_text = str(e)
            self.root.after(0, lambda t=err_text: messagebox.showerror("Fehler", f"BEMF-Messung fehlgeschlagen:\n{t}"))

        finally:
            if psu_drive:
                try:
                    psu_drive.output_off()
                    psu_drive.disconnect()
                except Exception:
                    pass
            if psu_load:
                try:
                    psu_load.output_off()
                    psu_load.disconnect()
                except Exception:
                    pass
            self.test_running = False
            self.update_status("Bereit", "black")
            self.update_psu_status("Nicht verbunden", "gray")
            # Re-enable all buttons
            self.start_btn.config(state="normal")
            self.bemf_btn.config(state="normal")
            self.add_motor_btn.config(state="normal")
            self.update_motor_btn.config(state="normal")
            self.del_motor_btn.config(state="normal")


if __name__ == "__main__":
    root = tk.Tk()
    app = TestBenchLauncher(root)
    root.mainloop()
