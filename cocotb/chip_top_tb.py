# SPDX-FileCopyrightText: © 2025 Project Template Contributors
# SPDX-License-Identifier: Apache-2.0

import os
import random
import logging
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer, Edge, RisingEdge, FallingEdge, ClockCycles
from cocotb_tools.runner import get_runner

sim = os.getenv("SIM", "icarus")
pdk_root = os.getenv("PDK_ROOT", Path("~/.ciel").expanduser())
pdk = os.getenv("PDK", "gf180mcuD")
scl = os.getenv("SCL", "gf180mcu_as_sc_mcu7t3v3")
gl = os.getenv("GL", False)
slot = os.getenv("SLOT", "1x1")

hdl_toplevel = "chip_top"

async def set_defaults(dut):
    dut.input_PAD.value = 0

async def enable_power(dut):
    dut.VDD.value = 1
    dut.VSS.value = 0

async def start_clock(clock, freq=50):
    """Start the clock @ freq MHz"""
    c = Clock(clock, 1 / freq * 1000, "ns")
    cocotb.start_soon(c.start())


async def reset(reset, active_low=True, time_ns=1000):
    """Reset dut"""
    cocotb.log.info("Reset asserted...")

    reset.value = not active_low
    await Timer(time_ns, "ns")
    reset.value = active_low

    cocotb.log.info("Reset deasserted.")


async def start_up(dut):
    """Startup sequence"""
    await set_defaults(dut)
    if gl:
        await enable_power(dut)
    await start_clock(dut.clk_PAD)
    await reset(dut.rst_n_PAD)


@cocotb.test()
async def test_counter(dut):
    """Run the counter test"""

    # Create a logger for this testbench
    logger = logging.getLogger("my_testbench")

    logger.info("Startup sequence...")

    # Start up
    await start_up(dut)

    logger.info("Running the test...")

    # Wait for some time...
    await ClockCycles(dut.clk_PAD, 10)

    # Start the counter by setting all inputs to 1
    dut.input_PAD.value = -1

    # Wait for a number of clock cycles
    await ClockCycles(dut.clk_PAD, 100)

    # Check the end result of the counter
    assert dut.bidir_PAD.value == 100 - 1

    logger.info("Done!")


def chip_top_runner():

    proj_path = Path(__file__).resolve().parent

    sources = []
    defines = {f"SLOT_{slot.upper()}": True}
    includes = [proj_path / "../src/"]

    if gl:
        # SCL models
        sources.append(Path(pdk_root) / pdk / "libs.ref" / scl / "verilog" / f"{scl}.v")
        primitives = Path(pdk_root) / pdk / "libs.ref" / scl / "verilog" / "primitives.v"
        if primitives.exists():
            sources.append(primitives)

        # We use the powered netlist
        sources.append(proj_path / f"../final/pnl/{hdl_toplevel}.pnl.v")

        defines = {"FUNCTIONAL": True, "USE_POWER_PINS": True}
    else:
        sources.append(proj_path / "../src/chip_top.sv")
        sources.append(proj_path / "../src/chip_core.sv")

    sources += [
        # IO pad models
        Path(pdk_root) / pdk / "libs.ref/gf180mcu_fd_io/verilog/gf180mcu_fd_io.v",
        Path(pdk_root) / pdk / "libs.ref/gf180mcu_fd_io/verilog/gf180mcu_ws_io.v",
        
        # SRAM macros (3.3V)
        proj_path / "../libs/gf180mcu_ocd_ip_sram/cells/gf180mcu_ocd_ip_sram__sram512x8m8wm1/gf180mcu_ocd_ip_sram__sram512x8m8wm1.v",
        
        # Custom IP
        proj_path / "../ip/gf180mcu_ws_ip__id/vh/gf180mcu_ws_ip__id.v",
        proj_path / "../ip/gf180mcu_ws_ip__logo/vh/gf180mcu_ws_ip__logo.v",

        # Ethan: Added these files:
        proj_path / "../src/dft_ethan/spi_debug.v",
        proj_path / "../src/dft_ethan/gf180mcu_fd_ip_sram__sram2048x8m8wm1.v",
        proj_path / "../src/mesh_psi_aan/mesh_3x3.v",
        proj_path / "../src/mesh_psi_aan/mesh_tile.v",
        proj_path / "../src/mesh_psi_aan/mesh_router.v",
        proj_path / "../src/mesh_psi_aan/boot_controller.v",
        # Add all .v files from subservient and serv submodules:
        *sorted((proj_path / "../src/subservient/rtl").glob("*.v")),
        *sorted((proj_path / "../src/serv/rtl").glob("*.v")),
        *sorted((proj_path / "../src/serv/servile").glob("*.v")),
    ]

    build_args = []

    if sim == "icarus":
        # For debugging
        # build_args = ["-Winfloop", "-pfileline=1"]
        pass

    if sim == "verilator":
        build_args = ["--timing", "--trace", "--trace-fst", "--trace-structs"]

    runner = get_runner(sim)
    runner.build(
        sources=sources,
        hdl_toplevel=hdl_toplevel,
        defines=defines,
        always=True,
        includes=includes,
        build_args=build_args,
        waves=True,
    )

    plusargs = []

    runner.test(
        hdl_toplevel=hdl_toplevel,
        test_module="chip_top_tb,",
        plusargs=plusargs,
        waves=True,
    )

# Ethan: helper SPI functions
# input_PAD[0] = spi_clk, [1] = spi_mosi, [2] = spi_cs_n, [3] = dft_mode
# bidir_PAD[0] = spi_miso (output from chip)

# ── DFT SPI Debug Interface ────────────────────────────────────────────────
# SPI half-period. Must satisfy: HALF_PERIOD >> 4 * sys_clk_period.
# At 25 MHz sys_clk (40 ns period), 500 ns gives ~12 sys_clk cycles per
# half-period, safely above the 4-cycle synchroniser requirement.
SPI_HALF_NS = 500

_DFT_MODE = 1 << 3   # input_PAD[3]
_CS_N     = 1 << 2   # input_PAD[2]  (active low)
_MOSI     = 1 << 1   # input_PAD[1]
_SCLK     = 1 << 0   # input_PAD[0]

def _miso(dut):
    # Read bidir_PAD[0] (spi_miso). Other bits may be X/Z; use string LSB.
    s = str(dut.bidir_PAD.value)  # e.g. "XXXX...X1" or "XXXX...X0"
    return 1 if s[-1] == '1' else 0

async def dft_spi_transact(dut, write, tile_id, addr, wdata=0x00):
    """
    Drive one 32-bit spi_debug frame and return the 8-bit read data.
    Frame: [31]=R/W  [30:27]=tile_id  [26:16]=addr  [15:8]=dc  [7:0]=wdata
    For reads (write=False), MISO is sampled during bits 22-29 (0-indexed).
    """
    frame = ((1 if write else 0) << 31) | ((tile_id & 0xF) << 27) \
        | ((addr & 0x7FF) << 16) | (wdata & 0xFF)
    
    miso_bits = []

    # Assert CS (cs_n low), keep dft_mode high
    dut.input_PAD.value = _DFT_MODE
    await Timer(SPI_HALF_NS, "ns")

    for i in range(32):
        mosi = (frame >> (31 - i)) & 1

        # Rising edge: drive MOSI
        dut.input_PAD.value = _DFT_MODE | (mosi * _MOSI) | _SCLK
        await Timer(SPI_HALF_NS, "ns")

        # Falling edge: slave drives MISO
        # spi_debug drives MISO when bit_cnt is 23..30
        dut.input_PAD.value = _DFT_MODE | (mosi * _MOSI)
        await Timer(SPI_HALF_NS // 2, "ns")  # wait for MISO to settle

        if not write and 22 <= i <= 29:
            miso_bits.append(_miso(dut))
        
        await Timer(SPI_HALF_NS // 2, "ns")

    # Deassert CS - triggers write commit inside spi_debug
    dut.input_PAD.value = _DFT_MODE | _CS_N
    await Timer(SPI_HALF_NS, "ns")

    if miso_bits:
        result = 0
        for b in miso_bits:
            result = (result << 1) | b
        return result
    return 0

async def dft_write(dut, tile_id, addr, data):
    await dft_spi_transact(dut, write=True, tile_id=tile_id, addr=addr, wdata=data)

async def dft_read(dut, tile_id, addr):
    return await dft_spi_transact(dut, write=False, tile_id=tile_id, addr=addr)

@cocotb.test()
async def test_dft_write_read(dut):
    """
    Write a byte to tile (0,0) SRAM via SPI debug interface, read it back,
    verify it matches. Cores are held in reset by dft_mode so they cannot
    interfere with the SRAM.
    """
    await start_up(dut)

    # Assert dft_mode before transactions (cores fo into reset inside chip)
    dut.input_PAD.value = _DFT_MODE | _CS_N
    await ClockCycles(dut.clk_PAD, 4)

    tile_id = 0b0000    # tile (row 0, col 0)
    addr = 0x050        # random address
    wdata = 0xA5        # random data

    await dft_write(dut, tile_id, addr, wdata)
    rdata = await dft_read(dut, tile_id, addr)

    dut._log.info(f"DFT write =0x{wdata:02x} read = 0x{rdata:02x}")
    assert rdata == wdata, f"DFT read back failed: wrote 0x{wdata:02x}, got 0x{rdata:02x}"

    # release dft_mode
    dut.input_PAD.value = _CS_N
    dut._log.info("test_dft_write_read PASSED")



    



    


if __name__ == "__main__":
    chip_top_runner()
