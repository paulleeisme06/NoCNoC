import os
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

def get_expected():
    for p in ['cocotb/firmware.bin', 'src/firmware.bin']:
        if os.path.exists(p):
            with open(p, 'rb') as f:
                return list(f.read()[:4])
    return [0x13, 0x01, 0x00, 0x6f]

@cocotb.test(timeout_time=120, timeout_unit="sec")
async def chip_top_flash_boot(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())

    dut.rst_n.value = 0
    for _ in range(8):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1

    dut._log.info("Reset released — waiting for boot via chip_top pads")

    for cycle in range(600_000):
        await RisingEdge(dut.clk)
        try:
            v = dut.dut.i_chip_core.u_mesh.boot_inst.cpu_reset_n.value
            if v.is_resolvable and int(v) == 1:
                dut._log.info(f"cpu_reset_n asserted at cycle {cycle}")
                break
        except Exception:
            pass

    for _ in range(10):
        await RisingEdge(dut.clk)

    exp = get_expected()
    dut._log.info(f"Expected firmware bytes: {[hex(b) for b in exp]}")

    errors = 0
    for r in range(3):
        for c in range(3):
            actual = []
            for addr in range(4):
                try:
                    val = dut.dut.i_chip_core.u_mesh.rows[r].cols[c].tile_inst.sram_inst.mem[addr].value
                    actual.append(int(val) & 0xFF if val.is_resolvable else None)
                except Exception:
                    actual.append(None)
            await RisingEdge(dut.clk)
            if actual != exp:
                dut._log.error(f"tile({r},{c}): FAIL got={[hex(b) for b in actual if b is not None]} exp={[hex(b) for b in exp]}")
                errors += 1
            else:
                dut._log.info(f"tile({r},{c}): PASS")

    if errors == 0:
        dut._log.info("ALL TILES PASS — chip_top pad ring flash boot verified")
    else:
        raise AssertionError(f"{errors} tile(s) failed")
