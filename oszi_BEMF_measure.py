# OscilloscopeStream.py
from __future__ import print_function
import time
import os
import sys
import libtiepie
import traceback

class OscilloscopeBEMF:
    def __init__(self, e_rpm=1000):
        print("Initializing Oscilloscope...")
        # Disable network search to speed up init if using USB
        libtiepie.network.auto_detect_enabled = False 
        libtiepie.device_list.update()

        self.scp = None
        for item in libtiepie.device_list:
            if item.can_open(libtiepie.DEVICETYPE_OSCILLOSCOPE):
                self.scp = item.open_oscilloscope()
                if self.scp.measure_modes & libtiepie.MM_BLOCK:
                    break
                else:
                    self.scp = None
        
        if not self.scp:
            raise Exception("No block-mode oscilloscope found!")
            
        # Configure global settings once
        self.scp.measure_mode = libtiepie.MM_BLOCK
        self.scp.sample_rate = 1e5  # 100 kHz
        
        # Calculate record duration for 10 electrical rotations (peaks)
        if e_rpm <= 0:
            e_rpm = 1000 # fallback to prevent division by zero
        f_el = e_rpm / 60.0
        t_rec = 10.0 / f_el
        self.scp.record_length = int(t_rec * self.scp.sample_rate)
        
        self.scp.pre_sample_ratio = 0.0
        self.scp.trigger.timeout = 0.5
        
        # Disable triggers by default
        for ch in self.scp.channels:
            ch.trigger.enabled = False

    def measure(self, filename='OscilloscopeStream.csv'):
        try:
            scp = self.scp
            
            # Channel Configuration
            measure_flags = [True, True, True, False] # Ch1, Ch2, Ch3, Ch4
            
            enabled_indices = []
            for i, ch in enumerate(scp.channels):
                should_enable = measure_flags[i] if i < len(measure_flags) else False
                ch.enabled = should_enable
                if should_enable:
                    enabled_indices.append(i)
                    ch.range = 80.0
                    ch.coupling = libtiepie.CK_DCV

            # Trigger Config (Zero Crossing on Ch1)
            if len(scp.channels) > 0:
                ch1 = scp.channels[0]
                ch1.trigger.enabled = True
                ch1.trigger.kind = libtiepie.TK_RISINGEDGE
                ch1.trigger.levels[0] = 0.5
                ch1.trigger.hystereses[0] = 0.03

            # Start Measurement
            print('Starting Oscilloscope acquisition...')
            start_time = time.time()
            scp.start()

            while not scp.is_data_ready:
                time.sleep(0.001)  # Fast poll
                if scp.is_data_overflow:
                    print('Data overflow!')
                    break
                if (time.time() - start_time) > (self.scp.trigger.timeout + 2.0):
                    print("Timeout waiting for oscilloscope data ready")
                    break
            
            duration = time.time() - start_time

            if scp.is_data_ready:
                data = scp.get_data()
                print(f'Oscilloscope: Got {len(data[0])} samples in {duration:.4f}s.')

                # --- Alignment Logic ---
                level_abs = 0.0
                trigger_ch = 0
                idx_cross = None
                
                # Try to align if Ch1 is present
                if trigger_ch in enabled_indices:
                    y = data[enabled_indices.index(trigger_ch)]
                    # Simple search around expected trigger point
                    search_range = range(0, min(len(y)-1, 2000))
                    for i in search_range:
                        if y[i] < level_abs <= y[i + 1]:
                            idx_cross = i
                            break
                
                if idx_cross is not None:
                    # Sub-sample precision
                    dy = data[enabled_indices.index(trigger_ch)][idx_cross + 1] - data[enabled_indices.index(trigger_ch)][idx_cross]
                    frac = (level_abs - data[enabled_indices.index(trigger_ch)][idx_cross]) / dy if dy != 0 else 0.0
                    cut = idx_cross
                    data_aligned = [ch_data[cut:] for ch_data in data]
                    
                    # Calculate t0 for alignment info
                    t0 = (idx_cross + frac) / scp.sample_rate
                    print(f'Aligned to zero crossing at ~{t0:.6f} s (index {idx_cross}+{frac:.2f}).')
                else:
                    data_aligned = data
                    print('Warning: zero crossing near trigger not found; output unaligned.')

                # --- Write CSV ---
                with open(filename, 'w', buffering=1) as csv_file:
                    # Header
                    csv_file.write('Sample')
                    for idx in enabled_indices:
                        csv_file.write(';Ch' + str(idx + 1))
                    csv_file.write(os.linesep)

                    # Data
                    chan_lengths = [len(ch) for ch in data_aligned]
                    row_count = max(chan_lengths) if chan_lengths else 0
                    
                    for i in range(row_count):
                        csv_file.write(str(i))
                        for j in range(len(data_aligned)):
                            val = str(data_aligned[j][i]) if i < len(data_aligned[j]) else ""
                            csv_file.write(';' + val)
                        csv_file.write(os.linesep)
                
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
            print("Oscilloscope closed.")

if __name__ == "__main__":
    try:
        oszi = OscilloscopeBEMF()
        oszi.measure()
    finally:
        if 'oszi' in locals() and oszi: oszi.close()