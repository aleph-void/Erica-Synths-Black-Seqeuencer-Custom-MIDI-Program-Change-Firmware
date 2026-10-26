/*
 * MIDI Program Change -> pattern select for the Erica Synths Black Sequencer.
 *
 * Patch code appended to stock firmware 1.06 (EBSEQ_V1_06_7824E1DD.BIN).
 * Every firmware address used here was located by disassembling that image;
 * docs/reverse-engineering-notes.md describes what each one does and how it
 * was identified. tools/patch_firmware.py installs the hooks that call into
 * this file.
 *
 * Threading model (matches the stock firmware):
 *   - pc_midi_dispatch() runs in the EXTI0 software interrupt that drains the
 *     MIDI UART ring buffer. It only records a request and then forwards the
 *     message to the stock dispatcher, so clock/transport/notes are unchanged.
 *   - pc_main_poll() runs in the main loop, the same context in which the
 *     front panel loads patterns from the SD card. All SD access happens here.
 */
#include <stdint.h>

/* ---- stock firmware entry points (Thumb, so bit 0 is set) ---------------- */

typedef struct {
    uint8_t status;
    uint8_t data1;
    uint8_t data2;
    uint8_t length;
} midi_msg_t;

typedef struct {
    uint16_t eeprom_addr;
    uint16_t size;
    void *data;
} fw_setting_t;

#define FW(addr, type) ((type)((uintptr_t)(addr) | 1u))

#define fw_midi_dispatch   FW(0x080294C4, void (*)(midi_msg_t *))
#define fw_song_poll       FW(0x080156B8, void (*)(void))
#define fw_irq_lock        FW(0x080336D0, void (*)(void))
#define fw_irq_unlock      FW(0x080336E0, void (*)(void))
#define fw_pattern_exists  FW(0x0802C1B0, int (*)(int bank, int pattern))
#define fw_bank_select     FW(0x0802C030, void (*)(int bank))
#define fw_song_stop       FW(0x080156EC, void (*)(int restart))
#define fw_pattern_load    FW(0x0802C0A0, void (*)(int pattern))
#define fw_idle_buffer     FW(0x0801370C, void *(*)(void))
#define fw_seq_cue         FW(0x08031B74, void (*)(void *pattern))
#define fw_seq_state       FW(0x08031B04, int (*)(void))
#define fw_seq_swap        FW(0x08031B80, void (*)(int))
#define fw_seq_rebind      FW(0x08031BDC, void (*)(void))
#define fw_commit_select   FW(0x0802C0F0, void (*)(void))
#define fw_widget_next     FW(0x08025B68, void (*)(void *, int, int, int))
#define fw_sprintf         FW(0x08036DCC, int (*)(char *, const char *, ...))
#define fw_setting_load    FW(0x0802CB34, int (*)(const fw_setting_t *))
#define fw_setting_save    FW(0x0802CB70, void (*)(const fw_setting_t *))
#define fw_devcfg_save     FW(0x0801EC58, void (*)(void))

/* ---- stock firmware state ------------------------------------------------ */

/* Project state: +0x14 playing bank, +0x15 playing pattern,
 *                +0x16 selected bank, +0x17 selected (queued) pattern. */
#define PROJECT        ((volatile uint8_t *)0x2001E274)
#define PRJ_CUR_BANK   0x14
#define PRJ_CUR_PAT    0x15
#define PRJ_SEL_BANK   0x16

#define CHAIN_COUNT    (*(volatile uint16_t *)(0x20008718 + 0x80))
#define SEQ_ENGINE     (*(uint8_t *volatile *)0x20005A8C)
#define SEQ_NEXT(seq)  (*(void *volatile *)((seq) + 0x0C))
#define PATTERN_PAGE   (*(uint8_t *volatile *)0x2000D874)

#define SEQ_PLAYING    1

/* ---- patch state --------------------------------------------------------- */

#define PC_MAGIC        0x50   /* 'P' */
#define PC_EEPROM_ADDR  0x0500 /* first slot after the stock 0x000-0x4FF map */

#define REQ_VALID       0x8000u
#define REQ_BANK_CUR    0x7Fu  /* use the currently selected bank */

#define BANK_NONE       0x00   /* no Bank Select received yet */
#define BANK_INVALID    0xFF   /* last Bank Select was out of range */

typedef struct {
    uint8_t magic;
    uint8_t channel;      /* 0 = off, 1..16 */
    uint8_t instant;      /* 0 = change at end of master length, 1 = now */
    uint8_t bank_select;  /* 1 = CC#0 on the PC channel selects the bank */
} pc_cfg_t;

/* Lives in the 32 bytes reserved after stock .bss (zeroed by stock startup). */
static struct {
    pc_cfg_t cfg;
    volatile uint16_t pending;  /* REQ_VALID | bank << 8 | pattern */
    volatile uint8_t bank_reg;  /* BANK_NONE, BANK_INVALID or bank + 1 */
    uint8_t dirty;
} pc __attribute__((section(".pcram")));

static const fw_setting_t pc_setting = {
    PC_EEPROM_ADDR, sizeof(pc_cfg_t), &pc.cfg
};

static void pc_cfg_sanitize(void)
{
    if (pc.cfg.magic != PC_MAGIC || pc.cfg.channel > 16) {
        pc.cfg.magic = PC_MAGIC;
        pc.cfg.channel = 0;
        pc.cfg.instant = 0;
        pc.cfg.bank_select = 0;
    }
    pc.cfg.instant = pc.cfg.instant ? 1 : 0;
    pc.cfg.bank_select = pc.cfg.bank_select ? 1 : 0;
}

/* ---- hook: MIDI message dispatch (EXTI0 interrupt context) --------------- */

void pc_midi_dispatch(midi_msg_t *msg)
{
    uint8_t status = msg->status;
    uint8_t channel = pc.cfg.channel;

    if (channel != 0 && status < 0xF0 && (status & 0x0F) == channel - 1) {
        uint8_t type = status & 0xF0;

        if (type == 0xC0) {
            uint8_t program = msg->data1;
            uint8_t bank = REQ_BANK_CUR;

            if (pc.cfg.bank_select && pc.bank_reg != BANK_NONE) {
                bank = pc.bank_reg == BANK_INVALID ? BANK_INVALID
                                                   : pc.bank_reg - 1;
            }
            if (program < 16 && bank != BANK_INVALID)
                pc.pending = REQ_VALID | (uint16_t)(bank << 8) | program;
        } else if (type == 0xB0 && msg->data1 == 0 && pc.cfg.bank_select) {
            pc.bank_reg = msg->data2 < 16 ? msg->data2 + 1 : BANK_INVALID;
        }
    }

    fw_midi_dispatch(msg);
}

/* ---- hook: main loop ------------------------------------------------------ */

static void pc_refresh_saved_mask(uint8_t *ctx, int bank)
{
    uint16_t mask = 0;
    int i;

    for (i = 0; i < 16; i++) {
        if (fw_pattern_exists(bank, i))
            mask |= 1u << i;
    }
    *(volatile uint16_t *)(ctx + 0xBE) = mask;
}

static void pc_apply(uint16_t req)
{
    int pattern = req & 0x0F;
    int bank = (req >> 8) & 0x7F;
    int bank_changed;
    int instant;
    uint8_t *page;
    uint8_t *ctx;

    if (bank == REQ_BANK_CUR)
        bank = PROJECT[PRJ_SEL_BANK];

    /* Empty slots are ignored rather than switching to a blank pattern. */
    if (!fw_pattern_exists(bank, pattern))
        return;

    /* Re-selecting the playing pattern with nothing queued would only reload
     * it from the SD card and discard unsaved edits, so ignore it. */
    if (bank == PROJECT[PRJ_CUR_BANK] && pattern == PROJECT[PRJ_CUR_PAT] &&
        SEQ_NEXT(SEQ_ENGINE) == 0)
        return;

    bank_changed = bank != PROJECT[PRJ_SEL_BANK];
    if (bank_changed)
        fw_bank_select(bank);

    /* Same sequence as pressing a step encoder on the PATTERN page. */
    CHAIN_COUNT = 0;
    fw_song_stop(0);

    /* Un-queue the idle buffer while it is being overwritten so a master
     * length wrap during the SD read cannot swap in a half-loaded pattern. */
    fw_irq_lock();
    fw_seq_cue(0);
    fw_irq_unlock();

    fw_pattern_load(pattern);
    fw_seq_cue(fw_idle_buffer());

    instant = pc.cfg.instant || fw_seq_state() != SEQ_PLAYING;

    page = PATTERN_PAGE;
    ctx = *(uint8_t **)(page + 0x0C);
    fw_widget_next(ctx + 0x14, bank, pattern, instant);

    if (instant) {
        fw_seq_swap(1);
        fw_seq_rebind();
        fw_commit_select();
    }

    if (bank_changed)
        pc_refresh_saved_mask(ctx, bank);
}

void pc_main_poll(void)
{
    uint16_t req;

    fw_song_poll();

    fw_irq_lock();
    req = pc.pending;
    pc.pending = 0;
    fw_irq_unlock();

    if (req & REQ_VALID)
        pc_apply(req);
}

/* ---- hook: settings load at boot ----------------------------------------- */

int pc_settings_init(const fw_setting_t *stock_setting)
{
    int ret = fw_setting_load(stock_setting);

    pc.pending = 0;
    pc.bank_reg = BANK_NONE;
    pc.dirty = 0;
    if (fw_setting_load(&pc_setting) != 0)
        pc.cfg.magic = 0;
    pc_cfg_sanitize();
    return ret;
}

/* ---- hooks: MIDI settings page, MISC tab --------------------------------- */

void pc_midi_page_exit(void)
{
    fw_devcfg_save();
    if (pc.dirty) {
        fw_setting_save(&pc_setting);
        pc.dirty = 0;
    }
}

static const char str_on[] = "ON";
static const char str_off[] = "OFF";

void pc_fmt_row(int row, char *buf)
{
    switch (row) {
    case 1:
        if (pc.cfg.channel)
            fw_sprintf(buf, "PC Channel      %3d", pc.cfg.channel);
        else
            fw_sprintf(buf, "PC Channel      %3s", str_off);
        break;
    case 2:
        fw_sprintf(buf, "PC Quantize     %3s",
                   pc.cfg.instant ? str_off : str_on);
        break;
    case 3:
        fw_sprintf(buf, "Bank Select     %3s",
                   pc.cfg.bank_select ? str_on : str_off);
        break;
    default:
        buf[0] = 0;
        break;
    }
}

void pc_edit_row(int row, int delta)
{
    int v;

    switch (row) {
    case 1:
        v = pc.cfg.channel + delta;
        if (v < 0)
            v = 0;
        if (v > 16)
            v = 16;
        pc.cfg.channel = v;
        break;
    case 2:
        pc.cfg.instant = delta > 0 ? 0 : 1;
        break;
    case 3:
        pc.cfg.bank_select = delta > 0 ? 1 : 0;
        pc.bank_reg = BANK_NONE;
        break;
    default:
        return;
    }
    pc.dirty = 1;
}
