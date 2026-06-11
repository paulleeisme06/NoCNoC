import os
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge

FIRMWARE_BIN = "firmware.bin"

def load_firmware():
    p = os.path.join(os.path.dirname(__file__), FIRMWARE_BIN)
    if os.path.exists(p):
        with open(p, "rb") as f:
            return list(f.read())
    return [0x13, 0x01, 0x00, 0x6f] + [0xFF] * 2044

FIRMWARE = load_firmware()

def get_expected():
    # Try cocotb dir first, then src
    for p in [
        os.path.join(os.path.dirname(__file__), FIRMWARE_BIN),
        'src/firmware.bin',
        'cocotb/firmware.bin'
    ]:
        if os.path.exists(p):
            with open(p, "rb") as f:
                return list(f.read()[:4])
    return [0x13, 0x01, 0x00, 0x6f]

async def spi_flash_stream(dut):
    while True:
        await FallingEdge(dut.flash_csb)
        for _ in range(32):
            await RisingEdge(dut.flash_clk)
        byte_idx = 0
        while True:
            for bit in range(7, -1, -1):
                await FallingEdge(dut.flash_clk)
                b = FIRMWARE[byte_idx] if byte_idx < len(FIRMWARE) else 0xFF
                dut.flash_miso.value = (b >> bit) & 1
                await RisingEdge(dut.flash_clk)
            byte_idx += 1
            try:
                if int(dut.flash_csb.value) == 1:
                    break
            except Exception:
                break
        dut.flash_miso.value = 0

@cocotb.test()
async def flash_populates_all_tile_srams(dut):
    dut.host_csb.value   = 1
    dut.host_sclk.value  = 0
    dut.host_mosi.value  = 0

    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    cocotb.start_soon(spi_flash_stream(dut))

    dut.rst.value = 1
    for _ in range(8):
        await RisingEdge(dut.clk)
    dut.rst.value = 0

    dut._log.info("Reset released — waiting for cpu_rst_n")

    for cycle in range(600_000):
        await RisingEdge(dut.clk)
        try:
            v = dut.dut.mesh_inst.cpu_rst_n.value
            if v.is_resolvable and int(v) == 1:
                dut._log.info(f"cpu_rst_n asserted at cycle {cycle}")
                break
        except Exception:
            pass

    for _ in range(10):
        await RisingEdge(dut.clk)

    exp = get_expected()
    dut._log.info(f"Expected SRAM[0:3] = {[hex(b) for b in exp]}")

    errors = 0
    for r in range(3):
        for c in range(3):
            actual = []
            for addr in range(4):
                try:
                    val = dut.dut.mesh_inst.rows[r].cols[c].tile_inst.sram_inst.mem[addr].value
                    actual.append(int(val) & 0xFF if val.is_resolvable else None)
                except Exception:
                    actual.append(None)
            await RisingEdge(dut.clk)
            if actual != exp:
                dut._log.error(f"tile({r},{c}): got={[hex(b) for b in actual if b is not None]} exp={[hex(b) for b in exp]}")
                errors += 1
            else:
                dut._log.info(f"tile({r},{c}): PASS")

    if errors == 0:
        dut._log.info("ALL 9 TILES PASS")
    else:
        raise AssertionError(f"{errors} tile(s) failed")
    