import os
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

def get_expected():
    for p in ["cocotb/firmware.bin", "src/firmware.bin"]:
        if os.path.exists(p):
            with open(p, "rb") as f:
                return list(f.read()[:4])
    return [0x13, 0x01, 0x00, 0x6f]

# Tile ID encoding: {row[2:0], col[2:0]}
TILE_IDS = {
    (0,0): 0,  (0,1): 1,  (0,2): 2,
    (1,0): 8,  (1,1): 9,  (1,2): 10,
    (2,0): 16, (2,1): 17, (2,2): 18,
}

@cocotb.test(timeout_time=600, timeout_unit="sec")
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
            v = dut.dut.i_chip_core.u_mesh.cpu_rst_n.value
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
    dut._log.info("=" * 60)
    dut._log.info(f"{'Tile':<10} {'ID':<6} {'SRAM[0:3]':<30} {'Status'}")
    dut._log.info("=" * 60)

    for (r, c), tile_id in TILE_IDS.items():
        actual = []
        for addr in range(4):
            try:
                val = dut.dut.i_chip_core.u_mesh.rows[r].cols[c].tile_inst.sram_inst.mem[addr].value
                actual.append(int(val) & 0xFF if val.is_resolvable else None)
            except Exception:
                actual.append(None)
        await RisingEdge(dut.clk)

        status = "PASS" if actual == exp else "FAIL"
        actual_str = " ".join(f"0x{b:02x}" if b is not None else "??" for b in actual)
        dut._log.info(f"tile({r},{c})   {tile_id:<6} {actual_str:<30} {status}")

        if actual != exp:
            errors += 1

    dut._log.info("=" * 60)


    # Wait for firmware to run and write checkerboard
    for cycle in range(2_000_000):
        await RisingEdge(dut.clk)
        try:
            ic = dut.dut.i_chip_core.u_mesh.rows[0].cols[0].tile_inst.sram_inst.mem[0x71C].value
            if ic.is_resolvable and int(ic) == 0:
            # Also check magic byte at 0x730
                mg = dut.dut.i_chip_core.u_mesh.rows[0].cols[0].tile_inst.sram_inst.mem[0x730].value
                if mg.is_resolvable and int(mg) == 0xEF:
                    dut._log.info(f"Seed complete at cycle {cycle}")
                    break
        except Exception:
            pass

# Wait extra cycles for all 9 tiles to finish
    for _ in range(50_000):
        await RisingEdge(dut.clk)

    dut._log.info("=" * 60)
    dut._log.info(f"{'Tile':<10} {'ID':<6} {'fill_val':<10} {'SRAM[0x500]':<12} {'Status'}")
    dut._log.info("=" * 60)

    unique_errors = 0
    for (r, c), tile_id in TILE_IDS.items():
        fill_val = (tile_id + 1) & 0xFF
        try:
            v = dut.dut.i_chip_core.u_mesh.rows[r].cols[c].tile_inst.sram_inst.mem[0x500].value
            actual = int(v) & 0xFF if v.is_resolvable else None
        except Exception:
            actual = None
        await RisingEdge(dut.clk)
        status = "PASS" if actual == fill_val else "FAIL"
        if actual != fill_val:
            unique_errors += 1
        dut._log.info(f"tile({r},{c})   {tile_id:<6} {fill_val:<10} {hex(actual) if actual is not None else '??':<12} {status}")

    dut._log.info("=" * 60)
    if unique_errors == 0:
        dut._log.info("ALL TILES PASS — unique data per tile verified")
    else:
        raise AssertionError(f"{unique_errors} tile(s) failed unique data check")

    if errors == 0:
        dut._log.info("ALL 9 TILES PASS — chip_top pad ring flash boot verified")
    else:
        raise AssertionError(f"{errors} tile(s) failed firmware check")
    dut._log.info("=== 10x10 GRID VISUALIZATION ===")
    for (r, c), tile_id in TILE_IDS.items():
        fill_val = (tile_id + 1) & 0xFF
        dut._log.info(f"tile({r},{c}) ID={tile_id} fill_val={fill_val}:")
        for row in range(10):
            row_vals = []
            for col in range(10):
                addr = 0x500 + row * 10 + col
                try:
                    v = dut.dut.i_chip_core.u_mesh.rows[r].cols[c].tile_inst.sram_inst.mem[addr].value
                    val = int(v) & 0xFF if v.is_resolvable else None
                    row_vals.append(f"{val:2d}" if val is not None else " ?")
                except Exception:
                    row_vals.append(" ?")
            dut._log.info("  " + " ".join(row_vals))
        await RisingEdge(dut.clk)
