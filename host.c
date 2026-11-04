// PTFX native test host: runs a plugin on raw int16 PCM (stdin->stdout).
// Args: host <sample_rate> <channels> <in_len> <out_len> <scratch> <mono> <param0> [param1 ...]
// Prints "PTFX_OUT_BYTES <n>" on stderr where n is the real processed length.
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "tracker_plugin.h"

#define MAX_PARAMS 12
#define CHUNK 8192

static PtfxHostApi g_host;

static uint32_t g_prepend = 0;
static uint32_t g_append = 0;
static uint32_t g_extra_total = 0;

static void set_extra_samples(uint32_t prepend, uint32_t append) {
    g_prepend = prepend;
    g_append = append;
    g_extra_total = prepend + append;
}

int main(int argc, char** argv) {
    if (argc < 8) {
        fprintf(stderr, "usage: host <rate> <channels> <in_len> <out_len> <scratch> <mono> <params...>\n");
        return 2;
    }

    uint32_t sample_rate = (uint32_t)strtoul(argv[1], 0, 10);
    uint8_t channels = (uint8_t)atoi(argv[2]);
    uint32_t in_len = (uint32_t)strtoul(argv[3], 0, 10);
    uint32_t out_len = (uint32_t)strtoul(argv[4], 0, 10);
    uint32_t scratch_size = (uint32_t)strtoul(argv[5], 0, 10);
    int mono = atoi(argv[6]);

    if (channels < 1) channels = 1;
    if (channels > 2) channels = 2;
    if (sample_rate == 0) sample_rate = 44100;

    float params[MAX_PARAMS] = {0};
    int num_params = 0;
    for (int i = 7; i < argc && num_params < MAX_PARAMS; i++) {
        params[num_params++] = (float)atof(argv[i]);
    }

    g_host.sample_rate = sample_rate;
    g_host.sinf = sinf;
    g_host.cosf = cosf;
    g_host.tanf = tanf;
    g_host.tanhf = tanhf;
    g_host.powf = powf;
    g_host.expf = expf;
    g_host.sqrtf = sqrtf;
    g_host.logf = logf;
    g_host.log10f = log10f;
    g_host.fminf = fminf;
    g_host.fmaxf = fmaxf;
    g_host.fabsf = fabsf;
    g_host.memcpy = memcpy;
    g_host.memset = memset;
    g_host.rand = rand;
    g_host.set_extra_samples = set_extra_samples;

    // Input is channel-interleaved on stdin; firmware hands plugins contiguous
    // per-channel pointers, so de-interleave here.
    size_t in_bytes = (size_t)in_len * channels * sizeof(int16_t);
    int16_t* in_raw = malloc(in_bytes ? in_bytes : 1);
    if (!in_raw) return 1;
    size_t got = in_bytes ? fread(in_raw, 1, in_bytes, stdin) : in_bytes;
    if (in_bytes > 0 && got != in_bytes) {
        fprintf(stderr, "PTFX_HOST_ERR: short input (%zu of %zu bytes)\n", got, in_bytes);
        return 1;
    }
    int16_t* in_pcm[2] = {0, 0};
    if (in_len > 0) {
        in_pcm[0] = malloc((size_t)in_len * sizeof(int16_t));
        if (channels > 1) in_pcm[1] = malloc((size_t)in_len * sizeof(int16_t));
        if (!in_pcm[0] || (channels > 1 && !in_pcm[1])) return 1;
        for (uint32_t i = 0; i < in_len; i++) {
            in_pcm[0][i] = in_raw[(size_t)i * channels];
            if (channels > 1) in_pcm[1][i] = in_raw[(size_t)i * channels + 1];
        }
    }

    int16_t* scratch = malloc(scratch_size ? scratch_size : 1);
    if (!scratch) return 1;
    memset(scratch, 0, scratch_size);

    extern void plugin_init(void* scratch_mem, uint32_t scratch_size, const PtfxHostApi* host);
    extern void plugin_set_param(uint8_t index, float value);
    extern int32_t plugin_process_block(const int16_t** in, int16_t** out, uint32_t samples, uint8_t channels);

    plugin_init(scratch, scratch_size, &g_host);
    for (int i = 0; i < num_params; i++) {
        plugin_set_param((uint8_t)i, params[i]);
    }

    // set_extra_samples() may have been called during init/set_param.
    uint32_t total = in_len + g_extra_total;
    if (total < out_len) total = out_len;

    int16_t* out_ch[2] = {0, 0};
    out_ch[0] = malloc((size_t)total * sizeof(int16_t));
    if (channels > 1) out_ch[1] = malloc((size_t)total * sizeof(int16_t));
    if (!out_ch[0] || (channels > 1 && !out_ch[1])) return 1;

    uint32_t produced = 0;
    for (uint32_t pos = 0; pos < total; ) {
        uint32_t n = total - pos;
        if (n > CHUNK) n = CHUNK;

        // Clamp at region boundaries, exactly like mtEffectModular::processBlock():
        // a block is either fully inside the audio body or fully in silence.
        const int16_t* in_block[2] = {0, 0};
        uint32_t body_start = g_prepend;
        uint32_t body_end = g_prepend + in_len;
        if (pos < body_start) {
            if (n > body_start - pos) n = body_start - pos;
        } else if (pos < body_end) {
            if (n > body_end - pos) n = body_end - pos;
            uint32_t off = pos - body_start;
            in_block[0] = in_pcm[0] + off;
            if (channels > 1) in_block[1] = in_pcm[1] + off;
        }

        int16_t* out_block[2] = { out_ch[0] + pos, 0 };
        if (channels > 1) out_block[1] = out_ch[1] + pos;
        int32_t r = plugin_process_block(in_block, out_block, n, channels);
        if (r <= 0) break;
        if ((uint32_t)r > n) r = (int32_t)n;
        produced += (uint32_t)r;
        pos += (uint32_t)r;
    }

    int out_ch_count = mono ? 1 : channels;
    for (uint32_t i = 0; i < produced; i++) {
        for (int c = 0; c < out_ch_count; c++) {
            int16_t v = out_ch[0][i];
            if (out_ch_count > 1) v = out_ch[c] ? out_ch[c][i] : out_ch[0][i];
            fputc(v & 0xFF, stdout);
            fputc((v >> 8) & 0xFF, stdout);
        }
    }
    fflush(stdout);
    fprintf(stderr, "PTFX_OUT_BYTES %u\n", produced);
    return 0;
}
