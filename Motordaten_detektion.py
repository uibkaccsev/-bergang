"""
Mit diesem Skritp wird der Setup Wizard FOC des Vesc-tools durgeführt. Es war nicht in der Bibliothek implementiert --> selbst nachgebaut
Skritp ist nicht als eigenständig gedacht, sondern wird von gui_launcer.py mit den nötigen Parametern aufgerufen
"""

import argparse
import time
import struct
import serial
import serial.tools.list_ports
import sys
import subprocess
import json
import math
from pathlib import Path

# sicherstellen, dass die Bibliotheken pyvesc, serial und libtiepie installiert sind --> sie wrude in gui_launcer.py nicht verwendet.
try:
    from pyvesc.protocol.packet import codec
    from pyvesc.protocol.interface import encode_request, decode
    try:
        from pyvesc.messages.getters import GetMotorConfig
        from pyvesc.messages.setters import SetMotorConfig
        HAVE_PYVESC_MCCONF = True
    except Exception:
        GetMotorConfig = None
        SetMotorConfig = None
        HAVE_PYVESC_MCCONF = False
except Exception as e:
    print("pyvesc is required (codec + interface).")
    print("Install: pip install pyvesc")
    raise

# Communication ID's 
COMM_GET_MCCONF = 14
COMM_SET_MCCONF = 13
COMM_DETECT_APPLY_ALL_FOC = 58


# Verlustlimits und Drehzahl für die Erkennung festsetzten
MOTORTYPE_PRESETS = {
    "Gross":  {"max_power_loss": 400.0,  "openloop_rpm": 1500},
    "Mittel":   {"max_power_loss":200.0,  "openloop_rpm": 1500},
    "Klein": {"max_power_loss": 150.0, "openloop_rpm": 1500},
}

# nach Comports suchen (Windows) und Ports mit verbundenen Vescs erkennen
found_ports = []
for port in serial.tools.list_ports.comports():
    if port.vid == 0x0483 and port.pid == 0x5740:
        found_ports.append(port.device)

def get_port_number(port_name):
    nums = ''.join(filter(str.isdigit, port_name))
    return int(nums) if nums else 0

found_ports.sort(key=get_port_number)

# Standart-Werte
PORT = found_ports[0]
BAUD = 115200
MAX_INPUT_CURRENT = 60.0
DETECT_CAN = False
SL_ERPM = 4000.0
TIMEOUT = 60.0
# -------------------------


def motorconfig_for_setup_wizard(poles, Spannung):
    """Generate a default motorconfig dictionary for use in the setup wizard."""
    if Spannung == 26:
        bat_cells = 7
    elif Spannung == 52:
        bat_cells = 14
    else:
        bat_cells = round(Spannung / 3.7)  # default/fallback
    bat_cells = f"{bat_cells:02x}"
    poles = f"{poles:02x}"
    #dummy motorconfig hex string. mit variablen für Polzahl, und Batterie-zellzahl
    mc = "0301de0e3f829cf7010002003c000000c4ffffff42700000c2700000271000325a000000c7c3500047c350001f404396000044bb8000006e023a00dc00c827102af8005564556405dc0032251c49b71b00c9b71b00271027102710431600004489800041200000026c1f40479c400044160000ff010302050604ff44fa00003dc3c6fc4230fd1e46ea60003df5c28f004334000040e000000044fa000046ea600038c879d83849c7743d353c8a3bd400794bb644a33d4ccccdfc1842480000447a00002710451c400044af00000000038400c8000a0000000a00050000ff9cffffffffffffffff43fa0000451c4000457a0000000000000001000003e8000300c80028003c012c00960000453b800000053a83126f010145001ab44500083d450006e5ffe5000e000d0000000000000101457a00000000000000232800c800c80000053b83126f3b83126f38d1b71707d0446100000146c35000003ccccccd000000000000000039b7803407d03f80000000000000000000643d4ccccd3b96bb990190000001f400c83f0000000000200003e803e80672067201f4000000000010453b80004708b80046c350004553400000083f1c28f603e800fa032d"+poles+"40313b143da9fbe700"+bat_cells+"40c000003f80000001032d41003200000b5409c4106810cc000056ca03"
    return mc

def int32_from_scaled_float(val, scale=1000.0):
    return int(round(val * scale))


def build_detect_payload(detect_can, max_power_loss, min_current_in,
                         max_current_in, openloop_rpm, sl_erpm):
    payload = bytearray()
    payload.append(COMM_DETECT_APPLY_ALL_FOC)
    payload.append(1 if detect_can else 0)
    for v in (max_power_loss, min_current_in, max_current_in, openloop_rpm, sl_erpm):
        payload += struct.pack(">i", int32_from_scaled_float(v, 1000.0))
    return bytes(payload)


def frame_and_send(ser, payload: bytes):
    framed = codec.frame(payload)
    ser.write(framed)


def read_framed_packet(ser, timeout_s=30.0):
    deadline = time.time() + timeout_s
    buf = bytearray()
    while time.time() < deadline:
        n = ser.in_waiting
        if n:
            buf += ser.read(n)
            try:
                payload, consumed = codec.unframe(bytes(buf))
                if payload is not None and consumed > 0:
                    del buf[:consumed]
                    return payload
            except Exception:
                pass
        time.sleep(0.01)
    return None


def send_get_mcconf(ser):
    if GetMotorConfig is not None:
        ser.write(encode_request(GetMotorConfig()))
    else:
        frame_and_send(ser, bytes([COMM_GET_MCCONF]))


def try_decode_pyvesc(payload):
    try:
        msg, consumed, error = decode(payload, recv=True)
        if msg is not None:
            return msg
    except Exception:
        pass
    return None


def save_mcconf_bytes(mc_bytes: bytes, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    binfile = out_dir / "mcconf.bin"
    hexfile = out_dir / "mcconf.hex"
    with binfile.open("wb") as f:
        f.write(mc_bytes)
    with hexfile.open("w") as f:
        f.write(mc_bytes.hex())
    return binfile, hexfile


def run_wizard(port, baud, size_preset, custom_max_loss, custom_i_in_max,
               detect_can, openloop_rpm, sl_erpm, poles, timeout, voltage):
    # choose preset: only MOTORTYPE_PRESETS are used for named motortypes now

    mt = MOTORTYPE_PRESETS.get(size_preset)
    if not mt:
        raise ValueError("Unknown motor type/preset: " + str(size_preset))
    max_power_loss = mt["max_power_loss"]
    max_current_in = MAX_INPUT_CURRENT
    
    preset_default_poles = mt.get("default_poles")

    min_current_in = -max_current_in

    poles = int(poles)
    openloop_rpm = mt["openloop_rpm"]*poles
    print("Configuration to be used for detection:")
    print(f"  detect_can:       {detect_can}")
    print(f"  max_power_loss:   {max_power_loss} W")
    print(f"  min_current_in:   {min_current_in} A")
    print(f"  max_current_in:   {max_current_in} A")
    print(f"  openloop_rpm:     {openloop_rpm} erpm")
    print(f"  sl_erpm:          {sl_erpm} erpm")
    print(f"  motor poles (si): {poles} (from script variable or preset)")
    print()

    if port is None:
        print("No serial port provided (CLI or PORT variable). Exiting.")
        return 1

    try:
        ser = serial.Serial(port, baudrate=baud, timeout=0.1)
    except Exception as e:
        print("Failed to open serial port:", e)
        return 2

    # Variablen für Parameter 
    detected_flux_linkage = None
    detected_resistance = None
    detected_inductance = None

    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        # 0) Dummykonfiguration auf mcu laden
        print("Dummykonfiguration wird geladen...")
        dummy_mc_hex = motorconfig_for_setup_wizard(poles, voltage)
        try:
            dummy_mc_bytes = bytes.fromhex(dummy_mc_hex)
            if len(dummy_mc_bytes) > 8:
                payload_struct = dummy_mc_bytes[4:-4]
                packet = bytes([COMM_SET_MCCONF]) + payload_struct
                frame_and_send(ser, packet)
                print("Dummykonfiguration gesendet.")
                time.sleep(0.5)
            else:
                print("Error: Die Dummykonfiguration ist zu kurz, um gesendet zu werden.")
        except Exception as e:
            print(f"Fehler beim Laden der Dummykonfiguration: {e}")

        ser.reset_input_buffer() 

        # 1) Parameterdetektiion starten
        payload = build_detect_payload(detect_can, max_power_loss,
                                       min_current_in, max_current_in,
                                       openloop_rpm, sl_erpm)
        print("Der motor bewegt sich für die Parameterdetektion. Bitte warten...")
        frame_and_send(ser, payload)

        print("Warten auf das Ergebnis der Parameterdetektion (dies kann bis zu ~1 Minute dauern)...")
        pkt = read_framed_packet(ser, timeout_s=timeout)
        if pkt is None:
            print("Keine Antwort vom VESC auf den Detektionsbefehl (Timeout).")
            return 3

        if len(pkt) >= 1 and pkt[0] == COMM_DETECT_APPLY_ALL_FOC:
            if len(pkt) >= 3:
                res_code = struct.unpack(">h", pkt[1:3])[0]
                print("Parameterdetektion abgeschlossen mit Ergebniscode:", res_code)
                if res_code < 0:
                    print("Die Parameterdetektion war fehlerhaft (negatives Ergebnis).")
            else:
                print("Detektionspaket nicht im erwarteten Format --> dennoch fortfahren")
        else:
            print(pkt)
            print("Empfangenes Paket entspricht nicht der erwarteten Detektionsantwort. Fortfahren mit mcconf-Anfrage.")

        # 2) generierte Motorkonfiguration vom Vesc anfragen
        print("\nAnfrage der generierten Motorkonfiguration vom VESC (COMM_GET_MCCONF)...")
        send_get_mcconf(ser)

        mcconf_payload = None
        native_msg = None
        start = time.time()
        while time.time() - start < 10.0:
            pkt = read_framed_packet(ser, timeout_s=1.0)
            if pkt is None:
                continue
            if len(pkt) >= 1 and pkt[0] == COMM_GET_MCCONF:
                mcconf_payload = pkt[1:]
                break
            else:
                msg = try_decode_pyvesc(pkt)
                if msg is not None:
                    if msg.__class__.__name__.lower().find("mcconf") != -1 or hasattr(msg, "foc_motor_r"):
                        native_msg = msg
                        break

        out_dir = Path("mcconf_out")
        if mcconf_payload:
            # das erhaltene Payload enthölt nur die reine Motorkonfiguration. Der Header und Trailer werden noch hinzugefügt
            LEADING_BYTES = bytes.fromhex("0301de0e")
            TRAILING_BYTES = bytes.fromhex("0056ca03")
            full_mcconf = LEADING_BYTES + mcconf_payload + TRAILING_BYTES

            # Parameter manuell aus Motorconfig auslesen
            try:
                # 
                full_hex = full_mcconf.hex()
                print(f"Full mcconf hex: {full_hex[:64]}...")
                
                # bytes der Flussverkettung 346 -> 354
                if len(full_hex) >= 354:
                    flux_hex_str = full_hex[346:354]
                    flux_bytes = bytes.fromhex(flux_hex_str)
                    detected_flux_linkage = struct.unpack('>f', flux_bytes)[0]
                    print(f"Manuelle Hex-Extraktion (Offset 346): Flussverkettung = {detected_flux_linkage}")
                    
                    # bytes des Widerstands Offset 338
                    r_hex_str = full_hex[338:346]
                    detected_resistance = struct.unpack('>f', bytes.fromhex(r_hex_str))[0]
                    print(f"Manuelle Hex-Extraktion (Offset 338): Widerstand = {detected_resistance}")
                    
                    # bytes der Induktion -> Offset 322
                    l_hex_str = full_hex[322:330]
                    detected_inductance = struct.unpack('>f', bytes.fromhex(l_hex_str))[0]
                    print(f"Manuelle Hex-Extraktion (Offset 322): Induktivität = {detected_inductance}")
                    
                else:
                    print(f"Config string too short for manual extraction (len={len(full_hex)})")
            except Exception as e:
                print(f"Error manually extracting flux linkage: {e}")
            # -------------------------------------

            binfile, hexfile = save_mcconf_bytes(full_mcconf, out_dir)
            print(f"Saved full mcconf (with header/trailer) to: {binfile} and {hexfile}")
            print("Final mcconf hex (first/last bytes shown):")
            print(full_mcconf.hex()[:64] + "..." + full_mcconf.hex()[-64:])

        elif native_msg is not None:
            # We have a decoded native message object, try patching it
            # Extract Flux Linkage from native msg if not yet found
            if detected_flux_linkage is None:
                if hasattr(native_msg, "foc_motor_flux_linkage"):
                    detected_flux_linkage = native_msg.foc_motor_flux_linkage
                elif hasattr(native_msg, "l_flux_linkage"):
                    detected_flux_linkage = native_msg.l_flux_linkage
            
            # Extract Resistance/Inductance from native_msg
            if hasattr(native_msg, "foc_motor_r"):
                detected_resistance = native_msg.foc_motor_r
            if hasattr(native_msg, "foc_motor_l"):
                detected_inductance = native_msg.foc_motor_l
                
            if hasattr(native_msg, "si_motor_poles"):
                print("Patching decoded message object with new pole count and sending back...")
                try:
                    native_msg.si_motor_poles = int(poles)
                    if SetMotorConfig is not None:
                        framed = codec.frame(bytes([COMM_SET_MCCONF]) + native_msg.serialize())
                        ser.write(framed)
                        print("Sent updated mcconf (COMM_SET_MCCONF) to VESC.")
                    else:
                        raw = getattr(native_msg, "to_bytes", None) or getattr(native_msg, "serialize", None)
                        if raw:
                            ser.write(codec.frame(bytes([COMM_SET_MCCONF]) + raw()))
                            print("Sent updated mcconf to VESC (fallback).")
                        else:
                            print("Cannot serialize decoded native message; aborting automatic upload.")
                except Exception as e:
                    print("Failed to send updated mcconf:", e)
            else:
                print("Decoded object lacks si_motor_poles; cannot set poles automatically.")
        else:
            print("Could not obtain mcconf from VESC. Ensure detection succeeded and try again.")

    finally:
            ser.close()


    # Drehmomentkonstante K_t berechnen, dass für die Lastprüfung der Bremsstrom abgeschätzt werden kann.
    print("\n--- K_t Calculation ---")
    results = {"K_V": 0.0, "K_t": 0.0}
    
    if detected_flux_linkage is not None:
        pole_pairs = poles / 2.0
        K_t = 1.5 * pole_pairs * detected_flux_linkage
        K_t = 2* K_t *1.5  # warum auch immer hier Faktor 2 eingesetzt werden muss, sonst Drehmomentkonstante nicht korrekt; die 1.5 sind als Sicherheitsfaktor zu verstehen, dass Drehmoment nicht überschritten wird.
        
        if K_t > 0.0001:
            K_V = 60.0 / (2.0 * math.pi * K_t)
        else:
            K_V = 0.0

        print(f"Detected Flux Linkage: {detected_flux_linkage:.6f} Wb")
        print(f"Calculated K_t = {pole_pairs} * {detected_flux_linkage:.6f} = {K_t:.4f} Nm/A")
        print(f"Calculated K_V (derived) = {K_V:.2f} RPM/V")
        
        results["K_t"] = round(K_t, 4)
        results["K_V"] = round(K_V, 2)
        results["Flux_Linkage"] = round(detected_flux_linkage*1000, 6)
    else:
        print("Warnung: Flussverkettung konnte nicht erkannt werden. K_t und K_V können nicht berechnet werden.")

    # Widerstand und Induktivität zu Ergebnissen hinufügen
    results["Phase_Resistance"] = detected_resistance*1000 if detected_resistance is not None else 0.0
    results["Inductance"] = detected_inductance*1000000 if detected_inductance is not None else 0.0

    # Save to Motordaten_detektion_ergebnis.json so gui_launcher can pick it up
    try:
        _out_dir = Path(sys.executable).resolve().parent / "results" if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent / "results"
        _out_dir.mkdir(exist_ok=True)
        with open(_out_dir / "Motordaten_detektion_ergebnis.json", "w") as f:
            json.dump(results, f, indent=4)
        print("K_t in Motordaten_detektion_ergebnis.json gespeichert")
    except Exception as e:
        print(f"Fehler beim Speichern der Ergebnisse: {e}")

    print("\nFertig. Wenn das Skript die Polzahl nicht automatisch setzen konnte, können Sie:")
    print("  - Öffnen Sie mcconf_out/mcconf.hex und verwenden Sie VESC-Tool oder VESC-Testbench's mcconf_set.py Helfer, um es hochzuladen.")
    print("  - Oder führen Sie es mit einem anderen Preset erneut aus und überprüfen Sie die Protokolle.")
    return 0


def parse_args():
    ap = argparse.ArgumentParser(description="VESC FOC Setup Wizard laufen lassen")
    # single positional motortype argument; parameters are set from MOTORTYPE_PRESETS and fixed defaults.
    ap.add_argument("motortype", choices=list(MOTORTYPE_PRESETS.keys()),
                    help="Wählen Sie den Motortyp/Preset aus, der verwendet werden soll (Parameter werden automatisch angewendet).")
    ap.add_argument("--poles", type=int, required=True, help="Anzahl der Motorpole (erforderlich).")
    ap.add_argument("--voltage", type=float, required=True, help="Systemspannung für die Konfigurationserstellung.")
    return ap.parse_args()


if __name__ == "__main__":
    args = parse_args()
    # derive custom parameters from the selected motortype preset
    mt = MOTORTYPE_PRESETS.get(args.motortype)
    if mt is None:
        raise SystemExit(f"Unbekannter Motortyp: {args.motortype}")
    custom_max_loss = mt["max_power_loss"]
    openloop_rpm = mt["openloop_rpm"]*args.poles # erpm auf rpm skalieren

    rc = run_wizard(
        port=PORT,
        baud=BAUD,
        size_preset=args.motortype,
        custom_max_loss=custom_max_loss,
        custom_i_in_max=MAX_INPUT_CURRENT,
        detect_can=DETECT_CAN,
        openloop_rpm=openloop_rpm,
        sl_erpm=SL_ERPM,
        poles=args.poles,
        timeout=TIMEOUT,
        voltage=args.voltage
    )

    sys.exit(rc)