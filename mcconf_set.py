import time
from pyvesc.messages.setters import SetMotorConfig
from pyvesc.messages.Vedder_BLDC_Commands import VedderCmd
from pyvesc.protocol.packet import codec
from pyvesc.protocol.interface import encode
from pyvesc.params.confgenerator import MCCONF_SIGNATURE

def _hex_to_bytes(hex_str: str) -> bytes:
    """Convert a permissive hex string to bytes."""
    if not hex_str:
        return b""
    cleaned = hex_str.replace("\n", " ").replace("\t", " ")
    parts = []
    for tok in cleaned.replace(",", " ").replace("_", " ").split():
        if tok.startswith("0x") or tok.startswith("0X"):
            tok = tok[2:]
        parts.append(tok)
    hex_compact = "".join(parts)
    if len(hex_compact) % 2 != 0:
        hex_compact = "0" + hex_compact
    return bytes.fromhex(hex_compact)

def _try_unframe(buf: bytes):
    try:
        payload, consumed = codec.unframe(buf)
        if payload is not None and consumed > 0:
            return payload, consumed
    except Exception:
        pass
    return None, 0

def _extract_mcconf(input_bytes: bytes) -> bytes:
    """Extract mcconf bytes from framed or unframed input."""
    if not input_bytes:
        return b""

    # Case A: framed (starts with 0x02 or 0x03)
    if input_bytes[0] in (0x02, 0x03): 
        payload, consumed = _try_unframe(input_bytes)
        if payload is None:
            payload = input_bytes
        
        # payload should start with command id
        if len(payload) >= 1 and payload[0] in (VedderCmd.COMM_GET_MCCONF, VedderCmd.COMM_SET_MCCONF):
            mc = payload[1:]
        else:
            mc = payload
    else:
        # Case B: unframed payload
        first = input_bytes[0]
        if first in (VedderCmd.COMM_GET_MCCONF, VedderCmd.COMM_SET_MCCONF):
            mc = input_bytes[1:]
        else:
            # Case C: raw mcconf
            sig = int.from_bytes(input_bytes[:4], 'big') if len(input_bytes) >= 4 else None
            if sig == MCCONF_SIGNATURE:
                mc = input_bytes
            else:
                # Fallback: assume raw mcconf
                mc = input_bytes

    return mc

def load_configuration(vesc, config_hex_string, motor_name="Motor", lock=None):
    """
    Parses the config string and uploads it to the VESC.
    Blocks until sent and a short delay has passed.
    """
    if not config_hex_string or config_hex_string == "ERROR":
        print(f"[{motor_name}] No configuration string provided (or ERROR). Skipping.")
        return

    print(f"[{motor_name}] Processing configuration string...")
    try:
        raw_bytes = _hex_to_bytes(config_hex_string)
        mcconf_bytes = _extract_mcconf(raw_bytes)
    except Exception as e:
        print(f"[{motor_name}] Error parsing configuration string: {e}")
        return

    if not mcconf_bytes:
        print(f"[{motor_name}] Failed to extract valid mcconf from string.")
        return

    if len(mcconf_bytes) >= 4:
        sig = int.from_bytes(mcconf_bytes[:4], 'big')
        sig_ok = sig == MCCONF_SIGNATURE
        print(f"[{motor_name}] Signature check: 0x{sig:08x} ({'OK' if sig_ok else 'UNEXPECTED'})")
    else:
        print(f"[{motor_name}] Warning: Config too short for signature check.")

    print(f"[{motor_name}] Uploading configuration ({len(mcconf_bytes)} bytes)...")
    
    # Create the message
    msg = SetMotorConfig(mcconf_bytes)
    packet = encode(msg)
    
    # Send
    if lock:
        with lock:
            _send_packet(vesc, packet)
    else:
        _send_packet(vesc, packet)
        
    print(f"[{motor_name}] Configuration upload complete.")

def _send_packet(vesc, packet):
    ser = vesc.serial_port
    # Flush input before sending to clear old data
    try:
        ser.reset_input_buffer()
    except:
        pass
    ser.write(packet)
    ser.flush()
    # Wait for VESC to process and write to flash (can take a moment)
    time.sleep(0.5)
    # Clear any response (ACK)
    try:
        if ser.in_waiting:
            ser.read(ser.in_waiting)
    except:
        pass
