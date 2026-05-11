#include <stdint.h>

// ============================================================================
// Mesh configuration — change only MESH_N to resize the mesh.
// TILE_ID encoding uses 2 bits per dimension: max mesh size is 4×4.
// For larger meshes (5×5+) you need to widen TILE_ID in the Verilog too.
// ============================================================================
#define MESH_R  5
#define MESH_C  5
#define MESH_ROWS  MESH_R   // alias if you use MESH_ROWS anywhere
#define MESH_COLS  MESH_C

// ============================================================================
// NoC memory-mapped registers
// ============================================================================
#define NOC_INJECT_BASE  0x80000000u   /* write = inject flit */
#define NOC_RECV_BASE    0x80000004u   /* read  = pop ejection FIFO */
#define NOC_ID_BASE      0x80000008u   /* read  = my TILE_ID (4-bit) */

// TILE_ID(r,c) — must match the Verilog encoding: row in [3:2], col in [1:0]
#define TILE_ID(r,c)    (((uint32_t)(r) << 3) | (uint32_t)(c))

// Flit layout: [31:28]=dest TILE_ID, [10]=valid, [9:0]=payload bitmap
#define FLIT_DEST_SHIFT  26u
#define FLIT_BMAP_MASK   0x3FFu
#define FLIT_VALID_BIT   0x400u

// ============================================================================
// Signal words written to NOC_INJECT_BASE for testbench visibility
// (dest field = 0 so they route to tile(0,0); the testbench watches for them)
// ============================================================================
#define SIG_BOOT_ALIVE   0xF0000001u
#define SIG_SEED_LIVE    0xF0000002u
#define SIG_MATH_DONE    0xF0000003u
#define SIG_GEN_STABLE   0xF0000004u

// ============================================================================
// Game-of-Life grid parameters
// ============================================================================
#define SIZE       10          /* each idowns a 10×10 sub-grid */

// ============================================================================
// SRAM memory map (all offsets are byte addresses within the 2 KB SRAM)
// ============================================================================
#define GRID_BASE       0x0500u
#define GHOST_BASE      0x0600u
#define NEXT_GRID_BASE  0x0640u

// Ghost buffer layout (each buffer holds SIZE bytes):
//   ghost_N  @ GHOST_BASE +  0  .. +  9   (bottom row of north neighbour)
//   ghost_S  @ GHOST_BASE + 10  .. + 19   (top    row of south neighbour)
//   ghost_W  @ GHOST_BASE + 20  .. + 29   (right  col of west  neighbour)
//   ghost_E  @ GHOST_BASE + 30  .. + 39   (left   col of east  neighbour)

// ============================================================================
// Debug scratchpad (written by firmware, read back by testbench)
// ============================================================================
#define DEBUG_BASE           0x0700u
#define DEBUG_LAST_RECV_N    (DEBUG_BASE +  0)
#define DEBUG_LAST_RECV_S    (DEBUG_BASE +  4)
#define DEBUG_LAST_RECV_W    (DEBUG_BASE +  8)
#define DEBUG_LAST_RECV_E    (DEBUG_BASE + 12)
#define DEBUG_NEIGHBOR_HIST  (DEBUG_BASE + 16)
#define DEBUG_ITER_COUNT     (DEBUG_BASE + 28)
#define DEBUG_GHOST_FLAGS    (DEBUG_BASE + 32)
#define DEBUG_LIVE_COUNT     (DEBUG_BASE + 36)
#define DEBUG_COL0_BM        (DEBUG_BASE + 40)
#define DEBUG_MY_ID          (DEBUG_BASE + 44)
#define DEBUG_SEND_BM        (DEBUG_BASE + 48)
#define DEBUG_ROW_TRACE_BASE (DEBUG_BASE + 52)   /* 0x0734: rows 0-9 */
#define DEBUG_ROW8_AT_CALL   (DEBUG_BASE + 52)   /* 0x0734 */
#define DEBUG_ROW9_AT_CALL   (DEBUG_BASE + 56)   /* 0x0738 */

// ============================================================================
// Typed pointers into SRAM
// ============================================================================
#define grid      ((volatile uint8_t *)GRID_BASE)
#define ghost_N   ((volatile uint8_t *)(GHOST_BASE +  0))
#define ghost_S   ((volatile uint8_t *)(GHOST_BASE + 10))
#define ghost_W   ((volatile uint8_t *)(GHOST_BASE + 20))
#define ghost_E   ((volatile uint8_t *)(GHOST_BASE + 30))
#define next_grid ((volatile uint8_t *)NEXT_GRID_BASE)

#define debug_last_recv_n   ((volatile uint32_t *)DEBUG_LAST_RECV_N)
#define debug_last_recv_s   ((volatile uint32_t *)DEBUG_LAST_RECV_S)
#define debug_last_recv_w   ((volatile uint32_t *)DEBUG_LAST_RECV_W)
#define debug_last_recv_e   ((volatile uint32_t *)DEBUG_LAST_RECV_E)
#define debug_neighbor_hist ((volatile uint8_t  *)DEBUG_NEIGHBOR_HIST)
#define debug_iter_count    ((volatile uint32_t *)DEBUG_ITER_COUNT)
#define debug_ghost_flags   ((volatile uint32_t *)DEBUG_GHOST_FLAGS)
#define debug_send_bm       ((volatile uint32_t *)DEBUG_SEND_BM)

// ============================================================================
// NoC helpers
// ============================================================================
static inline void noc_write(uint32_t word)
{
    *(volatile uint32_t *)NOC_INJECT_BASE = word;
}

static inline uint32_t noc_recv_raw(void)
{
    return *(volatile uint32_t *)NOC_RECV_BASE;
}

static inline uint32_t noc_read_my_id(void)
{
    return *(volatile uint32_t *)NOC_ID_BASE & 0xFu;
}

static inline void noc_signal(uint32_t sig_word)
{
    noc_write(sig_word);
}

/* Block until a valid ghost flit arrives, return its 10-bit bitmap payload. */
static inline uint32_t recv_ghost(void)
{
    uint32_t p;
    do { p = *(volatile uint32_t *)NOC_RECV_BASE; } while (!(p & FLIT_VALID_BIT));
    return p & FLIT_BMAP_MASK;
}

// ============================================================================
// col_bitmap helpers
//
// col_bitmap_lo(col) — bits 0..7  (rows 0-7)
// col_bitmap_hi(col) — bits 0..1  encode rows 8-9 (caller shifts left by 8)
//
// Kept as two separate functions to avoid compiler issues with shifts > 7 on
// narrow integer paths (the SERV core is bit-serial, so large shifts can be
// miscompiled by certain toolchains).
// ============================================================================
__attribute__((noinline))
static uint32_t col_bitmap_lo(int col)
{
    uint32_t bm = 0;
    int i;
    for (i = 0; i < 8; i++) {
        if (grid[i * SIZE + col] & 1u)
            bm |= (1u << i);
    }
    return bm;
}

__attribute__((noinline))
static uint32_t col_bitmap_hi(int col)
{
    uint32_t bm = 0;
    if (grid[8 * SIZE + col] & 1u) bm |= 1u;   /* row 8 → bit 0 */
    if (grid[9 * SIZE + col] & 1u) bm |= 2u;   /* row 9 → bit 1 */
    return bm;
}

// Combine lo and hi into a 10-bit column bitmap.
// The 8 individual left-shifts avoid any single shift-by-8, which can be
// mis-handled on 8-bit-path compilers.
static inline uint32_t make_col_bitmap(int col)
{
    uint32_t lo = col_bitmap_lo(col);
    uint32_t hi = col_bitmap_hi(col);
    hi = hi << 1; hi = hi << 1; hi = hi << 1; hi = hi << 1;
    hi = hi << 1; hi = hi << 1; hi = hi << 1; hi = hi << 1;
    return (lo | hi) & FLIT_BMAP_MASK;
}

// ============================================================================
// neighbour_count — counts live neighbours of cell (row, col) in this tile,
// consulting ghost buffers for border cells.
// ============================================================================
__attribute__((noinline))
static int neighbour_count(int row, int col)
{
    int idx   = row * SIZE + col;
    int above = (row > 0);
    int below = (row < SIZE - 1);
    int left  = (col > 0);
    int right = (col < SIZE - 1);
    int n = 0;

    /* Row above */
    if (above) {
        if (left)  n += grid[idx - SIZE - 1] & 1;
                   n += grid[idx - SIZE    ] & 1;
        if (right) n += grid[idx - SIZE + 1] & 1;
    } else {
        if (left)  n += ghost_N[col - 1] & 1;
                   n += ghost_N[col    ] & 1;
        if (right) n += ghost_N[col + 1] & 1;
    }

    /* Same row */
    if (left)  n += grid[idx - 1] & 1;
    else       n += ghost_W[row] & 1;
    if (right) n += grid[idx + 1] & 1;
    else       n += ghost_E[row] & 1;

    /* Row below */
    if (below) {
        if (left)  n += grid[idx + SIZE - 1] & 1;
                   n += grid[idx + SIZE    ] & 1;
        if (right) n += grid[idx + SIZE + 1] & 1;
    } else {
        if (left)  n += ghost_S[col - 1] & 1;
                   n += ghost_S[col    ] & 1;
        if (right) n += ghost_S[col + 1] & 1;
    }

    return n;
}

// ============================================================================
// _start — minimal RISC-V reset entry point
//
// Sets up stack, zeroes ghost / next_grid / debug regions, then calls main().
// ============================================================================
__attribute__((section(".text.init"), naked))
void _start(void)
{
    __asm__ volatile (
        "li   sp, 0x7fc\n"
        /* zero ghost region 0x0600..0x0627 */
        "li   t0, 0x0600\n"
        "li   t1, 0x0628\n"
        "1: bge  t0, t1, 2f\n"
        "   sb   zero, 0(t0)\n"
        "   addi t0, t0, 1\n"
        "   j    1b\n"
        /* zero next_grid region 0x0640..0x06a3 */
        "2: li   t0, 0x0640\n"
        "li   t1, 0x06a4\n"
        "3: bge  t0, t1, 4f\n"
        "   sb   zero, 0(t0)\n"
        "   addi t0, t0, 1\n"
        "   j    3b\n"
        /* zero debug region 0x0700..0x077f */
        "4: li   t0, 0x0700\n"
        "li   t1, 0x0780\n"
        "5: bge  t0, t1, 6f\n"
        "   sb   zero, 0(t0)\n"
        "   addi t0, t0, 1\n"
        "   j    5b\n"
        "6: call main\n"
        "7: j    7b\n"
    );
}

// ============================================================================
// main
// ============================================================================
int main(void)
{
    /* Magic canary — testbench reads these back to verify address mapping */
    *(volatile uint32_t *)0x0730u = 0xDEADBEEFu;
    *(volatile uint32_t *)0x0734u = 0xCAFEBABEu;

    uint32_t my_id  = noc_read_my_id();
    *(volatile uint32_t *)DEBUG_MY_ID = my_id;
    int my_row = (int)((my_id >> 2) & 0x3u);
    int my_col = (int)(my_id & 0x3u);

    noc_signal(SIG_BOOT_ALIVE);

    /* -----------------------------------------------------------------------
     * Seed the Game-of-Life grid (same pattern on every tile; the testbench
     * uses build_global_seed() which tiles this pattern across the mesh).
     * --------------------------------------------------------------------- */
    for (int i = 0; i < SIZE * SIZE; i++) grid[i] = 0u;

    /* Blinker (vertical, cols 4-6) */
    grid[4 * SIZE + 5] = 1;
    grid[5 * SIZE + 5] = 1;
    grid[6 * SIZE + 5] = 1;

    /* Corner live pairs */
    grid[8 * SIZE + 0] = 1; grid[9 * SIZE + 0] = 1;
    grid[8 * SIZE + 9] = 1; grid[9 * SIZE + 9] = 1;

    noc_signal(SIG_SEED_LIVE);

    /* -----------------------------------------------------------------------
     * Main GoL loop
     * --------------------------------------------------------------------- */
    uint32_t iter = 0;
    while (1) {
        *debug_iter_count = iter;

        __sync_synchronize();

        /* ── Send column ghosts to horizontal neighbours ─────────────────── */

        /* Send left column (col 0) to the tile on our west */
        if (my_col > 0) {
            uint32_t dest = TILE_ID(my_row, my_col - 1);
            //uint32_t bm   = make_col_bitmap(0)
            uint32_t bm   = 0x300;
            *debug_send_bm = bm;
            noc_write((dest << FLIT_DEST_SHIFT) | FLIT_VALID_BIT | bm);
            *debug_ghost_flags |= 0x8u;
        }

        /* Send right column (col SIZE-1) to the tile on our east */
        if (my_col < MESH_COLS - 1) {
            uint32_t dest = TILE_ID(my_row, my_col + 1);
            //uint32_t bm   = make_col_bitmap(SIZE - 1);
            uint32_t bm   = 0x300;
            noc_write((dest << FLIT_DEST_SHIFT) | FLIT_VALID_BIT | bm);
            *debug_ghost_flags |= 0x4u;
        }

        /* ── Receive column ghosts from horizontal neighbours ──────────────── */

        /* Receive ghost from west neighbour → fill ghost_W */
        if (my_col > 0) {
            uint32_t bmr = recv_ghost();
            *debug_last_recv_w = bmr;
            for (int i = 0; i < SIZE; i++) ghost_W[i] = (bmr >> i) & 1u;
        }

        /* Receive ghost from east neighbour → fill ghost_E */
        if (my_col < MESH_COLS - 1) {
            uint32_t bmr = recv_ghost();
            *debug_last_recv_e = bmr;
            for (int i = 0; i < SIZE; i++) ghost_E[i] = (bmr >> i) & 1u;
        }

        /* ── Compute next generation ────────────────────────────────────────── */
        uint8_t neighbor_counts[9] = {0};
        for (int row = 0; row < SIZE; row++) {
            for (int col = 0; col < SIZE; col++) {
                int alive = grid[row * SIZE + col] & 1;
                int n     = neighbour_count(row, col);
                next_grid[row * SIZE + col] =
                    (uint8_t)(alive ? (n == 2 || n == 3) : (n == 3));
                if (n <= 8) neighbor_counts[n]++;
            }
        }
        for (int i = 0; i <= 8; i++)
            debug_neighbor_hist[i] = neighbor_counts[i];

        noc_signal(SIG_MATH_DONE);

        /* Promote next_grid → grid */
        for (int i = 0; i < SIZE * SIZE; i++)
            grid[i] = next_grid[i];

        noc_signal(SIG_GEN_STABLE);
        iter++;
    }
    return 0;
}