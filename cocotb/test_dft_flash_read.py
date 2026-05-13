"""
test_dft_flash_read.py — Chip-to-host readback via DFT SRAM debug ports (flash boot)

Sequence:
  1. Boot all 9 tiles from flash.
  2. Wait for boot_controller to assert cpu_rst_n (boot complete).
  3. Assert host_rst via SPI to hold CPUs and prevent SRAM bus traffic.
  4. For each tile, drive DFT ports (dft_mode, dft_tile_id, dft_addr) and
     read dft_rdata one clock cycle later.
  5. Verify SRAM[0..3] matches firmware bytes on all 9 tiles.

DFT tile_id encoding: {row[1:0], col[1:0]}
  tile(0,0)=0  tile(0,1)=1  tile(0,2)=2
  tile(1,0)=4  tile(1,1)=5  tile(1,2)=6
  tile(2,0)=8  tile(2,1)=9  tile(2,2)=10
"""

import os
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, Timer

_HERE = os.path.dirname(__file__)
FIRMWARE_BIN = os.path.join(_HERE, "firmware.bin")
SPI_HALF_NS = 500

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
# Host SPI helpers (used only to assert/release CPU reset)
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


# ---------------------------------------------------------------------------
# DFT helpers
# ---------------------------------------------------------------------------

def _dft_tile_id(row, col):
    return (row << 2) | col

async def dft_read_byte(dut, row, col, addr):
    """Read SRAM[addr] from tile(row,col) via DFT ports.
    The SRAM is synchronous: address presented at posedge CLK, Q valid ~100ps later.
    We wait one full clock cycle then sample dft_rdata.
    """
    dut.dft_mode.value    = 1
    dut.dft_tile_id.value = _dft_tile_id(row, col)
    dut.dft_we.value      = 0      # read-only
    dut.dft_addr.value    = addr
    dut.dft_wdata.value   = 0
    await RisingEdge(dut.clk)     # SRAM captures address here
    await Timer(200, unit='ps')   # wait past clk_dly (+100 ps) for Q to settle
    val = int(dut.dft_rdata.value)
    if not dut.dft_rdata.value.is_resolvable:
        return None
    return val & 0xFF


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@cocotb.test()
async def dft_flash_read(dut):
    """Boot all tiles from flash, then read SRAM contents via DFT debug ports."""

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
    dut._log.info("Waiting for flash boot to complete ...")
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

    # --- Assert host_rst to freeze CPUs (no SRAM bus traffic during DFT reads) ---
    await host_set_reset(dut, True)
    for _ in range(20):
        await RisingEdge(dut.clk)

    # --- DFT readback ---
    fw_check = FIRMWARE[:4]
    dut._log.info(f"Expected firmware bytes: {[hex(b) for b in fw_check]}")
    dut._log.info("Reading SRAM[0..3] from all 9 tiles via DFT ports ...")

    total_errors = 0
    results = {}   # (r,c) -> list of (addr, exp, got)

    for r in range(3):
        for c in range(3):
            tile_errors = 0
            tile_data = []
            for addr, exp in enumerate(fw_check):
                got = await dft_read_byte(dut, r, c, addr)
                tile_data.append((addr, exp, got))
                if got is None:
                    dut._log.error(f"tile({r},{c}) SRAM[{addr}]: DFT returned X/Z")
                    tile_errors += 1
                elif got != exp:
                    dut._log.error(
                        f"tile({r},{c}) SRAM[{addr}]: got 0x{got:02x}, expected 0x{exp:02x}"
                    )
                    tile_errors += 1
            results[(r, c)] = (tile_data, tile_errors)
            status = "PASS" if tile_errors == 0 else f"FAIL ({tile_errors} errors)"
            dut._log.info(f"  tile({r},{c}): {status}")
            total_errors += tile_errors

    # --- Turn off DFT ---
    dut.dft_mode.value = 0

    # --- Visual summary ---
    addrs = list(range(len(fw_check)))
    col_w = 9
    hdr = f"{'Tile':<8}" + "".join(f"{'SRAM['+str(a)+']':>{col_w}}" for a in addrs) + f"{'':>{col_w}}"
    sep = "-" * len(hdr)
    print("\n" + sep)
    print("  DFT FLASH BOOT READBACK SUMMARY")
    print(sep)
    print(f"{'Tile':<8}" + "".join(f"{'Exp 0x'+f'{fw_check[a]:02x}':>{col_w}}" for a in addrs) + f"{'Result':>{col_w}}")
    print(sep)
    for r in range(3):
        for c in range(3):
            tile_data, tile_errors = results[(r, c)]
            row_str = f"({r},{c})   "
            for addr, exp, got in tile_data:
                if got is None:
                    cell = "X/Z"
                elif got == exp:
                    cell = f"0x{got:02x} OK"
                else:
                    cell = f"0x{got:02x}!"
                row_str += f"{cell:>{col_w}}"
            row_str += f"{'PASS' if tile_errors == 0 else 'FAIL':>{col_w}}"
            print(row_str)
    print(sep + "\n")

    if total_errors == 0:
        dut._log.info("ALL 9 TILES PASS — DFT readback matches flash boot data.")
    else:
        raise AssertionError(
            f"DFT readback FAILED: {total_errors} mismatches across tiles"
        )
