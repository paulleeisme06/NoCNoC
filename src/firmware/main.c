#include <stdint.h>

#define NOC_INJECT_BASE  0x80000000u
#define NOC_RECV_BASE    0x80000004u
#define NOC_ID_BASE      0x80000008u

#define TILE_ID(r,c)     (((uint32_t)(r) << 2) | (uint32_t)(c))
#define FLIT_DEST_SHIFT  28u
#define FLIT_BMAP_MASK   0x3FFu
#define FLIT_VALID_BIT   0x400u

#define SIG_BOOT_ALIVE   0xF0000001u
#define SIG_SEED_LIVE    0xF0000002u
#define SIG_MATH_DONE    0xF0000003u
#define SIG_GEN_STABLE   0xF0000004u

#define SIZE       10
#define MESH_ROWS  3
#define MESH_COLS  3

#define GRID_BASE       0x0500u
#define GHOST_BASE      0x0600u
#define NEXT_GRID_BASE  0x0640u

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
#define DEBUG_ROW_TRACE_BASE (DEBUG_BASE + 52)   /* 0x0734 */
#define DEBUG_ROW8_AT_CALL   (DEBUG_BASE + 52)   /* 0x0734: row-8 contribution shifted into bit 8 */
#define DEBUG_ROW9_AT_CALL   (DEBUG_BASE + 56)   /* 0x0738: row-9 contribution shifted into bit 9 */
#define DEBUG_PRE_OR_S8      (DEBUG_BASE + 60)   /* 0x073C: shifted8 right before OR */
#define DEBUG_PRE_OR_S9      (DEBUG_BASE + 64)   /* 0x0740: shifted9 right before OR */
#define DEBUG_CELL9_RAW      (DEBUG_BASE + 68)   /* 0x0744: raw grid[9*SIZE+col] byte */
#define DEBUG_BIT9_VAL       (DEBUG_BASE + 72)   /* 0x0748: bit9 after & 1 */
#define DEBUG_SEND_BM        (DEBUG_BASE + 80)   /* 0x0750: final bm sent west (matches test) */

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

static inline uint32_t recv_ghost(void)
{
    uint32_t p;
    do { p = *(volatile uint32_t *)NOC_RECV_BASE; } while (!(p & FLIT_VALID_BIT));
    return p & FLIT_BMAP_MASK;
}

/*__attribute__((noinline))
static uint32_t col_bitmap(int col)
{
    volatile uint8_t *g = (volatile uint8_t *)GRID_BASE;
    uint32_t bm = 0;
    uint32_t i;
    for (i = 0; i < 8; i++) {
        uint32_t cell = g[i * 10 + col];
        uint32_t bit  = cell & 1u;
        uint32_t shifted = bit << i;
        bm = bm | shifted;
    }
    return bm;
}*/

__attribute__((noinline))
static uint32_t col_bitmap_lo(int col)
{
    uint32_t bm = 0;
    int i;
    for (i = 0; i < 8; i++) {
        if (grid[i * SIZE + col] & 1u)
            bm |= (1u << i);   /* shifts 0-7 only, always safe */
    }
    return bm;
}


__attribute__((noinline))
static int neighbour_count(int row, int col)
{
    int idx   = row * SIZE + col;
    int above = (row > 0);
    int below = (row < SIZE - 1);
    int left  = (col > 0);
    int right = (col < SIZE - 1);
    int n = 0;
    if (above) {
        if (left)  n += grid[idx - SIZE - 1] & 1;
                n += grid[idx - SIZE    ] & 1;
        if (right) n += grid[idx - SIZE + 1] & 1;
    } else {
        if (left)  n += ghost_N[col - 1] & 1;
                n += ghost_N[col    ] & 1;
        if (right) n += ghost_N[col + 1] & 1;
    }
    if (left)  n += grid[idx - 1] & 1;
    else       n += ghost_W[row] & 1;
    if (right) n += grid[idx + 1] & 1;
    else       n += ghost_E[row] & 1;
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

static const uint32_t PATTERN[30] = {
    0x155FFE01u,  /* row  0: tile(0,0)=X  tile(0,1)=Ring  tile(0,2)=VStripes */
    0x15580502u,  /* row  1 */
    0x15580484u,  /* row  2 */
    0x15580448u,  /* row  3 */
    0x15580430u,  /* row  4 */
    0x15580430u,  /* row  5 */
    0x15580448u,  /* row  6 */
    0x15580484u,  /* row  7 */
    0x15580502u,  /* row  8 */
    0x155FFE01u,  /* row  9 */
    0x155043FFu,  /* row 10: tile(1,0)=HStripes  tile(1,1)=Diamond  tile(1,2)=Checker */
    0x2AA0A000u,  /* row 11 */
    0x155113FFu,  /* row 12 */
    0x2AA20800u,  /* row 13 */
    0x155407FFu,  /* row 14 */
    0x2AA20800u,  /* row 15 */
    0x155113FFu,  /* row 16 */
    0x2AA0A000u,  /* row 17 */
    0x155043FFu,  /* row 18 */
    0x2AA00000u,  /* row 19 */
    0x00000800u,  /* row 20: tile(2,0)=Block  tile(2,1)=Glider  tile(2,2)=Blinker */
    0x00001000u,  /* row 21 */
    0x00001C00u,  /* row 22 */
    0x00000000u,  /* row 23 */
    0x00000030u,  /* row 24 */
    0x03800030u,  /* row 25 */
    0x00000000u,  /* row 26 */
    0x00000000u,  /* row 27 */
    0x00000000u,  /* row 28 */
    0x00000000u,  /* row 29 */
};

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
        /* zero debug region 0x0700..0x0723 */
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

int main(void)
{
    /* MAGIC CHECK — proves testbench can read what firmware writes to 0x0730 */
    *(volatile uint32_t *)0x0730u = 0xDEADBEEFu;
    *(volatile uint32_t *)0x0734u = 0xCAFEBABEu;

    uint32_t my_id = noc_read_my_id();
    *(volatile uint32_t *)DEBUG_MY_ID = my_id;
    int my_row = (int)((my_id >> 2) & 0x3u);
    int my_col = (int)(my_id & 0x3u);

    noc_signal(SIG_BOOT_ALIVE);

    {
        int y_off = my_row * SIZE;
        int x_off = my_col * SIZE;
        for (int row = 0; row < SIZE; row++) {
            uint32_t bits = PATTERN[y_off + row] >> x_off;
            for (int col = 0; col < SIZE; col++)
                grid[row * SIZE + col] = (uint8_t)((bits >> col) & 1u);
        }
    }

    noc_signal(SIG_SEED_LIVE);

    uint32_t iter = 0;
    while (1) {
        __sync_synchronize();

        /* Phase A: send col9 east, then recv col9 from west → ghost_W. */
        if (my_col < MESH_COLS - 1) {
            uint32_t dest   = TILE_ID(my_row, my_col + 1);
            uint32_t bm9_lo = col_bitmap_lo(SIZE - 1);
            uint32_t s8     = (uint32_t)(grid[8 * SIZE + (SIZE - 1)] & 1u) << 8;
            uint32_t s9     = (uint32_t)(grid[9 * SIZE + (SIZE - 1)] & 1u) << 9;
            uint32_t bm9    = bm9_lo | s8 | s9;
            noc_write((dest << FLIT_DEST_SHIFT) | FLIT_VALID_BIT | (bm9 & FLIT_BMAP_MASK));
        }
        if (my_col > 0) {
            uint32_t bmr = recv_ghost();
            *debug_last_recv_w = bmr;
            for (int i = 0; i < SIZE; i++) ghost_W[i] = (bmr >> i) & 1u;
        }

        /* Phase B: send col0 west, then recv col0 from east → ghost_E. */
        if (my_col > 0) {
            uint32_t dest   = TILE_ID(my_row, my_col - 1);
            uint32_t bm0_lo = col_bitmap_lo(0);
            uint32_t s8     = (uint32_t)(grid[8 * SIZE + 0] & 1u) << 8;
            uint32_t s9     = (uint32_t)(grid[9 * SIZE + 0] & 1u) << 9;
            uint32_t bm0    = bm0_lo | s8 | s9;
            noc_write((dest << FLIT_DEST_SHIFT) | FLIT_VALID_BIT | (bm0 & FLIT_BMAP_MASK));
        }
        if (my_col < MESH_COLS - 1) {
            uint32_t bmr = recv_ghost();
            *debug_last_recv_e = bmr;
            for (int i = 0; i < SIZE; i++) ghost_E[i] = (bmr >> i) & 1u;
        }

        for (int row = 0; row < SIZE; row++) {
            for (int col = 0; col < SIZE; col++) {
                int alive = grid[row * SIZE + col] & 1;
                int n     = neighbour_count(row, col);
                next_grid[row * SIZE + col] =
                    (uint8_t)(alive ? (n == 2 || n == 3) : (n == 3));
            }
        }

        noc_signal(SIG_MATH_DONE);

        for (int i = 0; i < SIZE * SIZE; i++)
            grid[i] = next_grid[i];

        noc_signal(SIG_GEN_STABLE);
        iter++;
    }
    return 0;
}