"""
test_dft_host_read.py — Chip-to-host readback via DFT ports after host SPI write

Sequence:
  1. Boot all 9 tiles from flash (establishes known SRAM[0..3] = firmware bytes).
  2. Assert host_rst via SPI to hold CPUs in reset.
  3. For each tile(r,c): use CMD 0x04 (SET_TILE) + CMD 0x00 (WRITE_SRAM) to write
     a unique 10-byte signature to SRAM[100..109]:
       SRAM[100+i] = (r*3 + c)*10 + i   (values 0..89 across tiles)
  4. Read back SRAM[100..109] from each tile via DFT ports.
  5. Verify: written values match; SRAM[0..3] (boot data) still intact.

This exercises the full pipeline:
  Flash boot → host SPI write (router flit path) → DFT chip-to-host readback
"""

import os
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, Timer

_HERE = os.path.dirname(__file__)
FIRMWARE_BIN = os.path.join(_HERE, "firmware.bin")
SPI_HALF_NS  = 500
WRITE_BASE   = 100   # SRAM address where host-written data starts
PATCH_LEN    = 10    # bytes per tile

def load_firmware():
    if os.path.exists(FIRMWARE_BIN):
        with open(FIRMWARE_BIN, "rb") as f:
            return list(f.read())
    return [0x13, 0x01, 0x00, 0x40] + [0xAA] * 1020

FIRMWARE = load_firmware()


# ---------------------------------------------------------------------------
# Flash SPI model
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Host SPI helpers
# ---------------------------------------------------------------------------

async def _spi_byte(dut, byte_val):
    recv = 0
    for bit in range(7, -1, -1):
        dut.host_mosi.value = (byte_val >> bit) & 1
        await Timer(SPI_HALF_NS, unit='ns')
        dut.host_sclk.value = 1
        recv = (recv << 1) | int(dut.host_miso.value)
        await Timer(SPI_HALF_NS, unit='ns')
        dut.host_sclk.value = 0
    return recv & 0xFF

async def spi_transaction(dut, byte_list):
    dut.host_csb.value = 0
    await Timer(SPI_HALF_NS, unit='ns')
    result = [await _spi_byte(dut, b) for b in byte_list]
    await Timer(SPI_HALF_NS, unit='ns')
    dut.host_csb.value = 1
    await Timer(SPI_HALF_NS * 4, unit='ns')
    return result

async def host_set_reset(dut, assert_rst):
    await spi_transaction(dut, [0x03, 0xFF if assert_rst else 0x00])

async def host_set_tile(dut, row, col):
    await spi_transaction(dut, [0x04, row & 0xFF, col & 0xFF])

async def host_write_sram(dut, addr, data):
    await spi_transaction(dut, [
        0x00,
        (addr >> 8) & 0x07,
        addr & 0xFF,
        data & 0xFF,
    ])


# ---------------------------------------------------------------------------
# DFT helpers
# ---------------------------------------------------------------------------

def _dft_tile_id(row, col):
    return (row << 2) | col

async def dft_read_byte(dut, row, col, addr):
    """Read SRAM[addr] from tile(row,col) via DFT ports."""
    dut.dft_mode.value    = 1
    dut.dft_tile_id.value = _dft_tile_id(row, col)
    dut.dft_we.value      = 0
    dut.dft_addr.value    = addr
    dut.dft_wdata.value   = 0
    await RisingEdge(dut.clk)
    await Timer(200, unit='ps')
    if not dut.dft_rdata.value.is_resolvable:
        return None
    return int(dut.dft_rdata.value) & 0xFF


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@cocotb.test()
async def dft_host_read(dut):
    """Flash boot → host SPI write → DFT readback for all 9 tiles."""

    # --- Init pins ---
    dut.flash_miso.value  = 0
    dut.host_csb.value    = 1
    dut.host_sclk.value   = 0
    dut.host_mosi.value   = 0
    dut.dft_mode.value    = 0
    dut.dft_tile_id.value = 0
    dut.dft_we.value      = 0
    dut.dft_addr.value    = 0
    dut.dft_wdata.value   = 0

    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    cocotb.start_soon(spi_flash_stream(dut))

    # --- Power-on reset ---
    dut.rst.value = 1
    for _ in range(8):
        await RisingEdge(dut.clk)
    dut.rst.value = 0

    # --- Wait for flash boot ---
    dut._log.info("Waiting for flash boot ...")
    boot_cycle = None
    for cycle in range(700_000):
        await RisingEdge(dut.clk)
        try:
            v = dut.mesh_inst.cpu_rst_n.value
            if v.is_resolvable and int(v) == 1:
                boot_cycle = cycle
                break
        except Exception:
            pass
        if cycle % 100_000 == 0 and cycle > 0:
            dut._log.info(f"  heartbeat: cycle {cycle}")

    assert boot_cycle is not None, "Timeout: cpu_rst_n never asserted"
    dut._log.info(f"Boot complete at cycle {boot_cycle}")

    # --- Assert host_rst: freeze CPUs, hand SRAM bus to host ---
    dut._log.info("Asserting host reset ...")
    await host_set_reset(dut, True)
    for _ in range(10):
        await RisingEdge(dut.clk)

    # --- Phase 1: host SPI writes unique signatures to SRAM[100..109] ---
    dut._log.info(f"Writing unique 10-byte signatures to SRAM[{WRITE_BASE}..{WRITE_BASE+PATCH_LEN-1}] per tile ...")
    for r in range(3):
        for c in range(3):
            await host_set_tile(dut, r, c)
            for i in range(PATCH_LEN):
                addr = WRITE_BASE + i
                val  = ((r * 3 + c) * PATCH_LEN + i) & 0xFF
                await host_write_sram(dut, addr, val)
            dut._log.info(f"  tile({r},{c}): wrote {PATCH_LEN} bytes starting at addr {WRITE_BASE}")

    # Settle
    for _ in range(20):
        await RisingEdge(dut.clk)

    # --- Phase 2: DFT readback ---
    dut._log.info("Reading back via DFT ports ...")
    total_errors = 0
    results = {}   # (r,c) -> {"written": [(addr,val)], "read_host": [(addr,exp,got)], "read_boot": [(addr,exp,got)], "errors": int}

    for r in range(3):
        for c in range(3):
            tile_errors = 0
            written   = []
            read_host = []
            read_boot = []

            # Check written data at SRAM[100..109]
            for i in range(PATCH_LEN):
                addr = WRITE_BASE + i
                exp  = ((r * 3 + c) * PATCH_LEN + i) & 0xFF
                written.append((addr, exp))
                got  = await dft_read_byte(dut, r, c, addr)
                read_host.append((addr, exp, got))
                if got is None:
                    dut._log.error(f"tile({r},{c}) SRAM[{addr}]: DFT returned X/Z")
                    tile_errors += 1
                elif got != exp:
                    dut._log.error(
                        f"tile({r},{c}) SRAM[{addr}]: got 0x{got:02x}, expected 0x{exp:02x}"
                    )
                    tile_errors += 1

            # Also verify boot data at SRAM[0..3] is still intact
            for addr, exp in enumerate(FIRMWARE[:4]):
                got = await dft_read_byte(dut, r, c, addr)
                read_boot.append((addr, exp, got))
                if got is None:
                    dut._log.error(f"tile({r},{c}) SRAM[{addr}] (boot): DFT returned X/Z")
                    tile_errors += 1
                elif got != exp:
                    dut._log.error(
                        f"tile({r},{c}) SRAM[{addr}] (boot): got 0x{got:02x}, expected 0x{exp:02x}"
                    )
                    tile_errors += 1

            results[(r, c)] = {
                "written":   written,
                "read_host": read_host,
                "read_boot": read_boot,
                "errors":    tile_errors,
            }
            status = "PASS" if tile_errors == 0 else f"FAIL ({tile_errors} errors)"
            dut._log.info(f"  tile({r},{c}): {status}")
            total_errors += tile_errors

    # --- Turn off DFT ---
    dut.dft_mode.value = 0

    # --- Visual summary: host-written region ---
    col_w = 11
    print("\n" + "=" * 80)
    print("  HOST WRITE -> DFT READBACK SUMMARY  (SRAM[100..109])")
    print("=" * 80)
    hdr = f"{'Tile':<8}" + "".join(f"[{WRITE_BASE+i}]".center(col_w) for i in range(PATCH_LEN)) + f"{'Result':>{col_w}}"
    print(hdr)
    print("-" * 80)
    for r in range(3):
        for c in range(3):
            info = results[(r, c)]
            row = f"({r},{c})   "
            for addr, exp, got in info["read_host"]:
                if got is None:
                    cell = "X/Z"
                elif got == exp:
                    cell = f"W{exp:02x}=R{got:02x}"
                else:
                    cell = f"W{exp:02x}≠R{got:02x}"
                row += cell.center(col_w)
            row += f"{'PASS' if info['errors'] == 0 else 'FAIL':>{col_w}}"
            print(row)
    print("-" * 80)

    # --- Visual summary: boot data still intact ---
    print()
    print("  BOOT DATA INTEGRITY CHECK  (SRAM[0..3])")
    print("-" * 50)
    boot_hdr = f"{'Tile':<8}" + "".join(f"[{i}]".center(12) for i in range(4)) + f"{'Result':>10}"
    print(boot_hdr)
    print("-" * 50)
    for r in range(3):
        for c in range(3):
            info = results[(r, c)]
            row = f"({r},{c})   "
            boot_ok = True
            for addr, exp, got in info["read_boot"]:
                if got is None:
                    cell = "X/Z"
                    boot_ok = False
                elif got == exp:
                    cell = f"0x{got:02x} OK"
                else:
                    cell = f"0x{got:02x}!"
                    boot_ok = False
                row += cell.center(12)
            row += f"{'OK' if boot_ok else 'FAIL':>10}"
            print(row)
    print("=" * 80 + "\n")

    if total_errors == 0:
        dut._log.info("ALL 9 TILES PASS — host write + DFT readback verified.")
    else:
        raise AssertionError(
            f"host write + DFT readback FAILED: {total_errors} mismatches"
        )
