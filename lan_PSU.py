# lan_PSU.py
# Control script for ITECH IT6005C-80-150 Regenerative Power Supply via LAN (SCPI)

import pyvisa
import time

class IT6005C:
    """
    Controller for ITECH IT6005C-80-150 Power Supply via LAN (TCP/IP).
    Uses SCPI commands over a raw socket connection.
    """
    
    def __init__(self, ip_address: str, port: int = 30000):
        """
        Initialize connection to the PSU.
        
        Args:
            ip_address: IP address of the PSU (e.g., "192.168.1.100")
            port: SCPI port (default 30000 for ITECH instruments)
        """
        self.ip = ip_address
        self.port = port
        self.rm = pyvisa.ResourceManager('@py')  # Use pyvisa-py backend
        self.resource_string = f"TCPIP::{ip_address}::{port}::SOCKET"
        self.inst = None
        
    def connect(self):
        """Open connection to the PSU."""
        try:
            self.inst = self.rm.open_resource(self.resource_string)
            self.inst.read_termination = '\n'
            self.inst.write_termination = '\n'
            self.inst.timeout = 5000  # 5 seconds timeout
            print(f"Connected to PSU at {self.ip}:{self.port}")
            print(self.rm.list_resources())
            
            # Clear status and query identification
            self.write("*CLS")
            idn = self.query("*IDN?")
            print(f"Device: {idn}")
            return True
        except Exception as e:
            print(f"Connection failed: {e}")
            return False
    
    def disconnect(self):
        """Close connection to the PSU."""
        if self.inst:
            try:
                # Ensure output is off when disconnecting (safety)
                self.output_off()
            except:
                pass
            self.inst.close()
            print("Disconnected from PSU.")
    
    def write(self, command: str):
        """Send a command to the PSU."""
        if self.inst:
            self.inst.write(command)
            time.sleep(0.05) # Small delay to ensure processing
    
    def query(self, command: str) -> str:
        """Send a query and return the response."""
        if self.inst:
            return self.inst.query(command).strip()
        return ""
    
    # --- Output Control ---
    
    def output_on(self):
        """Turn the output ON."""
        self.write("OUTP ON")
        print("Output: ON")
    
    def output_off(self):
        """Turn the output OFF."""
        self.write("OUTP OFF")
        print("Output: OFF")
    
    def get_output_state(self) -> bool:
        """Query if output is ON or OFF."""
        response = self.query("OUTP?")
        return response == "1" or response.upper() == "ON"
    
    # --- Voltage Settings ---
    
    def set_voltage(self, voltage: float):
        """
        Set the output voltage level.
        
        Args:
            voltage: Voltage in Volts (0 to 80V for IT6005C-80-150)
        """
        self.write(f"VOLT {voltage}")
        print(f"Voltage set to: {voltage} V")
    
    def get_voltage_setpoint(self) -> float:
        """Get the voltage setpoint."""
        return float(self.query("VOLT?"))
    
    def get_voltage_measured(self) -> float:
        """Get the actual measured output voltage."""
        return float(self.query("MEAS:VOLT?"))
    
    # --- Current Settings ---
    
    def set_current_limit(self, current: float):
        """
        Set the output current limit (upper limit, source mode).
        
        Args:
            current: Current in Amps (0 to 150A for IT6005C-80-150)
        """
        self.write(f"CURR:LIM {current}")
        print(f"Current limit set to: {current} A")
    
    def get_current_setpoint(self) -> float:
        """Get the current limit setpoint."""
        return float(self.query("CURR?"))
    
    def get_current_measured(self) -> float:
        """Get the actual measured output current."""
        return float(self.query("MEAS:CURR?"))
    
    # --- Current Protection (OCP) ---
    
    def set_ocp_level(self, current: float):
        """
        Set the Over-Current Protection level.
        
        Args:
            current: OCP threshold in Amps
        """
        self.write(f"CURR:PROT {current}")
        print(f"OCP level set to: {current} A")
    
    def enable_ocp(self, enable: bool = True):
        """Enable or disable Over-Current Protection."""
        state = "ON" if enable else "OFF"
        self.write(f"CURR:PROT:STAT {state}")
        print(f"OCP: {state}")
    
    # --- Sink Mode (Negative Current / Regenerative) ---
    
    def set_sink_current_limit(self, current: float):
        """
        Set the sink (regenerative) current limit.
        This is the "lower" or negative current limit.
        
        Args:
            current: Sink current limit in Amps (needs to be negative value, example: -50A max sink)
        """
        self.write(f"CURR:LIM:NEG {current}")
        print(f"Sink current limit set to: {current} A")
    
    # --- Power Settings ---
    
    def set_power_limit(self, power: float):
        """
        Set the output power limit.
        
        Args:
            power: Power in Watts (0 to 5000W for IT6005C)
        """
        self.write(f"POW {power}")
        print(f"Power limit set to: {power} W")
    
    def get_power_measured(self) -> float:
        """Get the actual measured output power."""
        return float(self.query("MEAS:POW?"))
    
    # --- Utility ---
    
    def reset(self):
        """Reset the instrument to default settings."""
        self.write("*RST")
        print("PSU reset to defaults.")
    
    def clear_errors(self):
        """Clear any error flags."""
        self.write("*CLS")
    
    def get_errors(self) -> str:
        """Query the error queue."""
        return self.query("SYST:ERR?")
    
    def __enter__(self):
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()


# --- Example Usage ---
if __name__ == "__main__":
    # PSU IP addresses
    PSU1_IP = "192.168.200.100"
    PSU2_IP = "192.168.200.101"
    
    # Dummy values for testing code
    VOLTAGE = 48.0          # V
    CURRENT_LIMIT = 5.0    # A (source)
    SINK_CURRENT = -5.0      # A (regenerative)
    STARTUP_DELAY = 2.0     # seconds between PSU1 and PSU2 turning on
    
    # Create PSU objects
    psu1 = IT6005C(PSU1_IP)
    psu2 = IT6005C(PSU2_IP)
    
    try:
        # Connect to both PSUs
        print("--- Connecting to PSUs ---")
        psu1.connect()
        psu2.connect()
        
        # Configure both PSUs with same settings
        print("\n--- Configuring PSU 1 ---")
        psu1.set_voltage(VOLTAGE)
        psu1.set_current_limit(CURRENT_LIMIT)
        psu1.set_sink_current_limit(SINK_CURRENT)
        
        print("\n--- Configuring PSU 2 ---")
        psu2.set_voltage(VOLTAGE)
        psu2.set_current_limit(CURRENT_LIMIT)
        psu2.set_sink_current_limit(SINK_CURRENT)
        
        # Turn ON with staggered start
        print("\n--- Startup Sequence ---")
        print("Turning ON PSU 1...")
        psu1.output_on()
        
        print(f"Waiting {STARTUP_DELAY} seconds before starting PSU 2...")
        time.sleep(STARTUP_DELAY)
        
        print("Turning ON PSU 2...")
        psu2.output_on()
        
        print(len(psu1.rm.list_resources()))
        # Wait and measure
        print("\n--- Running ---")
        time.sleep(1)  # Let them run for 1 seconds
        

        print("\n--- Shutdown Sequence ---")
        psu1.output_off()
        psu2.output_off()
        
    finally:
        # Always disconnect cleanly
        print("\n--- Disconnecting ---")
        psu1.disconnect()
        psu2.disconnect()
