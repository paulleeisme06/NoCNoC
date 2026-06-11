# NoCNoC: A Hierarchical 3×4 Mesh Network-on-Chip

NoCNoC is a hierarchical 3×4 mesh Network-on-Chip (NoC) in **GlobalFoundries 180 nm (3.3 V)**, built with **LibreLane**. The architecture is a tiled, scalable compute mesh where one tile is hardened once as a macro and instantiated 12 times.

## Setup & Helpful Commands

### 1. Initialize Submodules
To get started, you must initialize the submodules. This fetches the SERV RISC-V core, the LibreLane flow components, and other critical IP blocks including the SRAM wrappers.
```bash
git submodule update --init --recursive
```

### 2. PDK & Flow
-   **PDK**: We use a custom fork of the [gf180mcu PDK](https://github.com/wafer-space/gf180mcu).
    ```bash
    make clone-pdk
    ```
-   **Environment**: Launch the development shell (Nix-based) to access the LibreLane tools:
    ```bash
    nix-shell
    ```

### 3. Firmware Toolchain
For compiling firmware (`.bin` files), you **must** use `riscv64-unknown-elf-gcc` **version 15.2.0**.
-   **Check Version**: `riscv64-unknown-elf-gcc --version`

### 4. Implementation & Viewing
-   **Run Implementation**: `make librelane`
-   **OpenROAD (GDS View)**: `make librelane-openroad`
-   **KLayout (LVS/DRC View)**: `make librelane-klayout`

---

## Architecture

The chip is organized as a 4×3 mesh of independent RISC-V compute nodes connected via inter-tile NoC links.

### Building Blocks
-   **Tile (×12, 730×920 µm)**: Contains a **SERV RISC-V** core, **2 KB SRAM** (two 1024×8 banks merged), and a **NoC router**. It is hardened into a macro (GDS/LEF/LIB).
-   **Mesh Fabric (mesh_rxc)**: Handles the inter-tile wiring and decodes DFT/debug access.
-   **Host-to-Chip SPI Debug (spi_debug)**: Provides SPI access for an external host to read/write each tile's memory.
-   **Boot Controller**: Feeds instructions to the mesh from an **external off-chip SPI flash**.
-   **Periphery**: Includes 58 IO/power pads, a chip-ID block, and the wafer.space logo (14 macros total).

### Scale & Results
-   **Integration**: 191,358 instances (41,803 std cells + 12 hardened tiles).
-   **Physical**: 3932×5122 µm die, ~3048×4238 µm core area.
-   **Power/Clock**: ~12.3 mW consumption at a 40 MHz clock frequency.
-   **Signoff**: Full clean signoff (DRC, LVS, antenna, XOR, density, power all 0; 0 timing violations across 6 corners; IR drop ~0.14 mV).

---

## Technical Story: Power Integration & Design Choices

### The Power Integration Contribution
Because each tile must use Metal4/Metal5 for its own grid (due to the SRAM macros forcing Metal5), the standard "macro gives up its top layers" method was not feasible. We solved this by **pitch-matching the chip PDN to the tile** (74.48 µm = 19 rows = 133 sites, grid-commensurate) and aligning the power straps to the tile's boundary pins via **sub-micron perpendicular edge vias**. The placement was re-tuned so VDD and VSS each bridge on a different edge.

### Why No Power Ring?
We chose a pin-aligned version over a formal power ring for several engineering reasons:
-   **7× fewer signal-integrity warnings**: No-ring had 23 max-slew warnings vs. 162 for the ring-based experiment.
-   **Less Congestion**: The ring wraps tiles in extra M4/M5 metal, crowding signal routing at the edges. No-ring leaves more headroom.
-   **Simpler PDN**: Fewer structures mean fewer failure modes and easier verification for first silicon.
-   **Zero Functional Loss**: Both methods pass all gating checks, but the ring's IR drop gain (0.11 mV vs 0.14 mV) is negligible (both are ~100× below any concern).

### Timing Signoff (Cores ARE Timed)
Detailed hierarchical STA provides two layers of evidence:
1.  **Tile Level**: Hardened tiles were independently STA'd with real register-to-register paths. They closed at setup +10.5 ns, hold +0.092 ns, with 0 violations.
2.  **Chip Level**: 2,350 timing endpoints reference the tiles. They contribute their characterized 1,136-arc Liberty timing to the top-level graph. The chip closes at setup +16.1 ns, hold +0.092 ns, 0 violations.

**Note on Max Cap/Slew**: These are **non-gating electrical warnings**, not timing failures. They do not affect timing (zero violations) or manufacturability (DRC/LVS pass). Most are sub-femtofarad noise inside the tiles and IO pads.

## Verification & Simulation

We use a comprehensive **cocotb** simulation suite for multi-level verification:

-   `make sim`: Runs a full RTL simulation of the 3×4 mesh using the top-level `chip_top_tb.py`.
-   `make sim-flash`: Verifies the physical integration path from the external SPI flash, through the housekeeping boot-adapter, to the internal NoC SRAMs.
-   `make sim-gl`: Gate-level simulation of the entire chip using the synthesized netlist.
-   `make sim-gl-tile`: Targeted mixed RTL/GL simulation that uses the gate-level netlist for a single tile while keeping the rest of the mesh in RTL for faster iteration.

## Acknowledgments

This project builds upon several award-winning open-source components:
-   **SERV**: The world's smallest compliant bitserial RISC-V CPU, designed by Olof Kindgren.
-   **Subservient**: A minimal SoC infrastructure designed for SERV, providing the SRAM-banking logic and SoC-let wrapper used in our tiles.

## Limitations & Caveats

-   **3×4 Mesh Maximum**: This is the max in the fixed die slot; 4×4 would collapse the routing channels (~42 µm vs the ~236 µm needed).
-   **Conservative Clock**: 40 MHz is used (only ~36% of the period) as this is not a performance-targeted part.
-   **DFT Readback Issue**: Due to the row-3 readback limitation in this specific run (fixed on the ring branch), the host can write to all 12 tiles but only read back from 9. Normal operation is unaffected.
-   **External Boot**: Programs are fed from an off-chip external flash component, not on-die memory.

## Usage
Each tile is an independent RISC-V node. The NoC routes messages between them. A host loads programs and data via the SPI debug interface, and the cores boot from external flash.
