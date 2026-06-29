# OscilloscopeStream.py
from __future__ import print_function
import time
import os
import sys
import libtiepie
import traceback

class OscilloscopeDM:
    def __init__(self):
        print("Initializing Oscilloscope (DM Mode)...")
        # Oszilloskop ist per USB angeschlossen -> Netzwerk-Suche deaktivieren
        libtiepie.network.auto_detect_enabled = False
        
        self.scp = None
        for attempt in range(3):
            libtiepie.device_list.update()
            print(f"Versuch {attempt+1}: {libtiepie.device_list} Gerät(e) erkannt.")
            for item in libtiepie.device_list:
                time.sleep(0.1)  # Kurze Pause --> Vermeidet Probleme bei schnellen Erkennungsversuchen
                if item.can_open(libtiepie.DEVICETYPE_OSCILLOSCOPE):
                    self.scp = item.open_oscilloscope()
                    if self.scp.measure_modes & libtiepie.MM_BLOCK:
                        break
                    else:
                        self.scp = None
            
            if self.scp:
                break
            
            if attempt < 2:
                print(f"Oscilloscope nicht erkannt. Erneuter Versuch... ({attempt+1}/3)")
                time.sleep(1.0)
        
        if not self.scp:
            raise Exception("Kein Oszilloskop im Blockmodus nach 3 Versuchen gefunden!")
            
        # Grundeinstellungen Oszilloskop
        self.scp.measure_mode = libtiepie.MM_BLOCK
        self.scp.sample_rate = 1e5  # 100 kHz
        self.scp.record_length = 10000  # Increased to 10000 samples for stability
        self.scp.pre_sample_ratio = 0.0
        # No trigger timeout set in original file, but good practice to have one if we want to avoid hanging
        # However, original file didn't set it. Let's set a default just in case.
        self.scp.trigger.timeout = 0.8
        
        # Disable triggers by default
        for ch in self.scp.channels:
            ch.trigger.enabled = True
        
        self.last_average = None

    # Changed: add optional v_range parameter (voltage range in V)
    def measure(self, filename='OscilloscopeStream_dm.csv', v_range=80.0):
        try:
            scp = self.scp
            
            # Channel Configuration (Only Ch4 enabled)
            # Disable all channels first
            for ch in scp.channels:
                ch.enabled = False
            
            # Enable only Channel 4
            if len(scp.channels) > 3:
                scp.channels[3].enabled = True
                # Use provided v_range or default to 80.0 V
                scp.channels[3].range = float(v_range) if v_range is not None else 80.0
                scp.channels[3].coupling = libtiepie.CK_DCV
            else:
                print("Error: Channel 4 not found")
                return

            # Set short trigger timeout for immediate capture
            #scp.trigger.timeout = 0.1 
            
            # Start Measurement
            start_time = time.time()
            scp.start()

            # Force trigger immediately
            try:
                scp.force_trigger()
            except Exception:
                pass  # Some scopes may not support force trigger
            
            while not scp.is_data_ready:
                time.sleep(0.001)  # Avoid busy-waiting
                if scp.is_data_overflow:
                    print('Data overflow!')
                    break
                if (time.time() - start_time) > 2.0:
                    print("Timeout waiting for data ready")
                    break
            
            duration = time.time() - start_time

            if scp.is_data_ready:
                data = scp.get_data()
                # data contains only enabled channels. data[0] is Ch4.
                
                if not data or len(data) == 0 or len(data[3]) == 0:
                    print("DM Measurement: No data captured")
                    print(len(data[0]), len(data[1]), len(data[2]), len(data[3]))
                    return

                ch4_data = data[3]
                print(f'Messung abgeschlossen. {len(ch4_data)} Samples in {duration:.4f}s erfasst.')

                # Durchschnitt berechnen
                avg_val = sum(ch4_data) / len(ch4_data)
                self.last_average = avg_val
                #print(f"DM Channel 4 Average: {avg_val:.4f} V")

                # --- Write CSV ---
                with open(filename, 'w', buffering=1) as csv_file:
                    csv_file.write('Sample;Ch4' + os.linesep)
                    for i, val in enumerate(ch4_data):
                        csv_file.write(f"{i};{val}{os.linesep}")
                
                print('Data written to: ' + filename)

            else:
                print("Oscilloscope timeout or error.")

        except Exception as e:
            print(f"Oscilloscope Error: {e}")
            traceback.print_exc()

    def close(self):
        if self.scp:
            try:
                self.scp.stop()
            except Exception:
                pass
            del self.scp
            self.scp = None
            print("Oscilloscope (DM) closed.")

if __name__ == "__main__":
    try:
        oszi = OscilloscopeDM()
        oszi.measure()
    finally:
        if 'oszi' in locals() and oszi: oszi.close()