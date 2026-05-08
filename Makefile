MAKEFILE_DIR := $(shell dirname $(realpath $(firstword $(MAKEFILE_LIST))))
 
RUN_TAG = $(shell ls librelane/runs/ | tail -n 1)
TOP = chip_top
 
PDK_ROOT ?= $(MAKEFILE_DIR)/gf180mcu
PDK ?= gf180mcuD
PDK_TAG ?= 1.8.0
SCL ?= gf180mcu_as_sc_mcu7t3v3
 
AVAILABLE_SLOTS = 1x1 0p5x1 1x0p5 0p5x0p5
DEFAULT_SLOT = 1x1
 
# Slot can be any of AVAILABLE_SLOTS
SLOT ?= $(DEFAULT_SLOT)
 
ifeq ($(SLOT),default)        
    SLOT = $(DEFAULT_SLOT)
endif
 
ifeq ($(filter $(SLOT),$(AVAILABLE_SLOTS)),)
    $(error $(SLOT) does not exist in AVAILABLE_SLOTS: $(AVAILABLE_SLOTS))
endif
 
#----------------------------------------
#Simulation Configuration:
#----------------------------------------
 
TOPLEVEL_LANG = verilog
TOPLEVEL      = top
#MODULE        = test_flash_mesh
 
SRC   := $(MAKEFILE_DIR)/src
FLASH := $(SRC)/flash_Sumi
MESH  := $(SRC)/integration
DFT   := $(SRC)/subservient/ethan
 
VERILOG_EXTRA_DIRS = \
    $(MAKEFILE_DIR)/src/serv/rtl \
    $(MAKEFILE_DIR)/src/serv/servile \
    $(MAKEFILE_DIR)/src/subservient/rtl
 
VERILOG_EXTRA = $(wildcard $(addsuffix /*.v,$(VERILOG_EXTRA_DIRS)))
 
VERILOG_SOURCES := \
    $(SRC)/top.v \
    $(MESH)/boot_controller.v \
    $(MESH)/mesh_3x3.v \
    $(MESH)/mesh_tile.v \
    $(MESH)/mesh_router.v \
    $(FLASH)/host_spi_slave.v \
    $(FLASH)/rd_crossbar.v \
    $(FLASH)/spi_arbiter.v \
    $(FLASH)/hk_boot_adapter.v \
    $(FLASH)/housekeeping.sv \
    $(FLASH)/flash_clk.sv \
    $(FLASH)/shiftregister.sv \
    $(DFT)/gf180mcu_fd_ip_sram__sram2048x8m8wm1.v \
    $(VERILOG_EXTRA)
 
export VERILOG_SOURCES
 
SIM = verilator
 
COMPILE_ARGS += \
    --timing \
	--public \
    -I$(SRC) \
    -I$(FLASH) \
    -I$(MESH) \
    $(addprefix -I,$(VERILOG_EXTRA_DIRS)) \
    -Wno-PINMISSING \
    -Wno-MODDUP \
    -Wno-MINTYPMAXDLY \
    -Wno-MULTIDRIVEN \
    -I$(SRC)/subservient/ethan
 
#-------------------------------------
#Help
#-------------------------------------
# Only include cocotb Makefile.sim if cocotb-config is available
COCOTB_MAKEFILES := $(shell cocotb-config --makefiles 2>/dev/null)
ifneq ($(COCOTB_MAKEFILES),)
    include $(COCOTB_MAKEFILES)/Makefile.sim
endif
 
.DEFAULT_GOAL := help
 
#Can add another target by using the ## sign 
help: ## Show this help message 
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Simulation:'
	@grep -E '^sim[a-zA-Z_-]*:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'
	@echo ''
	@echo 'ASIC Flow:'
	@grep -E '^librelane[a-zA-Z_-]*:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'
	@echo ''
	@echo 'PDK / Setup:'
	@grep -E '^(clone|install|render)[a-zA-Z_-]*:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'
.PHONY: help
 
all: librelane ## Build the project (runs LibreLane)
.PHONY: all
 
sim-flash: ## Run flash->housekeeping->mesh SRAM test
	$(MAKE) results.xml \
		MODULE=test_flash_mesh \
		SIM_BUILD=sim_build/flash \
		PYTHONPATH=$(MAKEFILE_DIR)/cocotb:$(PYTHONPATH) \
		VERILOG_SOURCES="$(VERILOG_SOURCES)"
.PHONY: sim-flash
 
sim-top: ## Run full pipeline test (boot → host seed → GoL → readback)
	$(MAKE) results.xml \
		MODULE=test_top \
		SIM_BUILD=sim_build/top \
		PYTHONPATH=$(MAKEFILE_DIR)/cocotb:$(PYTHONPATH) \
		VERILOG_SOURCES="$(VERILOG_SOURCES)"
.PHONY: sim-top

sim-host:
	$(MAKE) results.xml \
		TOPLEVEL=top \
		MODULE=test_host \
		SIM_BUILD=sim_build/host \
		PYTHONPATH=$(CURDIR)/cocotb:$(PYTHONPATH)


# andrea added this
MESH_SOURCES := \
    $(SRC)/integration/mesh_3x3.v \
    $(SRC)/integration/mesh_tile.v \
    $(SRC)/integration/mesh_router.v \
    $(SRC)/integration/boot_controller.v \
    $(SRC)/subservient/ethan/gf180mcu_fd_ip_sram__sram1024x8m8wm1.v \
    $(SRC)/subservient/ethan/gf180mcu_fd_ip_sram__sram2048x8m8wm1.v \
    $(SRC)/subservient/ethan/gf180mcu_ocd_ip_sram__sram1024x8m8wm1.v \
    $(SRC)/subservient/ethan/sram2048x8_gf180.v \
    $(SRC)/subservient/ethan/subservient_core.v \
    $(SRC)/subservient/ethan/subservient_rf_ram_if.v \
    $(SRC)/subservient/ethan/subservient_generic_ram.v \
    $(SRC)/subservient/ethan/subservient_gf180_ram_1024x8.v \
    $(SRC)/subservient/ethan/sram_mbist.v \
    $(SRC)/subservient/ethan/spi_wb_debug_v4.v \
    $(SRC)/subservient/ethan/subservient_gpio.v \
    $(SRC)/subservient/ethan/subservient.v \
    $(SRC)/subservient/ethan/subservient_debug_top.v \
    $(SRC)/subservient/ethan/subservient_ram.v \
    $(SRC)/subservient/ethan/subservient_debug_switch.v \
    $(VERILOG_EXTRA)
 
sim-mesh: ## Run mesh-only test (mesh_3x3 DUT, flash boot, GoL)
	$(MAKE) results.xml \
		TOPLEVEL=mesh_3x3 \
		MODULE=test_mesh \
		SIM_BUILD=sim_build/mesh \
		VERILOG_SOURCES="$(MESH_SOURCES)" \
		PYTHONPATH=$(MAKEFILE_DIR)/cocotb:$(PYTHONPATH)
.PHONY: sim-mesh
# andrea added this
 
clone-pdk: ## Clone the GF180MCU PDK repository
	rm -rf $(MAKEFILE_DIR)/gf180mcu
	git clone https://github.com/wafer-space/gf180mcu.git $(MAKEFILE_DIR)/gf180mcu --depth 1 --branch ${PDK_TAG}
.PHONY: clone-pdk
 
install-3v3-scl: ## Install the 3.3V standard cell library into the PDK
	git submodule update --init libs/gf180mcu_as_sc_mcu7t3v3
	cp -r $(MAKEFILE_DIR)/libs/gf180mcu_as_sc_mcu7t3v3/pdk/libs.ref/gf180mcu_as_sc_mcu7t3v3 $(PDK_ROOT)/$(PDK)/libs.ref/
	cp -r $(MAKEFILE_DIR)/libs/gf180mcu_as_sc_mcu7t3v3/pdk/libs.tech/librelane $(PDK_ROOT)/$(PDK)/libs.tech/
	cp -r $(MAKEFILE_DIR)/libs/gf180mcu_as_sc_mcu7t3v3/pdk/libs.tech/magic $(PDK_ROOT)/$(PDK)/libs.tech/
	cp $(MAKEFILE_DIR)/librelane/gf180mcu_as_sc_mcu7t3v3_config.tcl $(PDK_ROOT)/$(PDK)/libs.tech/librelane/gf180mcu_as_sc_mcu7t3v3/config.tcl
.PHONY: install-3v3-scl
 
librelane: ## Run LibreLane flow (synthesis, PnR, verification)
	librelane librelane/slots/slot_${SLOT}.yaml librelane/config.yaml --save-views-to $(MAKEFILE_DIR)/final --pdk ${PDK} --pdk-root ${PDK_ROOT} --scl ${SCL} --manual-pdk
.PHONY: librelane
 
librelane-nodrc: ## Run LibreLane flow without DRC checks
	librelane librelane/slots/slot_${SLOT}.yaml librelane/config.yaml --save-views-to $(MAKEFILE_DIR)/final --pdk ${PDK} --pdk-root ${PDK_ROOT} --scl ${SCL} --manual-pdk --skip KLayout.Antenna --skip KLayout.DRC --skip Magic.DRC
.PHONY: librelane-nodrc
 
librelane-klayoutdrc: ## Run LibreLane flow without magic DRC checks
	librelane librelane/slots/slot_${SLOT}.yaml librelane/config.yaml --save-views-to $(MAKEFILE_DIR)/final --pdk ${PDK} --pdk-root ${PDK_ROOT} --scl ${SCL} --manual-pdk --skip Magic.DRC
.PHONY: librelane-klayoutdrc
 
librelane-magicdrc: ## Run LibreLane flow without KLayout DRC checks
	librelane librelane/slots/slot_${SLOT}.yaml librelane/config.yaml --save-views-to $(MAKEFILE_DIR)/final --pdk ${PDK} --pdk-root ${PDK_ROOT} --scl ${SCL} --manual-pdk --skip KLayout.DRC
.PHONY: librelane-magicdrc
 
librelane-openroad: ## Open the last run in OpenROAD
	librelane librelane/slots/slot_${SLOT}.yaml librelane/config.yaml --pdk ${PDK} --pdk-root ${PDK_ROOT} --scl ${SCL} --manual-pdk --last-run --flow OpenInOpenROAD
.PHONY: librelane-openroad
 
librelane-klayout: ## Open the last run in KLayout
	librelane librelane/slots/slot_${SLOT}.yaml librelane/config.yaml --pdk ${PDK} --pdk-root ${PDK_ROOT} --scl ${SCL} --manual-pdk --last-run --flow OpenInKLayout
.PHONY: librelane-klayout
 
librelane-padring: ## Only create the padring
	PDK_ROOT=${PDK_ROOT} PDK=${PDK} python3 scripts/padring.py librelane/slots/slot_${SLOT}.yaml librelane/config.yaml
.PHONY: librelane-padring
 
sim: ## Run RTL simulation with cocotb
	cd cocotb; PDK_ROOT=${PDK_ROOT} PDK=${PDK} SLOT=${SLOT} python3.13 chip_top_tb.py
.PHONY: sim
 
sim-gl: ## Run gate-level simulation with cocotb (after copy-final)
	cd cocotb; GL=1 PDK_ROOT=${PDK_ROOT} PDK=${PDK} SLOT=${SLOT} python3.13 chip_top_tb.py
.PHONY: sim-gl
 
sim-view: ## View simulation waveforms in GTKWave
	gtkwave cocotb/sim_build/chip_top.fst
.PHONY: sim-view
 
render-image: ## Render an image from the final layout (after copy-final)
	mkdir -p img/
	PDK_ROOT=${PDK_ROOT} PDK=${PDK} python3 scripts/lay2img.py final/gds/${TOP}.gds img/${TOP}.png --width 2048 --oversampling 4
.PHONY: render-image