"""Read-only Modbus RTU access to the meters.

Only function codes 03 and 04 (read registers) are used. There is
deliberately no write path anywhere in the gateway.
"""
import logging
import struct
import threading
import time

from registers import TYPE_WORDS, get_map, plan_blocks

log = logging.getLogger("modbus")


def decode(words, typ, word_order="big"):
    """Decode 16-bit register words into a Python number."""
    if typ == "uint16":
        return words[0]
    if typ == "int16":
        return struct.unpack(">h", struct.pack(">H", words[0]))[0]
    hi, lo = (words[0], words[1]) if word_order == "big" else (words[1], words[0])
    raw = struct.pack(">HH", hi, lo)
    if typ == "float32":
        return struct.unpack(">f", raw)[0]
    if typ == "uint32":
        return struct.unpack(">I", raw)[0]
    if typ == "int32":
        return struct.unpack(">i", raw)[0]
    raise ValueError(f"unknown type {typ}")


def extract(block_start, words, entries, word_order="big"):
    out = {}
    for name, addr, _fc, typ, scale, _g in entries:
        off = addr - block_start
        val = decode(words[off:off + TYPE_WORDS[typ]], typ, word_order)
        if val != val:          # NaN guard
            continue
        out[name] = round(val * scale, 4)
    return out


class ModbusBus:
    """One physical RS485 bus (one USB adapter). Thread-safe."""

    def __init__(self, name, port, baudrate=9600, parity="N", stopbits=1, bytesize=8,
                 timeout=0.5, retries=2, max_block=60, inter_frame_s=0.02):
        from pymodbus.client import ModbusSerialClient  # imported lazily: not needed in simulator mode

        self.name = name
        self.port = port
        self.retries = retries
        self.max_block = max_block
        self.inter_frame_s = inter_frame_s
        self.lock = threading.Lock()
        self.client = ModbusSerialClient(port=port, baudrate=baudrate, parity=parity,
                                         stopbits=stopbits, bytesize=bytesize, timeout=timeout,
                                         retries=0)
        self._plans = {}

    def connect(self):
        ok = self.client.connect()
        log.info("bus %s on %s connect=%s", self.name, self.port, ok)
        return ok

    def _read(self, fc, start, count, unit):
        fn = self.client.read_holding_registers if fc == 3 else self.client.read_input_registers
        # pymodbus renamed 'slave' to 'device_id' in 3.10; support both.
        try:
            rr = fn(start, count=count, device_id=unit)
        except TypeError:
            rr = fn(start, count=count, slave=unit)
        if rr.isError():
            raise IOError(f"modbus error {rr}")
        return rr.registers

    def read_group(self, device, group):
        """Read one register group from a device. Returns a dict or raises IOError."""
        key = (device["model"], group)
        if key not in self._plans:
            self._plans[key] = plan_blocks(get_map(device["model"]), group, self.max_block)
        word_order = device.get("word_order", "big")
        values = {}
        with self.lock:
            for fc, start, count, entries in self._plans[key]:
                last_err = None
                for attempt in range(self.retries + 1):
                    try:
                        words = self._read(fc, start, count, device["unit"])
                        values.update(extract(start, words, entries, word_order))
                        last_err = None
                        break
                    except Exception as exc:  # serial timeouts, CRC errors, exceptions
                        last_err = exc
                        time.sleep(0.05 * (attempt + 1))
                if last_err is not None:
                    raise IOError(f"{device['id']} unit {device['unit']}: {last_err}")
                time.sleep(self.inter_frame_s)
        return values

    def close(self):
        self.client.close()
