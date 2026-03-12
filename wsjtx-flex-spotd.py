#!/usr/bin/env python3
"""
WSJT-X to Flex Radio Spots Bridge - Daemon-friendly version
Version: 1.4-daemon  (2025-03-11)
  - Removed interactive prompts → moved to top-level config variables
  - Replaced print() with proper logging (stdout/stderr friendly)
  - Intended for daemon/systemd/supervisord/nohup usage
"""

import socket
import struct
import time
import binascii
import re
import logging
import argparse
import sys

# ────────────────────────────────────────────────
# CONFIGURATION - EDIT THESE VARIABLES
# ────────────────────────────────────────────────

MY_CALLSIGN     = "KK7GWY"              # Your callsign (red spots when called)
FILTER_MODE     = "cq"                  # Options: "cq", "pota", "none"
SPOT_LIFETIME   = 120                   # seconds – also duplicate/refresh window
MIN_SNR         = -35                   # Minimum SNR for non-personal spots
COMMENT_TS      = True                  # Include time in spot comment
LOG_LEVEL       = logging.INFO         # DEBUG, INFO, WARNING, ERROR, CRITICAL

# Flex connection settings
FLEX_IP         = "192.168.50.121"
FLEX_PORT       = 4992

# WSJT-X UDP multicast
MCAST_GRP       = "224.0.0.1"
MCAST_PORT      = 2237

# Colors (hex)
COLOR_PERSONAL  = "#FF0000"             # Red – when someone calls you
COLOR_POTA      = "#00FF00"             # Green – CQ POTA spots

# ────────────────────────────────────────────────
# Logging setup (daemon-friendly)
# ────────────────────────────────────────────────
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────
# GLOBALS / STATE
# ────────────────────────────────────────────────
dial_freq       = 0
current_mode    = "FT8"
sent_spots      = {}                    # (callsign, freq_rounded) → last_sent_time
cmd_seq         = 0
flex_socket     = None

# ────────────────────────────────────────────────
# QString parser (schema 2: byte length + UTF-8)
# ────────────────────────────────────────────────
def parse_qstring(data, offset, buf_len):
    if offset + 4 > buf_len:
        return "[short]", offset
    length = struct.unpack_from('>I', data, offset)[0]
    offset += 4
    if length == 0xFFFFFFFF:
        return '', offset
    if offset + length > buf_len:
        return f"[trunc len={length}]", buf_len
    str_bytes = data[offset:offset + length]
    offset += length
    try:
        return str_bytes.decode('utf-8').rstrip('\x00'), offset
    except UnicodeDecodeError:
        return f"[bad utf8 len={length}]", offset

# ────────────────────────────────────────────────
# Persistent Flex connection
# ────────────────────────────────────────────────
def get_flex_socket():
    global flex_socket, cmd_seq
    if flex_socket is not None:
        try:
            flex_socket.send(b'')
            return flex_socket
        except Exception:
            logger.warning("Flex socket connection lost, reconnecting...")
            flex_socket.close()
            flex_socket = None

    logger.info(f"Connecting to Flex {FLEX_IP}:{FLEX_PORT} ...")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect((FLEX_IP, FLEX_PORT))
        welcome = s.recv(1024).decode('ascii', 'ignore').strip()
        logger.info(f"Connected: {welcome}")

        cmd_seq = 0
        bind_cmd = f'C{cmd_seq}|client program "WSJTX-Spotter"\n'
        s.sendall(bind_cmd.encode())
        s.recv(1024)  # drain
        flex_socket = s
        return s
    except Exception as e:
        logger.error(f"Connection failed: {e}")
        return None

# ────────────────────────────────────────────────
# Send spot with time-based deduplication
# ────────────────────────────────────────────────
def send_flex_spot(callsign, freq_mhz, mode, comment, color=None):
    global cmd_seq
    now = time.time()
    freq_key = round(freq_mhz, 5)  # 5 decimals to tolerate small variance
    key = (callsign.upper(), freq_key)

    if key in sent_spots:
        last_sent = sent_spots[key]
        if now - last_sent < SPOT_LIFETIME:
            logger.info(f"Duplicate skipped: {callsign} @ {freq_mhz:.6f}")
            return

    s = get_flex_socket()
    if s is None:
        return

    try:
        cmd_seq += 1
        cmd_parts = [
            f'C{cmd_seq}|spot add',
            f'rx_freq={freq_mhz:.6f}',
            f'callsign={callsign}',
            f'mode={mode}',
            f'source=WSJTX',
            f'comment={comment}',
            f'lifetime_seconds={SPOT_LIFETIME}'
        ]
        if color:
            cmd_parts.append(f'color={color}')

        spot_cmd = ' '.join(cmd_parts) + '\n'

        s.sendall(spot_cmd.encode())
        resp = s.recv(4096).decode('ascii', 'ignore').strip()

        if 'R' in resp and '|0|' in resp:
            col_str = f" ({color})" if color else ""
            label = "CALLING YOU" if color == COLOR_PERSONAL else "Accepted"
            logger.info(f"{label}: {callsign} @ {freq_mhz:.6f}{col_str}")
            sent_spots[key] = now
        elif 'R' in resp:
            sent_spots[key] = now
        else:
            logger.warning(f"Unexpected Flex response: {resp[:200]}...")

    except Exception as e:
        logger.error(f"Send failed: {e}")
        global flex_socket
        if flex_socket:
            flex_socket.close()
            flex_socket = None

# ────────────────────────────────────────────────
# Parse WSJT-X message
# ────────────────────────────────────────────────
def parse_wsjtx_message(data):
    global dial_freq, current_mode
    buf_len = len(data)
    if buf_len < 20:
        return None

    offset = 0
    magic = struct.unpack_from('>I', data, offset)[0]; offset += 4
    if magic != 0xADBCCBDA:
        return None

    schema = struct.unpack_from('>I', data, offset)[0]; offset += 4
    msg_type = struct.unpack_from('>I', data, offset)[0]; offset += 4

    id_str, offset = parse_qstring(data, offset, buf_len)

    if msg_type == 1:  # Status
        dial_raw = struct.unpack_from('>Q', data, offset)[0]; offset += 8
        mode_str, offset = parse_qstring(data, offset, buf_len)
        dial_freq = dial_raw
        if mode_str and mode_str != '~':
            current_mode = mode_str.upper().strip()
        logger.debug(f"Status: dial={dial_freq/1e6:.6f} MHz  mode={current_mode}")
        return {'type': 'status'}

    elif msg_type == 2:  # Decode
        is_new   = struct.unpack_from('>?', data, offset)[0]; offset += 1
        time_ms  = struct.unpack_from('>I',  data, offset)[0]; offset += 4
        snr      = struct.unpack_from('>i',  data, offset)[0]; offset += 4
        dt       = struct.unpack_from('>d',  data, offset)[0]; offset += 8
        df       = struct.unpack_from('>I',  data, offset)[0]; offset += 4

        mode_str, offset = parse_qstring(data, offset, buf_len)
        message,  offset = parse_qstring(data, offset, buf_len)

        mode_to_use = mode_str.upper().strip() if mode_str and mode_str != '~' else current_mode

        parts = re.split(r'\s+', message.strip())
        callsign = None
        modifier_list = ['POTA', 'SOTA', 'DX', 'NA']

        if len(parts) >= 3:
            if parts[0].upper() == 'CQ':
                if len(parts) >= 4 and parts[1].upper() in modifier_list:
                    callsign = parts[2]
                else:
                    callsign = parts[1]
            else:
                callsign = parts[1] if re.match(r'^[A-Z0-9/]{3,15}$', parts[1]) else None
        if not callsign and len(parts) >= 1:
            callsign = parts[0] if re.match(r'^[A-Z0-9/]{3,15}$', parts[0]) else None

        freq_mhz = (dial_freq + df) / 1e6 if dial_freq > 0 else 0.0

        # Priority 1: Calling YOU → force red, bypass filter
        is_calling_me = False
        if MY_CALLSIGN:
            my_upper = MY_CALLSIGN.upper()
            if len(parts) >= 2 and my_upper == parts[1].upper():
                is_calling_me = True
            elif my_upper in [p.upper() for p in parts]:
                is_calling_me = True

        if is_calling_me:
            ts = time.strftime('%H:%M') if COMMENT_TS else ''
            comment = f"{message} SNR {snr:+d} {ts}".strip()
            logger.info(f"Detected call to {MY_CALLSIGN} from {callsign}")
            return {
                'type': 'decode',
                'callsign': callsign,
                'freq': freq_mhz,
                'mode': mode_to_use,
                'comment': comment,
                'color': COLOR_PERSONAL
            }

        # Normal path
        if snr < MIN_SNR:
            return None

        msg_upper = message.upper()
        if FILTER_MODE == 'cq' and not msg_upper.startswith('CQ '):
            return None
        elif FILTER_MODE == 'pota' and 'CQ POTA' not in msg_upper:
            return None

        if callsign and len(callsign) >= 4 and 1 < freq_mhz < 1000:
            ts = time.strftime('%H:%M') if COMMENT_TS else ''
            comment = f"{message} SNR {snr:+d} {ts}".strip()

            color = COLOR_POTA if 'CQ POTA' in msg_upper else None

            return {
                'type': 'decode',
                'callsign': callsign,
                'freq': freq_mhz,
                'mode': mode_to_use,
                'comment': comment,
                'color': color
            }

    return None

# ────────────────────────────────────────────────
# Main daemon loop
# ────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="WSJT-X to Flex Spots Bridge")
    parser.add_argument('--version', action='version', version='1.4-daemon')
    args = parser.parse_args()

    logger.info("Starting WSJT-X → Flex Spots Bridge (daemon mode)")
    logger.info(f"Config: mycall={MY_CALLSIGN}, filter={FILTER_MODE}, lifetime={SPOT_LIFETIME}s")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', MCAST_PORT))
    mreq = struct.pack("4sl", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    logger.info(f"Listening on multicast {MCAST_GRP}:{MCAST_PORT}")

    while True:
        try:
            data, addr = sock.recvfrom(1500)
            parsed = parse_wsjtx_message(data)
            if parsed and parsed['type'] == 'decode':
                send_flex_spot(
                    parsed['callsign'],
                    parsed['freq'],
                    parsed['mode'],
                    parsed['comment'],
                    color=parsed.get('color')
                )
        except KeyboardInterrupt:
            logger.info("Received Ctrl+C → shutting down")
            break
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            time.sleep(1)

    if flex_socket:
        flex_socket.close()
        logger.info("Flex connection closed")

if __name__ == "__main__":
    main()
