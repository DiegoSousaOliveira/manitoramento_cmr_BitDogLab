/*
 * matriz.c — matriz 5x5 de LEDs WS2812.
 *
 * Os WS2812 recebem os bits num ritmo muito preciso (800 kHz); quem gera esse sinal é o PIO
 * (um "mini processador" do RP2040), com o programa ws2812.pio do próprio SDK.
 */
#include "matriz.h"

#include <string.h>

#include "config.h"
#include "hardware/pio.h"
#include "ws2812.pio.h"

static PIO pio;
static uint sm;
static bool pronta = false;
static uint32_t ultima[5][5];
static bool ja_mostrou = false;

bool matriz_iniciar(void) {
    uint offset;
    /* Pega um PIO livre (o chip do Wi-Fi também usa um PIO). */
    if (!pio_claim_free_sm_and_add_program_for_gpio_range(&ws2812_program, &pio, &sm, &offset, PINO_MATRIZ, 1, true)) {
        return false;
    }
    ws2812_program_init(pio, sm, offset, PINO_MATRIZ, 800000, false);
    pronta = true;
    uint32_t apagada[5][5] = {{0}};
    matriz_mostrar(apagada);
    return true;
}

/* Os LEDs estão ligados em "zigue-zague", começando no canto de BAIXO à direita (BitDogLab). */
static int indice(int linha, int coluna) {
    return linha % 2 == 0 ? 24 - (linha * 5 + coluna) : 24 - (linha * 5 + (4 - coluna));
}

static uint32_t escurecer(uint32_t componente) { return componente * BRILHO_MATRIZ / 255; }

void matriz_mostrar(const uint32_t cores[5][5]) {
    if (!pronta || (ja_mostrou && memcmp(cores, ultima, sizeof ultima) == 0)) {
        return;
    }
    uint32_t grb[25];
    for (int linha = 0; linha < 5; linha++) {
        for (int coluna = 0; coluna < 5; coluna++) {
            uint32_t c = cores[linha][coluna];
            uint32_t r = escurecer((c >> 16) & 0xFF), g = escurecer((c >> 8) & 0xFF), b = escurecer(c & 0xFF);
            grb[indice(linha, coluna)] = (g << 16) | (r << 8) | b; /* o WS2812 quer verde, vermelho, azul */
        }
    }
    for (int i = 0; i < 25; i++) {
        pio_sm_put_blocking(pio, sm, grb[i] << 8u);
    }
    memcpy(ultima, cores, sizeof ultima);
    ja_mostrou = true;
}
